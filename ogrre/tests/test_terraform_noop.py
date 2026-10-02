"""Automatic no-change completion must never apply or certify a stale plan."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "terraform_ci_noop", ROOT / "deployment/ci/terraform_ci.py"
)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


@pytest.fixture
def completion(monkeypatch, tmp_path):
    plan = b"saved no-change plan"
    metadata = dict(
        commit="commit",
        revision="revision",
        workspace="ogrre",
        state="gs://state",
        sha256=hashlib.sha256(plan).hexdigest(),
        run="123-1",
        generation="42",
        has_changes=False,
    )
    (tmp_path / "tfplan").write_bytes(plan)
    (tmp_path / "tfplan.json").write_text(json.dumps(metadata))
    for key, value in {
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "EXPECTED_PLAN_SHA256": metadata["sha256"],
        "EXPECTED_STATE_GENERATION": "42",
        "TF_OUTPUT_DIRECTORY": str(tmp_path / "outputs"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ci, "configuration", lambda: ("ogrre", "gs://ci", "gs://state"))
    monkeypatch.setattr(ci, "revision", lambda: "revision")
    monkeypatch.setattr(ci, "assert_current", lambda: None)
    monkeypatch.setattr(ci, "state_generation", lambda state: "42")
    published = []

    def run(*args):
        if args == ("git", "rev-parse", "HEAD"):
            return "commit"
        if args == (
            "terraform",
            f"-chdir={tmp_path / 'outputs'}",
            "output",
            "-json",
            "kubernetes_deploy_targets",
        ):
            return json.dumps({"staging": {"namespace": "uow-staging"}})
        if args == (
            "gcloud",
            "storage",
            "cp",
            str(tmp_path / "terraform-status.json"),
            "gs://ci/status/ogrre.json",
        ):
            published.append(
                json.loads((tmp_path / "terraform-status.json").read_text())
            )
            return ""
        pytest.fail(f"Unexpected command during no-op completion: {args}")

    def unexpected(*args, **kwargs):
        pytest.fail("No-op completion must not invoke Terraform apply")

    monkeypatch.setattr(ci, "run", run)
    monkeypatch.setattr(ci.subprocess, "run", unexpected)
    return tmp_path, metadata, published


def test_noop_records_verified_state_without_applying(completion):
    directory, metadata, published = completion
    ci.complete_noop()
    assert published == [{**metadata, "status": "verified"}]
    assert ci.is_ready(published[0], "revision", "gs://state", "42")
    assert not ci.is_ready(published[0], "revision", "gs://state", "43")
    assert not ci.is_ready(
        {**published[0], "has_changes": True}, "revision", "gs://state", "42"
    )
    assert "apply and approval were skipped" in (directory / "summary").read_text()


@pytest.mark.parametrize(
    "field,value",
    [
        ("has_changes", True),
        ("has_changes", "false"),
        ("has_changes", 0),
        ("commit", "old"),
        ("revision", "old"),
        ("workspace", "other"),
        ("state", "gs://other"),
        ("run", "123-2"),
        ("generation", "41"),
    ],
)
def test_noop_rejects_wrong_outcome_or_metadata(completion, field, value):
    directory, metadata, published = completion
    (directory / "tfplan.json").write_text(json.dumps({**metadata, field: value}))
    with pytest.raises(ValueError):
        ci.complete_noop()
    assert published == []


def test_noop_rejects_tampered_plan(completion):
    directory, _, published = completion
    (directory / "tfplan").write_bytes(b"different plan")
    with pytest.raises(ValueError, match="checksum"):
        ci.complete_noop()
    assert published == []


@pytest.mark.parametrize(
    "generations,publishes",
    [(["43"], False), (["42", "43"], False), (["42", "42", "43"], True)],
)
def test_noop_rejects_state_changes_before_or_during_completion(
    completion, monkeypatch, generations, publishes
):
    _, _, published = completion
    observed = iter(generations)
    monkeypatch.setattr(ci, "state_generation", lambda state: next(observed))
    with pytest.raises(ValueError, match="state changed after planning"):
        ci.complete_noop()
    assert bool(published) is publishes
    if published:
        assert not ci.is_ready(published[0], "revision", "gs://state", "43")


@pytest.mark.parametrize("check", [1, 2])
def test_noop_rejects_superseded_inputs(completion, monkeypatch, check):
    _, _, published = completion
    checks = 0

    def current():
        nonlocal checks
        checks += 1
        if checks == check:
            raise ValueError("Terraform inputs changed on main")

    monkeypatch.setattr(ci, "assert_current", current)
    with pytest.raises(ValueError, match="inputs changed"):
        ci.complete_noop()
    assert published == []


@pytest.mark.parametrize("failure", ["output", "invalid-output", "publish"])
def test_noop_does_not_report_success_on_output_or_marker_failures(
    completion, monkeypatch, failure
):
    directory, _, published = completion
    original = ci.run

    def run(*args):
        if args[0] == "terraform":
            if failure == "invalid-output":
                return "null"
            if failure == "output":
                raise subprocess.CalledProcessError(1, args)
        if args[:3] == ("gcloud", "storage", "cp") and failure == "publish":
            raise subprocess.CalledProcessError(1, args)
        return original(*args)

    monkeypatch.setattr(ci, "run", run)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        ci.complete_noop()
    assert published == []
    assert not (directory / "summary").exists()


def test_noop_cannot_request_apply_account_or_environment():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/terraform-apply.yml").read_text()
    )
    noop = workflow["jobs"]["noop"]
    assert "environment" not in noop
    auth = next(
        step
        for step in noop["steps"]
        if step.get("uses", "").startswith("google-github-actions/auth@")
    )
    assert auth["with"]["service_account"] == "${{ vars.TF_PLAN_SERVICE_ACCOUNT }}"
    setup = next(
        step
        for step in noop["steps"]
        if step.get("uses") == "./.github/actions/setup-terraform"
    )
    assert setup["with"]["mode"] == "output"
    assert "terraform_ci.py apply" not in json.dumps(noop)


@pytest.mark.parametrize("exit_code", [0, 2])
def test_state_changes_during_planning_never_produce_a_completable_plan(
    tmp_path, monkeypatch, exit_code
):
    monkeypatch.setattr(ci, "configuration", lambda: ("ogrre", "gs://ci", "gs://state"))
    observed = iter(["42", "43"])
    monkeypatch.setattr(ci, "state_generation", lambda state: next(observed))
    monkeypatch.setattr(ci, "run", lambda *args: "commit")
    monkeypatch.setattr(
        ci.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, exit_code, stdout="plan output"
        ),
    )
    for key, value in {
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
    }.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="state changed during planning"):
        ci.plan()
    assert not (tmp_path / "output").exists()
    assert not (tmp_path / "tfplan.json").exists()


def test_changed_plan_still_applies_and_records_new_state_generation(
    completion, monkeypatch
):
    directory, metadata, published = completion
    metadata["has_changes"] = True
    (directory / "tfplan.json").write_text(json.dumps(metadata))
    generations = iter(["42", "43"])
    monkeypatch.setattr(ci, "state_generation", lambda state: next(generations))
    original = ci.run
    applied = []

    def run(*args):
        if args[0] == "terraform":
            assert args[2:] == ("output", "-json", "kubernetes_deploy_targets")
            return "{}"
        return original(*args)

    monkeypatch.setattr(ci, "run", run)
    monkeypatch.setattr(
        ci.subprocess, "run", lambda args, **kwargs: applied.append(args)
    )
    ci.apply()
    assert len(applied) == 1
    assert applied[0][2] == "apply"
    assert applied[0][-1] == str(directory / "tfplan")
    assert [marker["status"] for marker in published] == ["applying", "applied"]
    assert published[-1]["generation"] == "43"
    assert ci.is_ready(published[-1], "revision", "gs://state", "43")


def test_fork_pushes_skip_staging_and_reusable_readiness():
    staging = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-k8s-staging.yml").read_text()
    )
    reconcile = yaml.safe_load(
        (ROOT / ".github/workflows/terraform-apply.yml").read_text()
    )
    repository_guard = (
        "github.repository == 'CATALOG-Historic-Records/orphaned-wells-ui-server'"
    )
    assert repository_guard in staging["jobs"]["check_ref"]["if"]
    assert repository_guard in staging["jobs"]["infrastructure"]["if"]
    ready_condition = reconcile["jobs"]["ready"]["if"]
    assert "always()" in ready_condition  # Upstream failures must still fail readiness.
    assert repository_guard in ready_condition
    assert "github.ref == 'refs/heads/main'" in ready_condition
    assert "vars.ENABLE_TERRAFORM_CI == 'true'" in ready_condition
