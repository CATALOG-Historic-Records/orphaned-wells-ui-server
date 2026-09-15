from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from bson import ObjectId
from google.api_core.exceptions import NotFound

from ogrre.internal import directory_upload, storage_api

USER = {"email": "uploader@example.com"}
GROUP = "a" * 24


def manifest(count=2):
    return {
        "session_id": "b" * 32,
        "files": [
            {
                "name": f"well-{index}.pdf",
                "relative_path": f"directory/well-{index}.pdf",
                "size": 100,
            }
            for index in range(count)
        ],
    }


def test_manifest_preserves_paths_and_supports_500_files():
    session_id, files, options = directory_upload.validate_manifest(manifest(500))
    assert len(files) == 500
    assert files[0]["object_name"] == f"directory_uploads/{session_id}/0/well-0.pdf"
    assert files[0]["relative_path"] == "directory/well-0.pdf"
    assert options["prevent_duplicates"] is True


@pytest.mark.parametrize(
    "update",
    [
        {"relative_path": "../well-0.pdf"},
        {"name": "../well-0.pdf"},
        {"relative_path": "/well-0.pdf"},
        {"name": "well#1.pdf", "relative_path": "well#1.pdf"},
        {"size": True},
        {"size": 0},
        {"size": 10**12},
        {"name": "script.exe", "relative_path": "script.exe"},
    ],
)
def test_rejects_invalid_files(update):
    request = manifest()
    request["files"][0].update(update)
    with pytest.raises(ValueError):
        directory_upload.validate_manifest(request)


def test_rejects_oversized_directory_and_nonboolean_options(monkeypatch):
    monkeypatch.setenv("DIRECTORY_UPLOAD_MAX_BYTES", "100")
    with pytest.raises(ValueError, match="size limit"):
        directory_upload.validate_manifest(manifest())
    request = manifest(1)
    request["prevent_duplicates"] = "false"
    with pytest.raises(ValueError, match="boolean"):
        directory_upload.validate_manifest(request)


def test_session_creation_is_idempotent_and_scoped(manager):
    session = manager.createDirectoryUpload(GROUP, USER, manifest())
    assert manager.createDirectoryUpload(GROUP, USER, manifest()) == session
    assert manager.db.directory_uploads.count_documents({}) == 1
    assert manager.recordHistory.call_count == 1
    assert manager.getDirectoryUpload(session["_id"], GROUP, {"email": "other"}) is None
    with pytest.raises(ValueError):
        manager.createDirectoryUpload(GROUP, USER, manifest(1))


def test_cannot_reuse_another_job_id(manager):
    manager.createBatchProcessingJob(
        "other-group", {"email": "other"}, "bucket", job_id="b" * 32
    )
    with pytest.raises(ValueError, match="already in use"):
        manager.createDirectoryUpload(GROUP, USER, manifest())


def test_finalize_checks_objects_once_and_returns_one_job(manager):
    session = manager.createDirectoryUpload(GROUP, USER, manifest())

    def verify(bucket, item):
        return {**item, "generation": "123", "crc32c": "checksum"}

    with patch.object(
        storage_api, "verify_directory_upload", side_effect=verify
    ) as check:
        first = manager.finalizeDirectoryUpload(session, USER)
        second = manager.finalizeDirectoryUpload(session, USER)
    assert first == second
    assert manager.db.processing_jobs.count_documents({}) == 1
    assert check.call_count == 2
    assert first["input"]["documents"][0]["generation"] == "123"
    assert first["input"]["output_prefix"].startswith("directory_upload_outputs/")


def test_missing_file_prevents_dispatch(manager):
    session = manager.createDirectoryUpload(GROUP, USER, manifest())
    with patch.object(
        storage_api,
        "verify_directory_upload",
        side_effect=ValueError("Incomplete file"),
    ):
        with pytest.raises(ValueError, match="Incomplete"):
            manager.finalizeDirectoryUpload(session, USER)
    assert manager.db.processing_jobs.count_documents({}) == 0


def test_upload_urls_cannot_overwrite_existing_objects():
    blob = Mock()
    item = {
        "object_name": "directory_uploads/session/0/well.pdf",
        "size": 100,
        "content_type": "application/pdf",
    }
    with patch.object(
        storage_api,
        "_get_bucket",
        return_value=(None, SimpleNamespace(blob=lambda _: blob)),
    ):
        storage_api.create_directory_upload_url(
            "bucket", item, "https://frontend.example"
        )
    assert blob.create_resumable_upload_session.call_args.kwargs == {
        "content_type": "application/pdf",
        "size": 100,
        "origin": "https://frontend.example",
        "if_generation_match": 0,
        "timeout": 30,
    }


def test_verification_checks_size_type_and_generation():
    blob = Mock(size=100, content_type="application/pdf", generation=2, crc32c="abc")
    item = {
        "object_name": "test",
        "relative_path": "test.pdf",
        "size": 100,
        "content_type": "application/pdf",
        "generation": "1",
    }
    with patch.object(
        storage_api,
        "_get_bucket",
        return_value=(None, SimpleNamespace(blob=lambda _: blob)),
    ):
        with pytest.raises(ValueError, match="changed"):
            storage_api.verify_directory_upload("bucket", item)
        blob.reload.side_effect = NotFound("missing")
        assert (
            storage_api.verify_directory_upload("bucket", item, required=False) is None
        )


def test_capacity_limit_is_atomic_and_same_job_does_not_use_two_slots(manager):
    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = list(
            pool.map(lambda i: manager.reserveProcessingCapacity(str(i), 2), range(8))
        )
    assert sum(admitted) == 2
    tokens = manager.db.processing_capacity.find_one()["jobs"]
    assert manager.reserveProcessingCapacity(tokens[0], 2)
    assert len(manager.db.processing_capacity.find_one()["jobs"]) == 2
    manager.releaseProcessingCapacity(tokens[0])
    assert manager.reserveProcessingCapacity("next", 2)


def test_failed_worker_marks_linked_records_and_stale_attempt_cannot_finish_retry(
    manager,
):
    job = manager.createBatchProcessingJob(
        GROUP, USER, "bucket", job_id="b" * 32, documents=[]
    )
    manager.beginProcessingJobDispatch(job["job_id"], {})
    assert manager.claimProcessingJob(job["job_id"])
    assert manager.claimProcessingJob(job["job_id"]) is None
    manager.db.records.insert_one(
        {
            "processing_job_id": job["job_id"],
            "processing_attempt": 0,
            "status": "processing",
        }
    )
    manager.completeProcessingJob(job["job_id"], "error", "worker failed", attempt=0)
    assert manager.db.records.find_one()["status"] == "error"
    retried = manager.retryProcessingJob(job["job_id"])
    assert retried["attempt"] == 1
    manager.completeProcessingJob(job["job_id"], "error", "stale worker", attempt=0)
    assert manager.getProcessingJob(job["job_id"])["status"] == "queued"
    assert not manager.beginProcessingJobDispatch(job["job_id"], {}, 0)
    manager.beginProcessingJobDispatch(job["job_id"], {}, 1)
    assert manager.claimProcessingJob(job["job_id"], 0) is None
    assert manager.claimProcessingJob(job["job_id"], 1)


def test_retry_reuses_record_id(manager):
    from ogrre.internal import batch_document_processing as batch

    document = SimpleNamespace(
        gcs_uri="gs://bucket/well.pdf", mime_type="application/pdf"
    )
    with patch.object(
        storage_api, "download_file_bytes", return_value=b"pdf"
    ), patch.object(
        batch, "_convert_document_to_png_files", return_value=[("well.png", b"png")]
    ), patch.object(
        batch, "_upload_png_files"
    ), patch.object(
        batch, "_update_whitespace_results"
    ):
        first = batch._prepare_document_for_batch(
            document, GROUP, USER, manager, "job", 0
        )
        manager.db.records.update_one(
            {"_id": ObjectId(first.record_id)}, {"$set": {"status": "error"}}
        )
        second = batch._prepare_document_for_batch(
            document, GROUP, USER, manager, "job", 1
        )
    assert first.record_id == second.record_id
    assert manager.db.records.count_documents({}) == 1
    assert manager.db.records.find_one()["processing_attempt"] == 1
