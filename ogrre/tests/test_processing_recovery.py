from types import SimpleNamespace
from unittest.mock import Mock, patch

from google.cloud import documentai
from kubernetes.client.exceptions import ApiException

from ogrre.internal import batch_document_processing as batch
from ogrre.internal import processing_job_runner as runner
from ogrre.tests.test_directory_upload import GROUP, USER


def test_maintenance_releases_failed_worker_and_dispatches_waiting_job(
    manager, monkeypatch
):
    monkeypatch.setenv("PROCESSING_JOB_MODE", "kubernetes")
    monkeypatch.setenv("PROCESSING_JOB_NAMESPACE", "test")
    monkeypatch.setenv("PROCESSING_JOB_IMAGE", "example/image:sha")
    monkeypatch.setenv("PROCESSING_JOB_MAX_ACTIVE", "1")
    first = manager.createBatchProcessingJob(GROUP, USER, "bucket")
    second = manager.createBatchProcessingJob(GROUP, USER, "bucket")
    api = Mock()
    api.read_namespaced_job.return_value = SimpleNamespace(
        status=SimpleNamespace(
            conditions=[
                SimpleNamespace(
                    type="Failed",
                    status="True",
                    reason="OOMKilled",
                    message="Worker failed",
                )
            ]
        )
    )
    with patch("kubernetes.config.load_incluster_config"), patch(
        "kubernetes.client.BatchV1Api", return_value=api
    ):
        runner.dispatch_batch_processing_job(first["job_id"], manager)
        assert (
            runner.dispatch_batch_processing_job(second["job_id"], manager)["status"]
            == "queued"
        )
        runner.maintain_processing_jobs(manager)
    assert manager.getProcessingJob(first["job_id"])["status"] == "error"
    assert manager.getProcessingJob(second["job_id"])["status"] == "dispatched"
    assert api.create_namespaced_job.call_count == 2


def test_missing_dispatched_job_is_recreated_with_the_same_name(manager, monkeypatch):
    monkeypatch.setenv("PROCESSING_JOB_MODE", "kubernetes")
    monkeypatch.setenv("PROCESSING_JOB_NAMESPACE", "test")
    monkeypatch.setenv("PROCESSING_JOB_IMAGE", "example/image:sha")
    job = manager.createBatchProcessingJob(GROUP, USER, "bucket")
    api = Mock()
    api.read_namespaced_job.side_effect = ApiException(status=404)
    with patch("kubernetes.config.load_incluster_config"), patch(
        "kubernetes.client.BatchV1Api", return_value=api
    ):
        runner.dispatch_batch_processing_job(job["job_id"], manager)
        runner.reconcile_processing_job(job["job_id"], manager)
    calls = api.create_namespaced_job.call_args_list
    assert len(calls) == 2
    assert (
        calls[0].kwargs["body"]["metadata"]["name"]
        == calls[1].kwargs["body"]["metadata"]["name"]
    )


def test_large_queue_does_not_hide_running_jobs(manager):
    for _ in range(101):
        manager.createBatchProcessingJob(GROUP, USER, "bucket")
    job = manager.createBatchProcessingJob(GROUP, USER, "bucket")
    manager.beginProcessingJobDispatch(job["job_id"], {})
    manager.claimProcessingJob(job["job_id"])
    assert manager.getActiveProcessingJobs()[0]["job_id"] == job["job_id"]


def test_retry_reattaches_recorded_operation_instead_of_submitting_again(manager):
    source = "gs://bucket/well.pdf"
    job = manager.createBatchProcessingJob(GROUP, USER, "bucket")
    manager.updateProcessingJob(
        job["job_id"],
        {"operations.0": {"name": "operations/existing", "consumed": False}},
    )
    document = documentai.GcsDocument(gcs_uri=source, mime_type="application/pdf")
    prepared = batch.PreparedDocument(source, "c" * 24, "well.png", document)
    operation = Mock()
    operation.metadata = SimpleNamespace(
        state=documentai.BatchProcessMetadata.State.SUCCEEDED,
        individual_process_statuses=[
            SimpleNamespace(
                input_gcs_source=source,
                output_gcs_destination="gs://output/result/",
                status=SimpleNamespace(code=0),
            )
        ],
    )
    with patch.object(
        batch, "_prepare_document_for_batch", return_value=prepared
    ), patch.object(
        batch.document_ai_api, "get_batch_operation", return_value=operation
    ) as resume, patch.object(
        batch.document_ai_api, "batch_process_documents"
    ) as submit, patch.object(
        batch, "_read_output_attributes", return_value=[]
    ), patch.object(
        batch, "_update_record_with_attributes"
    ):
        result = batch._process_one_batch(
            job["job_id"],
            0,
            documentai.BatchDocumentsInputConfig(
                gcs_documents=documentai.GcsDocuments(documents=[document])
            ),
            GROUP,
            USER,
            manager,
            "processor",
            "model",
            [],
            "output",
            "results/",
        )
    resume.assert_called_once_with("operations/existing")
    submit.assert_not_called()
    assert result["total_succeeded"] == 1
    assert (
        manager.getProcessingJob(job["job_id"])["operations"]["0"]["consumed"] is True
    )
