import os
from pathlib import Path
import unittest
from unittest.mock import patch

import yaml

from ogrre.internal.processing_job_runner import (
    _kubernetes_job_manifest,
    _processing_job_name,
)


class ProcessingJobManifestTests(unittest.TestCase):
    def test_job_names_distinguish_full_session_ids_and_retry_attempts(self):
        self.assertNotEqual(
            _processing_job_name("a" * 24 + "b" * 8),
            _processing_job_name("a" * 24 + "c" * 8),
        )
        self.assertNotEqual(
            _processing_job_name("a" * 32), _processing_job_name("a" * 32, 1)
        )

    def test_worker_manifest_uses_the_durable_job_id_and_configured_resources(self):
        job_id = "a" * 32
        with patch.dict(
            os.environ,
            {
                "PROCESSING_JOB_CPU_REQUEST": "2",
                "PROCESSING_JOB_MEMORY_REQUEST": "14Gi",
                "PROCESSING_JOB_ACTIVE_DEADLINE_SECONDS": "3600",
                "PROCESSING_JOB_TTL_SECONDS_AFTER_FINISHED": "120",
            },
            clear=False,
        ):
            manifest = _kubernetes_job_manifest(job_id, "registry.example/ogrre:sha")

        container = manifest["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(manifest["metadata"]["name"], _processing_job_name(job_id))
        self.assertEqual(container["args"], ["--job-id", job_id])
        self.assertEqual(container["resources"]["requests"]["cpu"], "2")
        self.assertEqual(container["resources"]["requests"]["memory"], "14Gi")
        self.assertEqual(
            manifest["spec"]["template"]["spec"]["serviceAccountName"],
            "processing-worker",
        )
        self.assertEqual(manifest["spec"]["activeDeadlineSeconds"], 3600)

    def test_worker_manifest_secret_and_volume_contracts_match_backend_deployment(self):
        backend_yaml_path = (
            Path(__file__).resolve().parents[2]
            / "deployment"
            / "kubernetes"
            / "backend.yaml"
        )
        docs = list(yaml.safe_load_all(backend_yaml_path.read_text()))
        deployment = next(doc for doc in docs if doc.get("kind") == "Deployment")
        api_pod_spec = deployment["spec"]["template"]["spec"]
        api_container = api_pod_spec["containers"][0]

        worker_manifest = _kubernetes_job_manifest(
            "a" * 32, "registry.example/ogrre:sha"
        )
        worker_pod_spec = worker_manifest["spec"]["template"]["spec"]
        worker_container = worker_pod_spec["containers"][0]

        self.assertEqual(
            worker_pod_spec["imagePullSecrets"],
            api_pod_spec["imagePullSecrets"],
        )
        self.assertEqual(
            worker_container["envFrom"],
            api_container["envFrom"],
        )

        api_secret_volumes = [vol for vol in api_pod_spec["volumes"] if "secret" in vol]
        worker_secret_volumes = [
            vol for vol in worker_pod_spec["volumes"] if "secret" in vol
        ]
        self.assertEqual(worker_secret_volumes, api_secret_volumes)

        secret_volume_names = {vol["name"] for vol in api_secret_volumes}
        api_secret_mounts = [
            mount
            for mount in api_container["volumeMounts"]
            if mount["name"] in secret_volume_names
        ]
        worker_secret_mounts = [
            mount
            for mount in worker_container["volumeMounts"]
            if mount["name"] in secret_volume_names
        ]
        self.assertEqual(worker_secret_mounts, api_secret_mounts)

        api_env_by_name = {
            item["name"]: item["value"] for item in api_container.get("env", [])
        }
        worker_env_by_name = {
            item["name"]: item["value"] for item in worker_container.get("env", [])
        }
        for shared_env_key in ("LOG_DIR", "LOCAL_STORAGE_ROOT"):
            self.assertEqual(
                worker_env_by_name[shared_env_key],
                api_env_by_name[shared_env_key],
            )
