"""Cloud-free regressions for CI ordering, input revisions, and saved-plan integrity."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


def load_script(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / f"deployment/ci/{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci = load_script("terraform_ci")
changes = load_script("changed_paths")
manifest = load_script("validate_manifest")


@pytest.mark.parametrize(
    "paths,expected",
    [
        (["ogrre/routers/router.py"], (False, False)),
        (["deployment/kubernetes/backend.yaml"], (False, True)),
        (["deployment/terraform/.terraform.lock.hcl"], (True, False)),
        (["deployment/terraform/.terraform-version"], (True, False)),
        (["deployment/terraform/README.md"], (False, False)),
        (["deployment/terraform/terraform.tfvars.example"], (False, False)),
        (["deployment/ci/terraform_pr_plan.py"], (True, False)),
        (["deployment/terraform/modules/backend_vm/startup.sh"], (True, False)),
        (
            ["deployment/terraform/main.tf", "deployment/kubernetes/backend.yaml"],
            (True, True),
        ),
    ],
)
def test_change_scope(paths, expected):
    assert tuple(changes.classify(paths).values()) == expected


def test_revision_survives_backend_and_documentation_commits(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "ROOT", tmp_path)
    ci.run("git", "init", "-q")
    ci.run("git", "config", "user.email", "test@example.invalid")
    ci.run("git", "config", "user.name", "CI test")

    def commit(path, content):
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        ci.run("git", "add", ".")
        ci.run("git", "commit", "-qm", "test")

    commit("deployment/terraform/main.tf", "initial")
    applied = ci.revision()
    commit("deployment/terraform/main.tf", "pending infrastructure")
    pending = ci.revision()
    assert pending != applied
    commit("ogrre/app.py", "backend-only follow-up")
    commit("deployment/terraform/README.md", "documentation")
    assert ci.revision() == pending
    commit("deployment/terraform/modules/vm/startup.sh", "new module asset")
    assert ci.revision() != pending


@pytest.mark.parametrize(
    "changed",
    [
        None,
        {},
        {"status": "applying"},
        {"revision": "old"},
        {"state": "other"},
        {"generation": "older"},
    ],
)
def test_readiness_fails_closed(changed):
    marker = dict(status="applied", revision="current", state="state", generation="42")
    if changed is None or changed == {}:
        marker = changed
    else:
        marker.update(changed)
    assert not ci.is_ready(marker, "current", "state", "42")


def test_readiness_requires_successful_inputs_and_current_state():
    marker = dict(status="applied", revision="current", state="state", generation="42")
    assert ci.is_ready(marker, "current", "state", "42")


@pytest.mark.parametrize(
    "error_text,missing",
    [("HTTPError 404", True), ("403 Forbidden", False), ("network error", False)],
)
def test_missing_marker_is_distinct_from_unreadable_marker(
    monkeypatch, error_text, missing
):
    def fail(*args):
        raise subprocess.CalledProcessError(1, args, stderr=error_text)

    monkeypatch.setattr(ci, "run", fail)
    if missing:
        assert ci.read_marker("gs://bucket/status/ogrre.json") is None
    else:
        with pytest.raises(subprocess.CalledProcessError):
            ci.read_marker("gs://bucket/status/ogrre.json")


def test_saved_plan_binds_content_and_metadata():
    plan = b"saved binary plan"
    metadata = dict(
        sha256=hashlib.sha256(plan).hexdigest(),
        commit="abc",
        workspace="ogrre",
        run="1-1",
    )
    ci.validate_plan(metadata, plan, metadata)
    with pytest.raises(ValueError):
        ci.validate_plan(metadata, b"tampered", metadata)
    for key in ("commit", "workspace", "run"):
        with pytest.raises(ValueError):
            ci.validate_plan({**metadata, key: "other"}, plan, metadata)


@pytest.mark.parametrize(
    "rules,protected",
    [
        ([], False),
        ([{"type": "required_reviewers", "reviewers": []}], False),
        ([{"type": "wait_timer", "wait_timer": 10}], False),
        (
            [
                {
                    "type": "required_reviewers",
                    "reviewers": [{"type": "Team", "reviewer": {"id": 1}}],
                }
            ],
            True,
        ),
    ],
)
def test_auto_created_environment_without_reviewers_cannot_apply(
    monkeypatch, rules, protected
):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(
        ci, "run", lambda *args: json.dumps({"protection_rules": rules})
    )
    if protected:
        ci.approval()
    else:
        with pytest.raises(ValueError, match="required reviewers"):
            ci.approval()


@pytest.mark.parametrize("returncode,success", [(0, True), (2, True), (1, False)])
def test_plan_exit_codes(monkeypatch, tmp_path, returncode, success):
    monkeypatch.setattr(ci, "configuration", lambda: ("ogrre", "gs://ci", "gs://state"))
    monkeypatch.setattr(ci, "revision", lambda: "revision")
    monkeypatch.setattr(ci, "run", lambda *args: "commit")
    monkeypatch.setattr(
        ci.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, returncode, stdout="plan output"
        ),
    )
    for key, value in {
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
    }.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "tfplan").write_bytes(b"plan")
    if success:
        ci.plan()
        assert json.loads((tmp_path / "tfplan.json").read_text())["run"] == "123-1"
    else:
        with pytest.raises(ValueError):
            ci.plan()
        assert not (tmp_path / "tfplan.json").exists()


def test_failed_apply_invalidates_previous_readiness(monkeypatch, tmp_path):
    plan = b"plan"
    expected = dict(
        commit="commit",
        revision="revision",
        workspace="ogrre",
        state="gs://state",
        sha256=hashlib.sha256(plan).hexdigest(),
        run="123-1",
    )
    (tmp_path / "tfplan").write_bytes(plan)
    (tmp_path / "tfplan.json").write_text(json.dumps(expected))
    for key, value in {
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "EXPECTED_PLAN_SHA256": expected["sha256"],
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ci, "configuration", lambda: ("ogrre", "gs://ci", "gs://state"))
    monkeypatch.setattr(ci, "revision", lambda: "revision")
    monkeypatch.setattr(ci, "assert_current", lambda: None)
    published = []

    def run(*args):
        if args[0] == "gcloud":
            published.append(json.loads(Path(args[3]).read_text()))
        return "commit"

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(ci, "run", run)
    monkeypatch.setattr(ci.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        ci.apply()
    assert [item["status"] for item in published] == ["applying"]


def test_manifest_rendering_detects_unbound_inputs_and_bad_selectors():
    template = (ROOT / "deployment/kubernetes/backend.yaml").read_text()
    assert len(manifest.validate(template)) == 6
    with pytest.raises(KeyError):
        manifest.validate(template.replace("${IMAGE}", "${MISSING_IMAGE}"))
    with pytest.raises(ValueError):
        manifest.validate(
            template.replace(
                "matchLabels:\n", "matchLabels:\n      invalid-label: unmatched\n"
            )
        )


def test_apply_and_deploy_share_a_non_cancelling_lock_and_approval_is_on_apply():
    apply = yaml.safe_load((ROOT / ".github/workflows/terraform-apply.yml").read_text())
    deploy = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-k8s-dispatch.yml").read_text()
    )
    assert apply["jobs"]["apply"]["environment"] == "terraform-apply"
    assert "environment" not in apply["jobs"]["plan"]
    for job in (apply["jobs"]["apply"], deploy["jobs"]["deploy"]):
        assert "ogrre-infrastructure-" in job["concurrency"]["group"]
        assert job["concurrency"]["cancel-in-progress"] is False


@pytest.mark.parametrize(
    "plan,needed,apply,success",
    [
        ("success", "false", "skipped", True),
        ("success", "true", "success", True),
        ("success", "true", "failure", False),
        ("success", "true", "cancelled", False),
        ("skipped", "", "skipped", False),
    ],
)
def test_reconciliation_result_cannot_convert_failure_to_success(
    plan, needed, apply, success
):
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/terraform-apply.yml").read_text()
    )
    script = workflow["jobs"]["ready"]["steps"][0]["run"]
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        env={"PLAN_RESULT": plan, "APPLY_NEEDED": needed, "APPLY_RESULT": apply},
    )
    assert (result.returncode == 0) == success
