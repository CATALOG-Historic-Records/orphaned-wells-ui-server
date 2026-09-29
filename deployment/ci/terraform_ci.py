"""Terraform CI readiness and saved-plan checks. Never download or print raw state."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
TF_ROOT = ROOT / "deployment/terraform"


def run(*args, **kwargs):
    return subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
        timeout=120,
        **kwargs,
    ).stdout.strip()


def revision(ref="HEAD"):
    """Hash tracked inputs, including module assets; omit operator documentation."""
    tree = run("git", "ls-tree", "-r", "-z", ref, "--", "deployment/terraform")
    inputs = []
    for entry in tree.split("\0"):
        if not entry:
            continue
        _, path = entry.split("\t", 1)
        if not path.endswith((".md", ".example")):
            inputs.append(entry)
    if not inputs:
        raise ValueError("No tracked Terraform inputs found")
    return hashlib.sha256("\0".join(inputs).encode()).hexdigest()


def configuration():
    workspace = os.environ.get("TF_WORKSPACE", "")
    bucket = os.environ.get("TF_CI_BUCKET", "")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", workspace):
        raise ValueError("TF_WORKSPACE must name an existing remote workspace")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]+[a-z0-9]", bucket):
        raise ValueError("TF_CI_BUCKET must be a bucket name, without gs://")
    # backend.tf deliberately uses literal values, shared by all deployment callers.
    backend = (TF_ROOT / "backend.tf").read_text()
    values = {}
    for key in ("bucket", "prefix"):
        match = re.search(rf'^\s*{key}\s*=\s*"([^"\n]+)"', backend, re.MULTILINE)
        if not match:
            raise ValueError(f"backend.tf must declare a literal {key}")
        values[key] = match[1]
    state = f"gs://{values['bucket']}/{values['prefix']}/{workspace}.tfstate"
    return workspace, f"gs://{bucket}", state


def state_generation(state):
    generation = run(
        "gcloud", "storage", "objects", "describe", state, "--format=value(generation)"
    )
    if not generation.isdigit():
        raise ValueError("Unable to identify the existing Terraform state generation")
    return generation


def read_marker(uri):
    try:
        marker = json.loads(run("gcloud", "storage", "cat", uri))
    except subprocess.CalledProcessError as error:
        # Only absence means first rollout; IAM/network errors must fail closed.
        if "404" in error.stderr or "No URLs matched" in error.stderr:
            return None
        raise
    if not isinstance(marker, dict):
        raise ValueError("Invalid Terraform readiness record")
    return marker


def is_ready(marker, expected_revision, state, generation):
    return bool(
        marker
        and marker.get("status") == "applied"
        and marker.get("revision") == expected_revision
        and marker.get("state") == state
        and marker.get("generation") == generation
    )


def output(**values):
    with open(os.environ["GITHUB_OUTPUT"], "a") as target:
        for key, value in values.items():
            target.write(f"{key}={value}\n")


def status(require_ready=False):
    workspace, bucket, state = configuration()
    expected = revision()
    ready = is_ready(
        read_marker(f"{bucket}/status/{workspace}.json"),
        expected,
        state,
        state_generation(state),
    )
    output(ready=str(ready).lower(), revision=expected)
    if require_ready and not ready:
        raise ValueError(
            "Terraform for current main is not successfully applied. "
            "Complete the gated staging workflow, then retry this deployment."
        )


def assert_current():
    run("git", "fetch", "--no-tags", "origin", "main")
    if revision() != revision("FETCH_HEAD"):
        raise ValueError("Terraform inputs changed on main; run and approve a new plan")


def approval(name="terraform-apply"):
    endpoint = f"repos/{os.environ['GITHUB_REPOSITORY']}/environments/{name}"
    environment = json.loads(run("gh", "api", endpoint))
    if not any(
        rule.get("type") == "required_reviewers" and rule.get("reviewers")
        for rule in environment.get("protection_rules", [])
    ):
        raise ValueError(
            f"Configure required reviewers on the {name} Environment before enabling CI"
        )


def plan():
    workspace, bucket, state = configuration()
    directory = Path(os.environ["RUNNER_TEMP"])
    plan_file = directory / "tfplan"
    result = subprocess.run(
        [
            "terraform",
            f"-chdir={TF_ROOT}",
            "plan",
            "-input=false",
            "-no-color",
            "-lock-timeout=5m",
            "-detailed-exitcode",
            f"-out={plan_file}",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=1800,
    )
    commit = run("git", "rev-parse", "HEAD")
    # Terraform's text output redacts sensitive values. Keep the full readable
    # plan in logs when it is too large for the summary; never print plan JSON.
    print(result.stdout)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write(
            f"### Terraform plan\nCommit: `{commit}` · Workspace: `{workspace}`\n\n"
        )
        summary.write(
            "```text\n" + result.stdout[-50000:].replace("```", "'''") + "\n```\n"
        )
        if len(result.stdout) > 50000:
            summary.write(
                "Summary truncated; review the full plan in this step's log before approval.\n"
            )
    if result.returncode not in (0, 2):
        raise ValueError("Terraform plan failed; see the workflow summary")
    metadata = {
        "commit": commit,
        "revision": revision(),
        "workspace": workspace,
        "state": state,
        "sha256": hashlib.sha256(plan_file.read_bytes()).hexdigest(),
        "run": f"{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}",
    }
    (directory / "tfplan.json").write_text(json.dumps(metadata))
    output(
        plan_uri=f"{bucket}/plans/{workspace}/{metadata['run']}",
        sha256=metadata["sha256"],
        revision=metadata["revision"],
    )


def validate_plan(metadata, plan_bytes, expected):
    if (
        metadata != expected
        or hashlib.sha256(plan_bytes).hexdigest() != expected["sha256"]
    ):
        raise ValueError(
            "Saved plan does not match the approved run, commit, or checksum"
        )


def apply():
    workspace, bucket, state = configuration()
    directory = Path(os.environ["RUNNER_TEMP"])
    plan_file = directory / "tfplan"
    expected = {
        "commit": run("git", "rev-parse", "HEAD"),
        "revision": revision(),
        "workspace": workspace,
        "state": state,
        "sha256": os.environ["EXPECTED_PLAN_SHA256"],
        "run": f"{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}",
    }
    validate_plan(
        json.loads((directory / "tfplan.json").read_text()),
        plan_file.read_bytes(),
        expected,
    )
    assert_current()
    marker_file = directory / "terraform-status.json"
    marker = {**expected, "status": "applying"}

    def publish_marker():
        marker_file.write_text(json.dumps(marker))
        run(
            "gcloud",
            "storage",
            "cp",
            str(marker_file),
            f"{bucket}/status/{workspace}.json",
        )

    # Invalidate readiness before mutation, including a forced apply of the same inputs.
    publish_marker()
    subprocess.run(
        [
            "terraform",
            f"-chdir={TF_ROOT}",
            "apply",
            "-input=false",
            "-lock-timeout=5m",
            str(plan_file),
        ],
        check=True,
        timeout=3300,
    )
    targets = json.loads(
        run(
            "terraform",
            f"-chdir={TF_ROOT}",
            "output",
            "-json",
            "kubernetes_deploy_targets",
        )
    )
    if not isinstance(targets, dict):
        raise ValueError("Terraform did not produce a deploy-target map")
    marker.update(status="applied", generation=state_generation(state))
    publish_marker()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("status", "ready", "plan", "apply", "current", "approval")
    )
    args = parser.parse_args()
    if args.command in ("status", "ready"):
        status(require_ready=args.command == "ready")
    elif args.command == "current":
        assert_current()
    else:
        {"plan": plan, "apply": apply, "approval": approval}[args.command]()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
