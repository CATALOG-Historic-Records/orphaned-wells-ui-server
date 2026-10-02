"""Cloud-free regressions for CI ordering, input revisions, and saved-plan integrity."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
from contextlib import nullcontext
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

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
        (["deployment/ci/bootstrap_terraform_ci.sh"], (True, False)),
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
    [
        ("HTTPError 404", True),
        ("ERROR: (gcloud.storage.cat) No URLs matched", True),
        (
            "ERROR: (gcloud.storage.cat) The following URLs matched no objects or files:\n"
            "gs://bucket/status/ogrre.json\n",
            True,
        ),
        ("403 Forbidden", False),
        ("network error", False),
        ("HTTPError 403: account-404 lacks storage.objects.get", False),
        ("HTTPError 500: Internal Server Error", False),
        (None, False),
        ("", False),
    ],
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
        with pytest.raises(
            ValueError, match="Unable to read Terraform readiness record"
        ) as error:
            ci.read_marker("gs://bucket/status/ogrre.json")
        assert "gs://bucket/status/ogrre.json" in str(error.value)
        if error_text:
            assert error_text in str(error.value)


@pytest.mark.parametrize("require_ready", [False, True])
def test_first_rollout_requests_reconciliation_but_blocks_deployment(
    monkeypatch, tmp_path, require_ready
):
    marker_uri = "gs://bucket/status/ogrre.json"
    monkeypatch.setattr(
        ci, "configuration", lambda: ("ogrre", "gs://bucket", "gs://state")
    )
    monkeypatch.setattr(ci, "revision", lambda: "current")
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    def run(*args):
        if args == ("gcloud", "storage", "cat", marker_uri):
            raise subprocess.CalledProcessError(
                1,
                args,
                stderr="ERROR: (gcloud.storage.cat) The following URLs matched no objects or files:\n"
                f"{marker_uri}\n",
            )
        assert args == (
            "gcloud",
            "storage",
            "objects",
            "describe",
            "gs://state",
            "--format=value(generation)",
        )
        return "42"

    monkeypatch.setattr(ci, "run", run)
    if require_ready:
        with pytest.raises(
            ValueError, match="Complete the staging infrastructure workflow"
        ):
            ci.status(require_ready=True)
    else:
        ci.status()
    assert output.read_text() == "ready=false\nrevision=current\n"


@pytest.mark.parametrize("content", ["not JSON", "[]", "null"])
def test_invalid_marker_content_still_blocks_readiness(monkeypatch, content):
    monkeypatch.setattr(ci, "run", lambda *args: content)
    with pytest.raises(ValueError):
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


@pytest.fixture
def upload_files(monkeypatch, tmp_path):
    monkeypatch.setattr(ci, "configuration", lambda: ("ogrre", "gs://ci", "gs://state"))
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))

    def token(*args):
        assert args == ("gcloud", "auth", "print-access-token")
        return "test-token"

    monkeypatch.setattr(ci, "run", token)
    files = {"tfplan": b"private-plan-bytes", "tfplan.json": b'{"run":"123-2"}'}
    for name, content in files.items():
        (tmp_path / name).write_bytes(content)
    return files


def test_plan_upload_uses_create_only_requests_and_cannot_overwrite(
    monkeypatch, upload_files, capsys
):
    objects = {}

    def insert(request, timeout):
        assert request.get_method() == "POST"  # No destination GET or LIST.
        url = urlsplit(request.full_url)
        assert (url.scheme, url.netloc, url.path) == (
            "https",
            "storage.googleapis.com",
            "/upload/storage/v1/b/ci/o",
        )
        query = parse_qs(url.query)
        assert query["uploadType"] == ["media"]
        assert query["ifGenerationMatch"] == ["0"]
        assert request.get_header("Authorization") == "Bearer test-token"
        assert timeout == 120
        name = query["name"][0]
        if name in objects:
            raise HTTPError(request.full_url, 412, "Precondition Failed", {}, None)
        objects[name] = request.data
        return nullcontext()

    monkeypatch.setattr(ci, "urlopen", insert)
    ci.upload()
    expected = {
        f"plans/ogrre/123-2/{name}": data for name, data in upload_files.items()
    }
    assert objects == expected
    with pytest.raises(ValueError, match="HTTP 412.*new full run"):
        ci.upload()
    assert objects == expected
    assert capsys.readouterr() == ("", "")


def test_failed_plan_upload_stops_before_publishing_metadata(
    monkeypatch, upload_files, capsys
):
    calls = []

    def forbidden(request, **kwargs):
        calls.append(request)
        raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(ci, "urlopen", forbidden)
    with pytest.raises(ValueError, match="Unable to upload tfplan: HTTP 403"):
        ci.upload()
    assert len(calls) == 1
    assert capsys.readouterr() == ("", "")


def test_only_main_publisher_uploads_with_direct_federation():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/terraform-apply.yml").read_text()
    )
    steps = workflow["jobs"]["plan"]["steps"]
    upload_index = next(
        i
        for i, step in enumerate(steps)
        if step.get("run") == "python3 deployment/ci/terraform_ci.py upload"
    )
    publisher = steps[upload_index - 1]
    assert publisher["uses"].startswith("google-github-actions/auth@")
    assert "service_account" not in publisher["with"]
    assert (
        publisher["with"]["workload_identity_provider"] == "${{ vars.TF_WIF_PROVIDER }}"
    )
    pr_workflow = (ROOT / ".github/workflows/terraform-checks.yml").read_text()
    assert "terraform_ci.py upload" not in pr_workflow


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
    monkeypatch.setattr(ci, "state_generation", lambda state: "42")
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
        metadata = json.loads((tmp_path / "tfplan.json").read_text())
        assert metadata["run"] == "123-1"
        assert metadata["generation"] == "42"
        assert metadata["has_changes"] is (returncode == 2)
        assert (
            f"has_changes={str(returncode == 2).lower()}\n"
            in (tmp_path / "output").read_text()
        )
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
        generation="42",
        has_changes=True,
    )
    (tmp_path / "tfplan").write_bytes(plan)
    (tmp_path / "tfplan.json").write_text(json.dumps(expected))
    for key, value in {
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "EXPECTED_PLAN_SHA256": expected["sha256"],
        "EXPECTED_STATE_GENERATION": "42",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ci, "configuration", lambda: ("ogrre", "gs://ci", "gs://state"))
    monkeypatch.setattr(ci, "revision", lambda: "revision")
    monkeypatch.setattr(ci, "assert_current", lambda: None)
    monkeypatch.setattr(ci, "state_generation", lambda state: "42")
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


def test_completion_and_deploy_share_a_lock_and_only_changes_request_approval():
    apply = yaml.safe_load((ROOT / ".github/workflows/terraform-apply.yml").read_text())
    deploy = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-k8s-dispatch.yml").read_text()
    )
    assert apply["jobs"]["apply"]["environment"] == "terraform-apply"
    assert "environment" not in apply["jobs"]["plan"]
    assert "environment" not in apply["jobs"]["noop"]
    assert "needs.plan.outputs.has_changes == 'true'" in apply["jobs"]["apply"]["if"]
    assert "needs.plan.outputs.has_changes == 'false'" in apply["jobs"]["noop"]["if"]
    for job in (
        apply["jobs"]["apply"],
        apply["jobs"]["noop"],
        deploy["jobs"]["deploy"],
    ):
        assert "ogrre-infrastructure-" in job["concurrency"]["group"]
        assert job["concurrency"]["cancel-in-progress"] is False


@pytest.mark.parametrize(
    "plan,needed,changes,apply,noop,success",
    [
        ("success", "false", "", "skipped", "skipped", True),
        ("success", "true", "true", "success", "skipped", True),
        ("success", "true", "false", "skipped", "success", True),
        ("success", "true", "true", "failure", "skipped", False),
        ("success", "true", "true", "cancelled", "skipped", False),
        ("success", "true", "false", "skipped", "failure", False),
        ("success", "true", "false", "skipped", "cancelled", False),
        ("success", "true", "false", "skipped", "skipped", False),
        ("success", "true", "", "skipped", "success", False),
        ("success", "false", "true", "skipped", "skipped", False),
        ("success", "true", "true", "skipped", "success", False),
        ("success", "true", "false", "success", "success", False),
        ("skipped", "", "", "skipped", "skipped", False),
        ("failure", "true", "false", "skipped", "skipped", False),
    ],
)
def test_reconciliation_result_cannot_convert_failure_to_success(
    plan, needed, changes, apply, noop, success
):
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/terraform-apply.yml").read_text()
    )
    script = workflow["jobs"]["ready"]["steps"][0]["run"]
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        env={
            "PLAN_RESULT": plan,
            "RECONCILIATION_NEEDED": needed,
            "HAS_CHANGES": changes,
            "APPLY_RESULT": apply,
            "NOOP_RESULT": noop,
        },
    )
    assert (result.returncode == 0) == success
