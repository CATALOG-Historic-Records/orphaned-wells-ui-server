from unittest.mock import Mock, patch

import pytest
from google.cloud import documentai

from ogrre.internal import batch_document_processing as batch
from ogrre.internal import directory_upload, storage_api
from ogrre.tests.test_directory_upload import GROUP, USER, manifest


def finalize(manager, request=None):
    session = manager.createDirectoryUpload(GROUP, USER, request or manifest())
    with patch.object(
        storage_api,
        "verify_directory_upload",
        side_effect=lambda bucket, item: {**item, "generation": "123"},
    ):
        return manager.finalizeDirectoryUpload(session, USER)


def test_finalization_creates_500_pending_records_without_processing_bytes(manager):
    with patch.object(batch, "_prepare_document_for_batch") as prepare, patch.object(
        manager.db.records, "bulk_write", wraps=manager.db.records.bulk_write
    ) as write:
        job = finalize(manager, manifest(500))
    assert job["status"] == "queued"
    assert manager.db.records.count_documents({"status": "queued"}) == 500
    assert write.call_count == 1
    assert manager.db.history.count_documents({"action": "createRecord"}) == 500
    prepare.assert_not_called()
    record = manager.db.records.find_one()
    assert record["image_files"] == []
    assert record["filename"] == ""
    assert record["attributesList"] == []
    assert record["processing_job_id"] == job["job_id"]


def test_repeated_finalization_preserves_record_ids_numbers_and_user_edits(manager):
    job = finalize(manager)
    record = manager.db.records.find_one()
    manager.db.records.update_one(
        {"_id": record["_id"]}, {"$set": {"name": "Edited name", "notes": "Keep me"}}
    )
    assert finalize(manager)["job_id"] == job["job_id"]
    assert manager.db.records.count_documents({}) == 2
    updated = manager.db.records.find_one({"_id": record["_id"]})
    assert updated["record_number"] == record["record_number"]
    assert updated["name"] == "Edited name"
    assert updated["notes"] == "Keep me"
    assert manager.db.history.count_documents({"action": "createRecord"}) == 2


def test_duplicate_decisions_exclude_existing_records_and_repeated_names(manager):
    manager.db.records.insert_one({"record_group_id": GROUP, "name": "well-0"})
    request = manifest(3)
    request["files"][2] = {
        "name": "well-1.pdf",
        "relative_path": "other/well-1.pdf",
        "size": 100,
    }
    job = finalize(manager, request)
    assert [item["skip_duplicate"] for item in job["input"]["documents"]] == [
        True,
        False,
        True,
    ]
    assert manager.db.records.count_documents({"processing_job_id": job["job_id"]}) == 1


def test_interrupted_record_initialization_can_finish_on_retry(manager):
    original = manager.db.records.bulk_write

    def partial_write(operations, **kwargs):
        original(operations[:1], **kwargs)
        raise RuntimeError("connection interrupted")

    with patch.object(manager.db.records, "bulk_write", side_effect=partial_write):
        with pytest.raises(RuntimeError):
            finalize(manager)
    first = manager.db.records.find_one()
    job = finalize(manager)
    assert job["input"]["records_initialized"]
    assert manager.db.records.count_documents({}) == 2
    assert (
        manager.db.records.find_one({"_id": first["_id"]})["record_number"]
        == first["record_number"]
    )
    assert manager.db.history.count_documents({"action": "createRecord"}) == 2


def test_worker_uses_pending_record_and_marks_conversion_failure_on_that_record(
    manager,
):
    job = finalize(manager, manifest(1))
    item = job["input"]["documents"][0]
    uri = f"gs://{job['input']['bucket_name']}/{item['object_name']}"
    record_id = directory_upload.processing_record_id(job["job_id"], uri)
    manager.updateRecord = Mock()
    document = documentai.GcsDocument(gcs_uri=uri, mime_type="application/pdf")
    blob = Mock()
    blob.download_as_bytes.side_effect = ValueError("Invalid PDF")
    with patch.object(
        storage_api,
        "_get_bucket",
        return_value=(None, Mock(blob=Mock(return_value=blob))),
    ):
        with pytest.raises(ValueError, match="Invalid PDF"):
            batch._prepare_document_for_batch(
                document, GROUP, USER, manager, job["job_id"], manifest_item=item
            )
    assert manager.updateRecord.call_args.args[0] == record_id
    assert manager.updateRecord.call_args.args[1]["status"] == "error"
    assert manager.db.records.count_documents({}) == 1


def test_worker_preparation_preserves_pending_record_metadata(manager):
    job = finalize(manager, manifest(1))
    record = manager.db.records.find_one()
    manager.db.records.update_one(
        {"_id": record["_id"]},
        {"$set": {"name": "Edited", "review_status": "incomplete"}},
    )
    manager.prepareProcessingRecord(
        str(record["_id"]),
        {
            "filename": "well-0.png",
            "image_files": ["well-0.png"],
            "processing_job_id": job["job_id"],
            "processing_attempt": 0,
        },
        USER,
    )
    updated = manager.db.records.find_one({"_id": record["_id"]})
    assert updated["status"] == "processing"
    assert updated["filename"] == "well-0.png"
    assert updated["record_number"] == record["record_number"]
    assert updated["name"] == "Edited"
    assert updated["review_status"] == "incomplete"


def test_failed_job_marks_queued_records_and_retry_preserves_successes(manager):
    job = finalize(manager)
    first = manager.db.records.find_one()
    manager.db.records.update_one(
        {"_id": first["_id"]}, {"$set": {"status": "digitized"}}
    )
    manager.completeProcessingJob(job["job_id"], "error", "Worker failed", attempt=0)
    assert manager.db.records.count_documents({"status": "error"}) == 1
    manager.retryProcessingJob(job["job_id"])
    assert (
        manager.db.records.count_documents(
            {"status": "queued", "processing_attempt": 1}
        )
        == 1
    )
    assert manager.db.records.find_one({"_id": first["_id"]})["status"] == "digitized"


def test_active_job_detection_includes_jobs_outside_recent_history(manager):
    first = manager.createBatchProcessingJob(GROUP, USER, "bucket")
    for _ in range(11):
        job = manager.createBatchProcessingJob(GROUP, USER, "bucket")
        manager.completeProcessingJob(job["job_id"], "completed")
    assert first["job_id"] not in [
        job["job_id"] for job in manager.listProcessingJobs(GROUP)
    ]
    assert manager.hasActiveProcessingJobs(GROUP)
    assert not manager.hasActiveProcessingJobs("other-group")


def test_worker_does_not_recreate_a_pending_record_deleted_before_processing(manager):
    job = finalize(manager, manifest(1))
    item = job["input"]["documents"][0]
    uri = f"gs://{job['input']['bucket_name']}/{item['object_name']}"
    document = documentai.GcsDocument(gcs_uri=uri, mime_type="application/pdf")
    manager.db.records.delete_many({})
    with patch.object(batch, "_prepare_document_for_batch") as prepare:
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
    prepare.assert_not_called()
    assert result["total_failed"] == 1
    assert manager.db.records.count_documents({}) == 0
