"""Queue and report approved fork PR plans using only upstream workflow helpers."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from changed_paths import classify
import terraform_ci as ci


REPOSITORY = "CATALOG-Historic-Records/orphaned-wells-ui-server"
CHECKS_PATH = ".github/workflows/terraform-checks.yml"
STATUS_CONTEXT = "Terraform PR plan"


def api(path, **fields):
    args = ["gh", "api", f"repos/{REPOSITORY}/{path}"]
    if fields:
        args.extend(["--method", "POST"])
        for key, value in fields.items():
            args.extend(["--raw-field", f"{key}={value}"])
    return json.loads(ci.run(*args))


def pages(path):
    result = json.loads(
        ci.run("gh", "api", "--paginate", "--slurp", f"repos/{REPOSITORY}/{path}")
    )
    return [item for page in result for item in page]


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("Expected a full Git commit SHA")
    return value


def snapshot(pr):
    if (
        pr["state"] != "open"
        or pr["base"]["ref"] != "main"
        or pr["base"]["repo"]["full_name"] != REPOSITORY
        or not pr["head"]["repo"]
    ):
        raise ValueError("Plan requires an open PR targeting upstream main")
    number = pr["number"]
    if type(number) is not int or number <= 0:
        raise ValueError("Invalid PR number")
    return dict(
        number=str(number),
        head_sha=sha(pr["head"]["sha"]),
        base_sha=sha(pr["base"]["sha"]),
    )


def source_run(event):
    trigger = event["workflow_run"]
    run = api(f"actions/runs/{int(trigger['id'])}")
    if (
        run["repository"]["full_name"] != REPOSITORY
        or run["event"] != "pull_request"
        or run["path"].split("@", 1)[0] != CHECKS_PATH
        or run["status"] != "completed"
        or run["conclusion"] != "success"
        or run["run_attempt"] != trigger["run_attempt"]
        or sha(run["head_sha"]) != sha(trigger["head_sha"])
    ):
        raise ValueError("Expected successful deployment checks for this PR revision")
    return run


def find_pr(run):
    # workflow_run.pull_requests can be empty for forks. Use upstream PR metadata,
    # never a PR-produced artifact, to associate the checked commit with its PR.
    candidates = [
        pr
        for pr in pages("pulls?state=open&base=main&per_page=100")
        if pr["head"]["sha"] == run["head_sha"]
        and pr["head"]["ref"] == run["head_branch"]
    ]
    if not candidates:
        return None  # Closed or superseded before the checks completed.
    if len(candidates) != 1:
        raise ValueError("Ambiguous PR revision; refusing to request credentials")
    return snapshot(candidates[0])


def terraform_changed(pr):
    # Fetch objects only: no checkout, hooks, PR scripts, or Terraform before approval.
    ci.run("git", "fetch", "--no-tags", "origin", f"refs/pull/{pr['number']}/head")
    if ci.run("git", "rev-parse", "FETCH_HEAD") != pr["head_sha"]:
        raise ValueError("PR changed during preparation; wait for its new checks")
    ci.run("git", "fetch", "--no-tags", "origin", pr["base_sha"])
    paths = ci.run(
        "git",
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        f"{pr['base_sha']}...{pr['head_sha']}",
        "--",
    ).split("\0")
    return classify(paths)["terraform"]


def run_url():
    return f"https://github.com/{REPOSITORY}/actions/runs/{int(os.environ['GITHUB_RUN_ID'])}"


def publish_status(pr, state, description):
    api(
        f"statuses/{pr['head_sha']}",
        state=state,
        context=STATUS_CONTEXT,
        description=description,
        target_url=run_url(),
    )


def prepare():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    pr = find_pr(source_run(event))
    if pr is None or not terraform_changed(pr):
        ci.output(needed="false")
        return
    ci.output(needed="true", **pr)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write(
            f"### Approve Terraform planning for PR #{pr['number']}\n"
            f"Review https://github.com/{REPOSITORY}/pull/{pr['number']} before approval.\n\n"
            f"PR commit: `{pr['head_sha']}`\n\nBase commit: `{pr['base_sha']}`\n\n"
            "The terraform-plan approval authorizes cloud reads for this revision only. "
            "It does not approve the PR merge or an infrastructure apply.\n"
        )
    publish_status(pr, "pending", "Waiting for terraform-plan approval")
    ci.approval("terraform-plan")


def expected_pr():
    number = os.environ["PR_NUMBER"]
    if not re.fullmatch(r"[1-9][0-9]*", number):
        raise ValueError("Invalid PR number")
    return dict(
        number=number,
        head_sha=sha(os.environ["PR_HEAD_SHA"]),
        base_sha=sha(os.environ["PR_BASE_SHA"]),
    )


def check_current(pr):
    if snapshot(api(f"pulls/{pr['number']}")) != pr:
        raise ValueError(
            "PR or base changed; rerun deployment checks and approve a new plan"
        )


def check():
    check_current(expected_pr())
    ci.approval("terraform-plan")


def plan():
    pr = expected_pr()
    candidate = ci.ROOT / ".terraform-pr"
    if ci.run("git", "-C", str(candidate), "rev-parse", "HEAD") != pr["head_sha"]:
        raise ValueError("Checkout does not match the approved PR commit")
    # Invoke the helper from main against the reviewed configuration, never a
    # helper/action taken from the fork. This only saves a runner-local preview.
    ci.ROOT = candidate
    ci.TF_ROOT = candidate / "deployment/terraform"
    ci.plan()


def report():
    pr = expected_pr()
    previous = next(
        (
            item
            for item in pages(f"commits/{pr['head_sha']}/statuses?per_page=100")
            if item["context"] == STATUS_CONTEXT
        ),
        None,
    )
    # A cancelled older run must not overwrite a newer run's status for this SHA.
    if previous and previous["target_url"] != run_url():
        return
    success = os.environ["PLAN_RESULT"] == "success"
    try:
        check_current(pr)
    except ValueError:
        success = False
    publish_status(
        pr,
        "success" if success else "failure",
        "Plan complete; review the workflow summary"
        if success
        else "Plan rejected, failed, cancelled, or outdated; rerun deployment checks",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "check", "plan", "report"))
    args = parser.parse_args()
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise ValueError("This helper only runs in the upstream repository")
    {"prepare": prepare, "check": check, "plan": plan, "report": report}[args.command]()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
