"""Classify a complete PR diff, including removed and renamed deployment files."""

import subprocess
import sys


def classify(paths):
    terraform = any(
        (
            path.startswith("deployment/terraform/")
            and not path.endswith((".md", ".example"))
        )
        or path.startswith(".github/actions/setup-terraform/")
        or path.startswith(".github/workflows/terraform-")
        or path
        in (
            "deployment/ci/terraform_ci.py",
            "deployment/ci/terraform_pr_plan.py",
            "deployment/ci/changed_paths.py",
        )
        for path in paths
    )
    kubernetes = any(
        path.startswith(("deployment/kubernetes/", ".github/workflows/deploy-k8s-"))
        or path == "deployment/ci/validate_manifest.py"
        for path in paths
    )
    return {"terraform": terraform, "kubernetes": kubernetes}


if __name__ == "__main__":
    diff = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            f"{sys.argv[1]}...{sys.argv[2]}",
            "--",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for key, value in classify(diff.split("\0")).items():
        print(f"{key}={str(value).lower()}")
