"""Render the workload template with synthetic values and check its API contracts."""

from pathlib import Path
from string import Template

import yaml


ROOT = Path(__file__).resolve().parents[2]
VALUES = {
    "NAMESPACE": "uow-test",
    "DEPLOY_ENV": "test",
    "REPLICAS": "2",
    "DEPLOY_RUN_ID": "test-run-1",
    "RUNTIME_CONFIG_SHA": "test-config-sha",
    "IMAGE": "example.invalid/backend:test",
    "HOSTNAME": "backend.example.invalid",
    "DEPLOYED_AT": "2026-01-01T00:00:00Z",
    "CPU_REQUEST": "500m",
    "MEMORY_REQUEST": "1Gi",
    "CPU_LIMIT": "1",
    "MEMORY_LIMIT": "2Gi",
    "STATIC_IP_NAME": "test-backend-ip",
}


def validate(template):
    documents = list(yaml.safe_load_all(Template(template).substitute(VALUES)))
    expected = {
        ("apps/v1", "Deployment"),
        ("v1", "Service"),
        ("cloud.google.com/v1", "BackendConfig"),
        ("networking.gke.io/v1", "ManagedCertificate"),
        ("networking.gke.io/v1beta1", "FrontendConfig"),
        ("networking.k8s.io/v1", "Ingress"),
    }
    actual = {(document["apiVersion"], document["kind"]) for document in documents}
    if actual != expected or len(documents) != len(expected):
        raise ValueError("Unexpected workload resource types or duplicate resources")
    for document in documents:
        if document["metadata"]["namespace"] != VALUES["NAMESPACE"]:
            raise ValueError("Every workload resource must use the selected namespace")
    deployment = next(item for item in documents if item["kind"] == "Deployment")
    spec = deployment["spec"]
    if not isinstance(spec["replicas"], int) or spec["replicas"] <= 0:
        raise ValueError("Deployment replicas must render as a positive integer")
    labels = spec["template"]["metadata"]["labels"]
    if any(
        labels.get(key) != value
        for key, value in spec["selector"]["matchLabels"].items()
    ):
        raise ValueError("Deployment selector must match pod labels")
    for container in spec["template"]["spec"]["containers"]:
        for variable in container.get("env", []):
            if "value" in variable and not isinstance(variable["value"], str):
                raise ValueError("Container environment values must render as strings")
    return documents


if __name__ == "__main__":
    validate((ROOT / "deployment/kubernetes/backend.yaml").read_text())
    print(
        "Rendered and validated six workload resources, including GKE resource types."
    )
