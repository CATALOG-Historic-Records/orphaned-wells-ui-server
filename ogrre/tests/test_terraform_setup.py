"""Exercise Terraform setup offline, without cloud credentials or real state."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("value,exit_code", [("old", 0), ("new", 2)])
def test_output_only_changes_are_distinct_from_no_changes(tmp_path, value, exit_code):
    terraform = shutil.which("terraform")
    if terraform is None:
        pytest.skip("Install the pinned Terraform CLI to run this offline regression")
    (tmp_path / "main.tf").write_text(f'output "example" {{ value = "{value}" }}\n')
    state = tmp_path / "terraform.tfstate"
    state.write_text(
        json.dumps(
            {
                "version": 4,
                "terraform_version": "1.13.5",
                "serial": 1,
                "lineage": "00000000-0000-0000-0000-000000000001",
                "resources": [],
                "outputs": {
                    "example": {"value": "old", "type": "string", "sensitive": False}
                },
            }
        )
    )
    before = state.read_bytes()
    environment = {"PATH": os.defpath, "CHECKPOINT_DISABLE": "1"}
    subprocess.run(
        [terraform, f"-chdir={tmp_path}", "init", "-backend=false", "-input=false"],
        env=environment,
        check=True,
        capture_output=True,
        timeout=30,
    )
    result = subprocess.run(
        [
            terraform,
            f"-chdir={tmp_path}",
            "plan",
            "-detailed-exitcode",
            "-input=false",
            "-no-color",
            f"-out={tmp_path / 'tfplan'}",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert state.read_bytes() == before


def test_validate_mode_does_not_initialize_cloud_backend(tmp_path):
    terraform = shutil.which("terraform")
    if terraform is None:
        pytest.skip("Install the pinned Terraform CLI to run this offline regression")
    source = tmp_path / "source"
    source.mkdir()
    (source / "backend.tf").write_text(
        'terraform {\n  backend "gcs" {\n'
        '    bucket = "nonexistent-terraform-test-bucket"\n  }\n}\n'
        'resource "terraform_data" "example" { input = "static validation" }\n'
    )
    (source / ".terraform.lock.hcl").write_text("")
    environment = {
        "PATH": f"{Path(terraform).parent}:{os.defpath}",
        "HOME": str(tmp_path),
        "CHECKPOINT_DISABLE": "1",
        "TF_WORKSPACE": "ogrre",
        "TF_DIRECTORY": str(source),
        "TF_MODE": "validate",
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        # Backend initialization must not attempt to read credentials at all.
        "GOOGLE_APPLICATION_CREDENTIALS": str(tmp_path / "missing-credentials.json"),
    }
    action = yaml.safe_load(
        (ROOT / ".github/actions/setup-terraform/action.yml").read_text()
    )
    script = next(
        step["run"] for step in action["runs"]["steps"] if step.get("id") == "directory"
    )
    setup = subprocess.run(
        ["bash", "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert setup.returncode == 0, setup.stdout + setup.stderr
    validation = subprocess.run(
        [terraform, f"-chdir={source}", "validate", "-no-color"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert validation.returncode == 0, validation.stdout + validation.stderr
    assert not (source / ".terraform/terraform.tfstate").exists()
    assert not (source / "terraform.tfstate").exists()


@pytest.mark.parametrize("mode", ["remote", "output"])
def test_setup_selects_existing_workspace_and_reads_locked_state(tmp_path, mode):
    terraform = shutil.which("terraform")
    if terraform is None:
        pytest.skip("Install the pinned Terraform CLI to run this offline regression")
    platform = json.loads(
        subprocess.check_output([terraform, "version", "-json"], text=True)
    )["platform"]
    mirror = tmp_path / "mirror"
    provider_dir = mirror / "registry.terraform.io/hashicorp/null/3.2.4" / platform
    provider_dir.mkdir(parents=True)
    # init hashes/installs the provider; output must never execute it.
    provider = provider_dir / "terraform-provider-null_v3.2.4"
    provider.write_text("#!/bin/sh\nexit 1\n")
    provider.chmod(0o700)
    cli_config = tmp_path / "terraform.rc"
    cli_config.write_text(
        f'provider_installation {{\n  filesystem_mirror {{\n    path = "{mirror}"\n  }}\n}}\n'
    )
    source = tmp_path / "source"
    source.mkdir()
    states = tmp_path / "states"
    state = states / "ogrre/terraform.tfstate"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "version": 4,
                "terraform_version": "1.13.5",
                "serial": 1,
                "lineage": "00000000-0000-0000-0000-000000000001",
                "outputs": {
                    "test_output": {
                        "value": "existing-output",
                        "type": "string",
                        "sensitive": False,
                    }
                },
                "resources": [
                    {
                        "mode": "managed",
                        "type": "null_resource",
                        "name": "example",
                        "provider": 'provider["registry.terraform.io/hashicorp/null"]',
                        "instances": [
                            {
                                "schema_version": 0,
                                "attributes": {"id": "example", "triggers": None},
                            }
                        ],
                    }
                ],
            }
        )
    )
    before = state.read_bytes()
    (source / "backend.tf").write_text(
        f'terraform {{\n  backend "local" {{\n    workspace_dir = "{states}"\n  }}\n}}\n'
    )
    environment = {
        **os.environ,
        "CHECKPOINT_DISABLE": "1",
        "TF_CLI_CONFIG_FILE": str(cli_config),
        "TF_WORKSPACE": "ogrre",
        "TF_DIRECTORY": str(source),
        "TF_MODE": mode,
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
    }
    subprocess.run(
        [terraform, f"-chdir={source}", "init", "-input=false", "-no-color"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    locked = (source / ".terraform.lock.hcl").read_bytes()
    action = yaml.safe_load(
        (ROOT / ".github/actions/setup-terraform/action.yml").read_text()
    )
    script = next(
        step["run"] for step in action["runs"]["steps"] if step.get("id") == "directory"
    )
    result = subprocess.run(
        ["bash", "-c", script], env=environment, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output_dir = Path(
        (tmp_path / "github-output").read_text().strip().removeprefix("path=")
    )
    assert (output_dir / ".terraform.lock.hcl").read_bytes() == locked
    output = subprocess.check_output(
        [terraform, f"-chdir={output_dir}", "output", "-raw", "test_output"],
        env=environment,
        text=True,
    )
    assert output == "existing-output"
    assert state.read_bytes() == before
