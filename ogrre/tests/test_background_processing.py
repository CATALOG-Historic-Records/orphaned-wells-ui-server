import asyncio
from unittest.mock import patch

import pytest
from fastapi import BackgroundTasks
from pymongo.errors import AutoReconnect, ConfigurationError

from ogrre.internal import batch_document_processing as batch
from ogrre.internal import processing_job_runner as runner
from ogrre.tests.test_directory_upload import GROUP, USER


@pytest.fixture(autouse=True)
def local_jobs(monkeypatch):
    monkeypatch.setenv("PROCESSING_JOB_MODE", "background")
    runner._background_active.clear()
    runner._background_failures.clear()
    yield
    runner._background_active.clear()
    runner._background_failures.clear()


def test_local_worker_reuses_api_manager_without_opening_a_new_mongo_client(manager):
    job = manager.createBatchProcessingJob(GROUP, USER, "bucket")
    tasks = BackgroundTasks()
    runner.dispatch_batch_processing_job(job["job_id"], manager, tasks)
    assert not runner.processing_worker_has_stopped(
        manager.getProcessingJob(job["job_id"])
    )
    with patch(
        "ogrre.internal.data_manager.DataManager",
        side_effect=ConfigurationError("DNS timed out"),
    ) as create, patch.object(batch, "_process_batch_documents"):
        asyncio.run(tasks())
    create.assert_not_called()
    assert manager.getProcessingJob(job["job_id"])["status"] == "completed"
    assert runner.processing_worker_has_stopped(manager.getProcessingJob(job["job_id"]))


def test_exception_before_job_claim_records_failure_instead_of_leaving_dispatched(
    manager,
):
    job = manager.createBatchProcessingJob(GROUP, USER, "bucket")
    tasks = BackgroundTasks()
    runner.dispatch_batch_processing_job(job["job_id"], manager, tasks)
    with patch(
        "ogrre.processing_worker.run_processing_job",
        side_effect=ConfigurationError("DNS timed out"),
    ):
        asyncio.run(tasks())
    failed = manager.getProcessingJob(job["job_id"])
    assert failed["status"] == "error"
    assert "ConfigurationError" in failed["error"]
    assert failed["started_at"] is None


def test_terminal_failure_is_saved_when_mongodb_recovers_without_rerunning_work(
    manager,
):
    job = manager.createBatchProcessingJob(GROUP, USER, "bucket")
    tasks = BackgroundTasks()
    runner.dispatch_batch_processing_job(job["job_id"], manager, tasks)
    with patch(
        "ogrre.processing_worker.run_processing_job",
        side_effect=AutoReconnect("Connection lost"),
    ) as run, patch.object(
        manager, "completeProcessingJob", side_effect=AutoReconnect("Still offline")
    ):
        asyncio.run(tasks())
    assert manager.getProcessingJob(job["job_id"])["status"] == "dispatched"
    runner.maintain_processing_jobs(manager)
    assert manager.getProcessingJob(job["job_id"])["status"] == "error"
    run.assert_called_once()
    assert runner._background_failures == {}
