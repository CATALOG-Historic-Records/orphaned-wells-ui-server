"""Approval and revision boundaries for cloud plans of fork contributions."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
HEAD = "a" * 40
BASE = "b" * 40
REPO = "CATALOG-Historic-Records/orphaned-wells-ui-server"


@pytest.fixture
def helper(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "deployment/ci"))
    spec = importlib.util.spec_from_file_location(
        "terraform_pr_plan", ROOT / "deployment/ci/terraform_pr_plan.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pull():
    return {
        "number": 123,
        "state": "open",
        "head": {"sha": HEAD, "ref": "feature", "repo": {"full_name": "person/fork"}},
        "base": {"sha": BASE, "ref": "main", "repo": {"full_name": REPO}},
    }


@pytest.fixture
def run():
    return {
        "id": 456,
        "run_attempt": 1,
        "head_sha": HEAD,
        "head_branch": "feature",
        "event": "pull_request",
        "path": ".github/workflows/terraform-checks.yml",
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": REPO},
        "pull_requests": [],
    }


def test_fork_is_identified_even_without_workflow_run_pr_payload(
    helper, monkeypatch, pull, run
):
    monkeypatch.setattr(helper, "api", lambda path: run)
    monkeypatch.setattr(helper, "pages", lambda path: [pull])
    checked_run = helper.source_run({"workflow_run": run})
    assert helper.find_pr(checked_run) == {
        "number": "123",
        "head_sha": HEAD,
        "base_sha": BASE,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("event", "push"),
        ("path", ".github/workflows/pretend-checks.yml"),
        ("status", "in_progress"),
        ("conclusion", "failure"),
        ("repository", {"full_name": "person/fork"}),
        ("head_sha", "c" * 40),
        ("run_attempt", 2),
    ],
)
def test_only_the_expected_successful_upstream_checks_are_accepted(
    helper, monkeypatch, run, field, value
):
    response = {**run, field: value}
    monkeypatch.setattr(helper, "api", lambda path: response)
    with pytest.raises(ValueError):
        helper.source_run({"workflow_run": run})


def test_closed_or_superseded_pr_does_not_queue_a_plan(helper, monkeypatch, pull, run):
    pull["head"]["sha"] = "c" * 40
    monkeypatch.setattr(helper, "pages", lambda path: [pull])
    assert helper.find_pr(run) is None


def test_ambiguous_pr_does_not_queue_a_plan(helper, monkeypatch, pull, run):
    second = copy.deepcopy(pull)
    second["number"] = 124
    monkeypatch.setattr(helper, "pages", lambda path: [pull, second])
    with pytest.raises(ValueError, match="Ambiguous"):
        helper.find_pr(run)


@pytest.mark.parametrize("change", ["head", "base", "closed", "target", "repository"])
def test_approval_cannot_be_used_for_a_changed_or_closed_pr(
    helper, monkeypatch, pull, change
):
    expected = helper.snapshot(pull)
    if change in ("head", "base"):
        pull[change]["sha"] = "c" * 40
    elif change == "closed":
        pull["state"] = "closed"
    elif change == "target":
        pull["base"]["ref"] = "isgs"
    else:
        pull["base"]["repo"]["full_name"] = "person/fork"
    monkeypatch.setattr(helper, "api", lambda path: pull)
    with pytest.raises(ValueError):
        helper.check_current(expected)


@pytest.mark.parametrize("self_review", [True, False])
def test_reviewers_are_required_but_self_approval_is_a_separate_setting(
    helper, monkeypatch, self_review
):
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    rule = {
        "type": "required_reviewers",
        "prevent_self_review": self_review,
        "reviewers": [],
    }
    monkeypatch.setattr(
        helper.ci, "run", lambda *args: json.dumps({"protection_rules": [rule]})
    )
    with pytest.raises(ValueError, match="terraform-plan"):
        helper.ci.approval("terraform-plan")
    rule["reviewers"] = [{"type": "User", "reviewer": {"id": 1}}]
    helper.ci.approval("terraform-plan")


def test_missing_gate_stops_preparation(helper, monkeypatch, pull, run, tmp_path):
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps({"workflow_run": run}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_file))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    monkeypatch.setattr(helper, "source_run", lambda event: run)
    monkeypatch.setattr(helper, "find_pr", lambda source: helper.snapshot(pull))
    monkeypatch.setattr(helper, "terraform_changed", lambda pr: True)
    monkeypatch.setattr(helper.ci, "output", lambda **values: None)
    monkeypatch.setattr(helper, "publish_status", lambda *args: None)

    def missing(name):
        raise ValueError("No required reviewers")

    monkeypatch.setattr(helper.ci, "approval", missing)
    with pytest.raises(ValueError, match="required reviewers"):
        helper.prepare()
    assert HEAD in (tmp_path / "summary").read_text()


def test_non_terraform_change_never_requests_approval(
    helper, monkeypatch, pull, run, tmp_path
):
    event_file = tmp_path / "event.json"
    event_file.write_text("{}")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_file))
    monkeypatch.setattr(helper, "source_run", lambda event: run)
    monkeypatch.setattr(helper, "find_pr", lambda source: helper.snapshot(pull))
    monkeypatch.setattr(helper, "terraform_changed", lambda pr: False)
    output = {}
    monkeypatch.setattr(helper.ci, "output", lambda **values: output.update(values))
    monkeypatch.setattr(
        helper.ci, "approval", lambda *args: pytest.fail("Unexpected gate")
    )
    helper.prepare()
    assert output == {"needed": "false"}


def test_changed_files_include_deleted_inputs_without_checking_out_fork_code(
    helper, monkeypatch, tmp_path
):
    monkeypatch.setattr(helper.ci, "ROOT", tmp_path)
    git = lambda *args: helper.ci.run("git", *args)
    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    terraform = tmp_path / "deployment/terraform"
    terraform.mkdir(parents=True)
    (terraform / "main.tf").write_text("# old input\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (terraform / "main.tf").unlink()
    git("add", ".")
    git("commit", "-qm", "remove input")
    head = git("rev-parse", "HEAD")
    git("update-ref", "refs/pull/123/head", head)
    git("remote", "add", "origin", str(tmp_path))
    git("checkout", "--detach", base)
    assert helper.terraform_changed(dict(number="123", head_sha=head, base_sha=base))
    assert git("rev-parse", "HEAD") == base
    assert (terraform / "main.tf").exists()


def test_candidate_must_match_approved_sha_before_plan(helper, monkeypatch, pull):
    monkeypatch.setattr(helper, "expected_pr", lambda: helper.snapshot(pull))
    monkeypatch.setattr(helper.ci, "run", lambda *args: "c" * 40)
    monkeypatch.setattr(
        helper.ci, "plan", lambda: pytest.fail("Wrong revision executed")
    )
    with pytest.raises(ValueError, match="Checkout"):
        helper.plan()


def test_plan_uses_upstream_helper_against_the_approved_candidate(
    helper, monkeypatch, pull, tmp_path
):
    monkeypatch.setattr(helper, "expected_pr", lambda: helper.snapshot(pull))
    monkeypatch.setattr(helper.ci, "ROOT", tmp_path)
    monkeypatch.setattr(helper.ci, "TF_ROOT", tmp_path / "deployment/terraform")
    monkeypatch.setattr(helper.ci, "run", lambda *args: HEAD)
    planned = []
    monkeypatch.setattr(
        helper.ci,
        "plan",
        lambda: planned.append((helper.ci.ROOT, helper.ci.TF_ROOT)),
    )
    helper.plan()
    assert planned == [
        (tmp_path / ".terraform-pr", tmp_path / ".terraform-pr/deployment/terraform")
    ]


def test_successful_plan_is_not_reported_as_current_after_a_new_push(
    helper, monkeypatch, pull
):
    monkeypatch.setattr(helper, "expected_pr", lambda: helper.snapshot(pull))
    monkeypatch.setattr(helper, "pages", lambda path: [])
    monkeypatch.setenv("PLAN_RESULT", "success")

    def stale(pr):
        raise ValueError("PR changed")

    monkeypatch.setattr(helper, "check_current", stale)
    states = []
    monkeypatch.setattr(
        helper, "publish_status", lambda pr, state, text: states.append(state)
    )
    helper.report()
    assert states == ["failure"]


@pytest.mark.parametrize(
    "result,state",
    [
        ("success", "success"),
        ("failure", "failure"),
        ("cancelled", "failure"),
        ("skipped", "failure"),
    ],
)
def test_pr_status_reflects_plan_outcome(helper, monkeypatch, pull, result, state):
    monkeypatch.setattr(helper, "expected_pr", lambda: helper.snapshot(pull))
    monkeypatch.setattr(helper, "pages", lambda path: [])
    monkeypatch.setattr(helper, "check_current", lambda pr: None)
    monkeypatch.setenv("PLAN_RESULT", result)
    statuses = []
    monkeypatch.setattr(
        helper, "publish_status", lambda pr, status, text: statuses.append(status)
    )
    helper.report()
    assert statuses == [state]


def test_old_report_cannot_overwrite_a_newer_runs_status(helper, monkeypatch, pull):
    monkeypatch.setattr(helper, "expected_pr", lambda: helper.snapshot(pull))
    monkeypatch.setattr(
        helper,
        "pages",
        lambda path: [{"context": helper.STATUS_CONTEXT, "target_url": "new"}],
    )
    monkeypatch.setattr(helper, "run_url", lambda: "old")
    monkeypatch.setattr(
        helper, "publish_status", lambda *args: pytest.fail("Overwrote newer run")
    )
    helper.report()


def test_cloud_credentials_and_candidate_execution_are_confined_to_approved_job():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/terraform-plan.yml").read_text()
    )
    checks = yaml.safe_load(
        (ROOT / ".github/workflows/terraform-checks.yml").read_text()
    )
    assert workflow[True] == {
        "workflow_run": {"workflows": ["Deployment checks"], "types": ["completed"]}
    }
    for job in checks["jobs"].values():
        assert "id-token" not in job.get("permissions", {})
        assert "uses" not in job
    for name, job in workflow["jobs"].items():
        if name == "plan":
            assert job["environment"]["name"] == "terraform-plan"
            assert job["permissions"]["id-token"] == "write"
            assert "statuses" not in job["permissions"]
        else:
            assert "id-token" not in job["permissions"]
        for step in job["steps"]:
            assert "cache" not in step.get("uses", "")
            assert "artifact" not in step.get("uses", "")
            assert ".terraform-pr/deployment/ci/" not in step.get("run", "")
    steps = workflow["jobs"]["plan"]["steps"]
    assert steps[0]["with"]["ref"] == "${{ github.sha }}"
    assert steps[1]["run"].endswith("terraform_pr_plan.py check")
    assert steps[2]["with"]["ref"] == "${{ needs.prepare.outputs.head_sha }}"
    assert steps[2]["with"]["persist-credentials"] is False
    assert steps[3]["uses"].startswith("google-github-actions/auth@")


def test_wif_pr_identity_requires_upstream_workflow_and_plan_environment():
    bootstrap = (ROOT / "deployment/ci/bootstrap_terraform_ci.sh").read_text()
    expression = next(
        line for line in bootstrap.splitlines() if line.startswith("pipeline=")
    )
    assert "assertion.workflow_ref == '$plan_workflow'" in expression
    assert "assertion.event_name == 'workflow_run'" in expression
    assert (
        "assertion.sub == 'repo:$REPOSITORY:environment:terraform-plan'" in expression
    )
    assert "assertion.event_name == 'pull_request'" not in expression
