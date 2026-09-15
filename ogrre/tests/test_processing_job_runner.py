import os
import unittest
from unittest.mock import patch

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
