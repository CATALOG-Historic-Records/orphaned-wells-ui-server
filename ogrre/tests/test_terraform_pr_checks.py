"""Regressions for credential-free PR checks and retiring PR cloud access."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
PRINCIPAL = (
    "principalSet://iam.googleapis.com/projects/123/locations/global/"
    "workloadIdentityPools/ogrre-terraform/attribute.pipeline"
)


def test_pr_checks_have_no_cloud_identity_or_privileged_followup():
    # BaseLoader preserves GitHub's YAML 1.2 "on" key and scalar strings.
    workflow = yaml.load(
        (ROOT / ".github/workflows/terraform-checks.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert set(workflow["on"]) == {"pull_request"}
    assert workflow["permissions"] == {"contents": "read"}
    assert "environment" not in workflow
    for job in workflow["jobs"].values():
        assert job["runs-on"] == "ubuntu-latest"
        assert "environment" not in job
        assert "permissions" not in job
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] == "false"
    terraform_steps = workflow["jobs"]["terraform"]["steps"]
    setup = next(
        step
        for step in terraform_steps
        if step.get("uses") == "./.github/actions/setup-terraform"
    )
    assert setup["with"] == {"mode": "validate"}
    pr_code = (
        json.dumps(workflow)
        + (ROOT / ".github/actions/setup-terraform/action.yml").read_text()
    )
    for privileged_input in ("secrets.", "id-token:", "google-github-actions/auth"):
        assert privileged_input not in pr_code
    assert not (ROOT / ".github/workflows/terraform-plan.yml").exists()
    assert not (ROOT / "deployment/ci/terraform_pr_plan.py").exists()
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        assert "allow-unsafe-pr-checkout" not in path.read_text()


@pytest.fixture
def bootstrap(tmp_path):
    """Run the actual shell script, replacing every cloud/GitHub CLI call."""
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    cli = binary_dir / "gcloud"
    cli.write_text(
        f"#!{sys.executable}\n"
        + """import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
with open(os.environ["CLI_LOG"], "a") as log:
    log.write(json.dumps([Path(sys.argv[0]).name, *args]) + "\\n")
# Model a rerun against policies that already contain conditional bindings.
if "add-iam-policy-binding" in args and not any(arg.startswith("--condition=") for arg in args):
    sys.exit("An existing conditional policy requires an explicit condition")
if Path(sys.argv[0]).name == "gh":
    print("456" if args[-1] == ".id" else "789")
elif args[:2] == ["projects", "describe"]:
    print("123")
elif args[:3] == ["iam", "service-accounts", "get-iam-policy"]:
    if os.environ.get("POLICY_ERROR") == "read":
        sys.exit(1)
    print(Path(os.environ["POLICY_FILE"]).read_text())
elif args[:3] == ["iam", "service-accounts", "remove-iam-policy-binding"]:
    if os.environ.get("POLICY_ERROR") == "remove":
        sys.exit(1)
    path = Path(os.environ["POLICY_FILE"])
    policy = json.loads(path.read_text())
    member = next(arg.removeprefix("--member=") for arg in args if arg.startswith("--member="))
    for binding in policy.get("bindings", []):
        if binding.get("role") == "roles/iam.workloadIdentityUser" and not binding.get("condition"):
            binding["members"] = [value for value in binding["members"] if value != member]
    path.write_text(json.dumps(policy))
"""
    )
    cli.chmod(0o700)
    (binary_dir / "gh").symlink_to(cli)
    (binary_dir / "python3").symlink_to(sys.executable)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"bindings": []}))
    log = tmp_path / "cli.jsonl"
    environment = {
        "PATH": f"{binary_dir}:{os.defpath}",
        "HOME": str(tmp_path),
        "PROJECT_ID": "example-project",
        "TF_WORKSPACE": "ogrre",
        "TF_CI_BUCKET": "example-terraform-ci",
        "DEPLOY_SERVICE_ACCOUNT": "deploy@example-project.iam.gserviceaccount.com",
        "CLI_LOG": str(log),
        "POLICY_FILE": str(policy),
    }

    def run():
        log.write_text("")
        result = subprocess.run(
            ["bash", str(ROOT / "deployment/ci/bootstrap_terraform_ci.sh")],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return result, calls

    return run, policy, environment


@pytest.mark.parametrize("legacy_binding", [False, True])
def test_bootstrap_retires_pr_access_and_preserves_main_access(
    bootstrap, legacy_binding
):
    run, policy, _ = bootstrap
    members = [f"{PRINCIPAL}/main-plan"]
    if legacy_binding:
        members.append(f"{PRINCIPAL}/pr-plan")
    policy.write_text(
        json.dumps(
            {
                "bindings": [
                    {"role": "roles/iam.workloadIdentityUser", "members": members}
                ]
            }
        )
    )
    result, calls = run()
    assert result.returncode == 0, result.stdout + result.stderr
    provider = next(call for call in calls if "update-oidc" in call)
    mapping = next(arg for arg in provider if arg.startswith("--attribute-mapping="))
    assert "/terraform-apply.yml@refs/heads/main" in mapping
    assert "assertion.ref == 'refs/heads/main'" in mapping
    assert "assertion.event_name in ['push', 'workflow_dispatch']" in mapping
    assert ": 'denied'" in mapping
    assert "pr-plan" not in mapping
    assert "workflow_run" not in mapping
    condition = next(
        arg for arg in provider if arg.startswith("--attribute-condition=")
    )
    assert "assertion.repository_id == '456'" in condition
    assert "assertion.repository_owner_id == '789'" in condition
    assert "attribute.pipeline != 'denied'" in condition
    removals = [call for call in calls if "remove-iam-policy-binding" in call]
    assert len(removals) == int(legacy_binding)
    if removals:
        assert f"--member={PRINCIPAL}/pr-plan" in removals[0]
        assert "--condition=None" in removals[0]
    grants = [call for call in calls if "add-iam-policy-binding" in call]
    assert any(f"--member={PRINCIPAL}/main-plan" in call for call in grants)
    assert any(f"--member={PRINCIPAL}/apply" in call for call in grants)
    assert not any(f"--member={PRINCIPAL}/pr-plan" in call for call in grants)
    for call in grants:
        condition = next(arg for arg in call if arg.startswith("--condition="))
        if "--role=roles/storage.objectAdmin" in call:
            if "gs://example-terraform-ci" in call:
                assert (
                    "--member=serviceAccount:github-terraform-plan@example-project.iam.gserviceaccount.com"
                    in call
                )
                assert condition == (
                    "--condition=expression=resource.name == 'projects/_/buckets/"
                    "example-terraform-ci/objects/status/ogrre.json',title=terraform-readiness"
                )
            else:
                assert condition == (
                    "--condition=expression=resource.name == 'projects/_/buckets/"
                    "tidy-outlet-412020-ogrre-terraform-state/objects/"
                    "orphaned-wells-ui-server/ogrre.tflock',title=terraform-plan-lock"
                )
        elif "--role=roles/storage.objectCreator" in call:
            assert condition == (
                "--condition=expression=resource.name.startsWith('projects/_/buckets/"
                "example-terraform-ci/objects/plans/'),title=main-plan-artifacts"
            )
        else:
            assert condition == "--condition=None"
    assert json.loads(policy.read_text())["bindings"][0]["members"] == [
        f"{PRINCIPAL}/main-plan"
    ]
    assert any("title=terraform-readiness" in arg for call in grants for arg in call)
    # A second bootstrap is safe after the legacy binding has been removed.
    result, calls = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any("remove-iam-policy-binding" in call for call in calls)


@pytest.mark.parametrize("failure", ["read", "parse", "remove"])
def test_bootstrap_does_not_hide_failed_pr_access_cleanup(bootstrap, failure):
    run, policy, environment = bootstrap
    policy.write_text(
        json.dumps(
            {
                "bindings": [
                    {
                        "role": "roles/iam.workloadIdentityUser",
                        "members": [f"{PRINCIPAL}/pr-plan"],
                    }
                ]
            }
        )
    )
    if failure == "parse":
        policy.write_text("invalid JSON")
    else:
        environment["POLICY_ERROR"] = failure
    result, calls = run()
    assert result.returncode != 0
    assert "Bootstrap complete" not in result.stdout
    assert not any("add-iam-policy-binding" in call for call in calls)
