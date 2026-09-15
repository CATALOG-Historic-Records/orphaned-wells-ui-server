"""Execute the deploy-target parser with synthetic inputs; no cloud access."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="Deployment parser checks require bash and jq",
)


@pytest.fixture
def parse_target(tmp_path):
    workflow_path = (
        Path(__file__).resolve().parents[2]
        / ".github/workflows/deploy-k8s-dispatch.yml"
    )
    workflow = yaml.safe_load(workflow_path.read_text())
    script = next(
        step["run"]
        for step in workflow["jobs"]["deploy"]["steps"]
        if step.get("id") == "target"
    )
    (tmp_path / "deployment/secrets").mkdir(parents=True)
    output = tmp_path / "github-output"

    def parse(environment, resources=None):
        target = {
            "cluster_name": "test-cluster",
            "cluster_location": "us-central1",
            "namespace": f"uow-{environment}",
            "host": "backend.example.com",
            "static_ip_name": "test-ip",
            "storage_bucket_name": "test-uploads",
            **(resources or {}),
        }
        subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", script],
            cwd=tmp_path,
            env={
                "PATH": os.environ["PATH"],
                "DEPLOY_ENV": environment,
                "K8S_DEPLOY_TARGETS_JSON": json.dumps({environment: target}),
                "GITHUB_OUTPUT": str(output),
            },
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return dict(line.split("=", 1) for line in output.read_text().splitlines())

    return parse


@pytest.mark.parametrize(
    "environment,replicas,api_memory,worker_cpu,worker_memory",
    [("staging", "1", "4Gi", "1", "6Gi"), ("isgs", "2", "6Gi", "1850m", "12Gi")],
)
def test_defaults_size_api_and_worker_independently(
    parse_target, environment, replicas, api_memory, worker_cpu, worker_memory
):
    target = parse_target(environment)
    assert target["replicas"] == replicas
    assert target["api_uvicorn_workers"] == "2"
    for field in ("request", "limit"):
        assert target[f"cpu_{field}"] == "1"
        assert target[f"memory_{field}"] == api_memory
        assert target[f"processing_job_cpu_{field}"] == worker_cpu
        assert target[f"processing_job_memory_{field}"] == worker_memory


@pytest.mark.parametrize("environment", ["staging", "isgs"])
@pytest.mark.parametrize(
    "cpu,memory_request,memory_limit",
    [("1", "4Gi", "4Gi"), ("500m", "1Gi", "4Gi"), ("1", "6Gi", "6Gi")],
)
def test_explicit_resources_are_preserved(
    parse_target, environment, cpu, memory_request, memory_limit
):
    resources = {
        "cpu_request": cpu,
        "cpu_limit": cpu,
        "memory_request": memory_request,
        "memory_limit": memory_limit,
        "processing_job_cpu_request": "2",
        "processing_job_cpu_limit": "2",
        "processing_job_memory_request": "14Gi",
        "processing_job_memory_limit": "14Gi",
    }
    target = parse_target(environment, resources)
    assert {key: target[key] for key in resources} == resources
