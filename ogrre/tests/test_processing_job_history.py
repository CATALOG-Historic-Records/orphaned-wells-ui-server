import time
from unittest.mock import Mock

import pytest
from bson import ObjectId

from ogrre.internal import directory_upload
from ogrre.tests.test_directory_upload import GROUP, USER, manifest
from ogrre.tests.test_directory_upload_routes import client  # noqa: F401


def make_job(manager, job_id="history-job", status="completed", group=GROUP, **kwargs):
    job = manager.createBatchProcessingJob(
        group, USER, "bucket", job_id=job_id, **kwargs
    )
    manager.updateProcessingJob(job_id, {"status": status})
    return manager.getProcessingJob(job_id)


@pytest.fixture
def history_projects(manager, monkeypatch):
    first, second, hidden = [ObjectId() for _ in range(3)]
    other_group = str(ObjectId())
    manager.db.projects.insert_many(
        [
            {"_id": first, "name": "Project A", "record_groups": [GROUP]},
            {"_id": second, "name": "Project B", "record_groups": [other_group]},
            {"_id": hidden, "name": "Private project", "record_groups": [str(hidden)]},
        ]
    )
    manager.db.record_groups.insert_many(
        [
            {"_id": ObjectId(GROUP), "name": "First records"},
            {"_id": ObjectId(other_group), "name": "Second records"},
        ]
    )
    monkeypatch.setattr(
        manager, "getUserProjectList", Mock(return_value=[first, second])
    )
    return str(first), str(second), other_group, str(hidden)


def test_combined_history_is_scoped_named_and_globally_paginated(
    client, manager, history_projects
):
    first, second, other_group, hidden = history_projects
    make_job(manager, "old-a")
    manager.updateProcessingJob("old-a", {"created_at": 1})
    make_job(manager, "new-b", group=other_group)
    make_job(manager, "active-a", "running")
    make_job(manager, "active-b", "queued", group=other_group)
    make_job(manager, "private", group=hidden)
    make_job(manager, "private-active", "running", group=hidden)
    # History reads require upload access, then remain scoped by project access.
    manager.hasPermission.side_effect = (
        lambda _email, permission: permission == "upload_document"
    )
    scopes = client.get("/processing_jobs/scopes").json()
    assert [project["id"] for project in scopes] == [first, second]
    assert scopes[0]["record_groups"] == [{"id": GROUP, "name": "First records"}]
    result = client.post("/processing_jobs/history", json={"page_size": 1}).json()
    assert result["count"] == 2
    assert result["active_count"] == 2
    assert result["jobs"][0]["job_id"] == "new-b"
    assert result["jobs"][0]["project_name"] == "Project B"
    assert result["jobs"][0]["record_group_name"] == "Second records"
    assert result["jobs"][0]["project_id"] == second
    page = client.post(
        "/processing_jobs/history", json={"page_size": 1, "page": 1, "active_page": 1}
    ).json()
    assert page["jobs"][0]["job_id"] == "old-a"
    assert page["active_jobs"][0]["job_id"] != result["active_jobs"][0]["job_id"]
    for scope in (
        {"project_id": first},
        {"record_group_id": GROUP},
        {"project_id": first, "record_group_id": GROUP},
    ):
        result = client.post("/processing_jobs/history", json=scope).json()
        assert [job["job_id"] for job in result["jobs"]] == ["old-a"]
        assert [job["job_id"] for job in result["active_jobs"]] == ["active-a"]
    result = client.post(
        "/processing_jobs/history",
        json={"project_id": first, "filter": {"status": {"$in": ["error"]}}},
    ).json()
    assert result["count"] == 0
    assert result["active_count"] == 1


def test_combined_history_rejects_invalid_and_unauthorized_scopes(
    client, manager, history_projects
):
    first, _, other_group, hidden = history_projects
    for body in (
        [],
        {"project_id": {"$ne": None}},
        {"record_group_id": [GROUP]},
        {"project_id": ""},
        {"record_group_id": "bad"},
        {"filter": {"record_group_id": hidden}},
        {"page_size": 1000},
    ):
        assert client.post("/processing_jobs/history", json=body).status_code == 400
    for body in (
        {"project_id": hidden},
        {"record_group_id": hidden},
        {"project_id": first, "record_group_id": other_group},
    ):
        assert client.post("/processing_jobs/history", json=body).status_code == 403
    make_job(manager)
    manager.getUserProjectList.return_value = []
    assert client.get("/processing_jobs/scopes").json() == []
    result = client.post("/processing_jobs/history", json={}).json()
    assert result == {"active_jobs": [], "active_count": 0, "jobs": [], "count": 0}


def test_history_requires_upload_permission(client, manager):
    manager.hasPermission.return_value = False
    assert client.get("/processing_jobs/scopes").status_code == 403
    assert client.post("/processing_jobs/history", json={}).status_code == 403
    assert client.get(f"/processing_jobs/{GROUP}").status_code == 403
    assert client.post(f"/processing_jobs/{GROUP}/history", json={}).status_code == 403
    assert client.get(f"/processing_jobs/{GROUP}/job").status_code == 403
    assert client.get("/batch_process_documents/job/status").status_code == 403


def test_history_keeps_older_active_jobs_separate_and_omits_large_details(
    client, manager
):
    for index in range(35):
        make_job(manager, str(index))
    make_job(manager, "active", "running")
    manager.updateProcessingJob("active", {"created_at": 1})
    make_job(manager, "hidden", group="other")
    make_job(manager, "directory", documents=[{"name": "well.pdf"}] * 500)
    manager.updateProcessingJob(
        "directory", {"summary.failed_document_uris": ["gs://bucket/private.pdf"] * 500}
    )
    result = client.post(
        f"/processing_jobs/{GROUP}/history", json={"page_size": 10}
    ).json()
    assert result["count"] == 36
    assert result["active_count"] == 1
    assert result["active_jobs"][0]["job_id"] == "active"
    assert len(result["jobs"]) == 10
    directory = next(job for job in result["jobs"] if job["job_id"] == "directory")
    assert directory["file_count"] == 500
    assert "documents" not in directory["input"]
    assert "failed_document_uris" not in directory["summary"]
    second = client.post(
        f"/processing_jobs/{GROUP}/history", json={"page_size": 10, "page": 1}
    ).json()
    assert not set(job["job_id"] for job in result["jobs"]) & set(
        job["job_id"] for job in second["jobs"]
    )


def test_history_filters_are_validated_and_group_scope_is_trusted(client, manager):
    make_job(manager, "failed", "error", documents=[])
    make_job(manager, "success")
    make_job(manager, "active", "running")
    result = client.post(
        f"/processing_jobs/{GROUP}/history",
        json={
            "filter": {
                "status": {"$in": ["error"]},
                "source_type": {"$in": ["directory"]},
                "request_user.email": {"$regex": "uploader@"},
                "created_at": {"$gt": 0},
            }
        },
    ).json()
    assert [job["job_id"] for job in result["jobs"]] == ["failed"]
    assert result["active_count"] == 1
    for body in (
        [],
        {"page_size": 1000},
        {"page": -1},
        {"page": True},
        {"filter": {"record_group_id": "other"}},
        {"filter": {"$where": "true"}},
        {"filter": {"status": {"$ne": "error"}}},
        {"filter": {"created_at": {"$gt": "bad"}}},
    ):
        assert (
            client.post(f"/processing_jobs/{GROUP}/history", json=body).status_code
            == 400
        )
    assert client.post("/processing_jobs/other/history", json={}).status_code == 403


def test_active_history_is_independently_paginated_and_empty_discovery_is_known(
    client, manager
):
    for index in range(30):
        make_job(manager, str(index), "queued")
    result = client.post(
        f"/processing_jobs/{GROUP}/history", json={"active_page": 1}
    ).json()
    assert result["active_count"] == 30
    assert len(result["active_jobs"]) == 5
    assert result["count"] == 0
    assert result["active_jobs"][0]["file_count"] is None
    manager.updateProcessingJob("0", {"file_count": 0})
    assert client.get(f"/processing_jobs/{GROUP}/0").json()["job"]["file_count"] == 0


def test_file_details_are_paginated_and_link_only_existing_scoped_records(
    client, manager
):
    _, documents, _ = directory_upload.validate_manifest(manifest(60))
    job = make_job(manager, documents=documents)
    uri = f"gs://bucket/{documents[25]['object_name']}"
    record_id = directory_upload.processing_record_id(job["job_id"], uri)
    manager.db.records.insert_one(
        {
            "_id": ObjectId(record_id),
            "record_group_id": GROUP,
            "processing_job_id": job["job_id"],
            "name": "Renamed well",
            "status": "digitized",
        }
    )
    endpoint = f"/processing_jobs/{GROUP}/{job['job_id']}"
    response = client.get(endpoint, params={"file_kind": "source", "page": 1}).json()
    assert response["file_count"] == 60
    assert len(response["files"]) == 25
    assert response["files"][0]["record_id"] == record_id
    assert "record_id" not in response["files"][1]
    assert "documents" not in response["job"]["input"]
    records = client.get(endpoint).json()
    assert records["files"][0]["name"] == "Renamed well"
    assert client.get(endpoint, params={"file_kind": "bad"}).status_code == 422
    assert client.get(endpoint, params={"page_size": 101}).status_code == 422
    make_job(manager, "another-group", group="other")
    assert client.get(f"/processing_jobs/{GROUP}/another-group").status_code == 404


def test_retry_explains_expiry_ownership_permissions_and_worker_state(
    client, manager, monkeypatch
):
    from ogrre.routers import router

    session = manager.createDirectoryUpload(GROUP, USER, manifest())
    make_job(
        manager,
        session["_id"],
        "error",
        documents=[],
        upload_expires_at=session["expires_at"],
    )
    endpoint = f"/processing_jobs/{GROUP}/{session['_id']}"
    monkeypatch.setattr(
        router, "processing_worker_has_stopped", Mock(return_value=False)
    )
    assert "still stopping" in client.get(endpoint).json()["retry"]["reason"]
    router.processing_worker_has_stopped.return_value = True
    assert client.get(endpoint).json()["retry"]["allowed"]
    manager.hasPermission.return_value = False
    assert client.get(endpoint).status_code == 403
    manager.hasPermission.return_value = True
    manager.db.directory_uploads.update_one(
        {"_id": session["_id"]}, {"$set": {"expires_at": time.time() - 1}}
    )
    assert "expired" in client.get(endpoint).json()["retry"]["reason"]
    manager.updateProcessingJob(session["_id"], {"request_user.email": "other"})
    assert "original uploader" in client.get(endpoint).json()["retry"]["reason"]
    manager.db.directory_uploads.update_one(
        {"_id": session["_id"]}, {"$set": {"user_email": "other"}}
    )
    assert client.post(endpoint + "/retry").status_code == 404
