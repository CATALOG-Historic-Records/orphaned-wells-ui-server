import asyncio
import os
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import yaml
from bson import ObjectId
from fastapi import BackgroundTasks, HTTPException

from ogrre.internal import storage_api
from ogrre.internal.export_job_runner import (
    _export_job_name,
    _kubernetes_export_job_manifest,
    run_export_job,
)
from ogrre.tests.test_directory_upload import GROUP, USER


class FakeRequest:
    def __init__(self, payload=None, origin="https://frontend.example"):
        self._payload = payload
        self.headers = {"origin": origin}
        self.cookies = {}
        self.base_url = "http://testserver/"

    async def json(self):
        if self._payload is None:
            raise ValueError("No JSON payload")
        return self._payload


@pytest.fixture
def export_projects(manager, monkeypatch, tmp_path):
    from ogrre.routers import router

    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path / "local-storage"))
    monkeypatch.setattr(storage_api, "STORAGE_BACKEND", "local")
    monkeypatch.setattr(
        storage_api, "LOCAL_STORAGE_ROOT", str(tmp_path / "local-storage")
    )
    monkeypatch.setattr(router, "data_manager", manager)

    first, second, hidden = [ObjectId() for _ in range(3)]
    other_group = str(ObjectId())
    manager.db.projects.insert_many(
        [
            {"_id": first, "name": "Project Alpha", "record_groups": [GROUP]},
            {"_id": second, "name": "Project Beta", "record_groups": [other_group]},
            {"_id": hidden, "name": "Hidden Project", "record_groups": [str(hidden)]},
        ]
    )
    manager.db.record_groups.insert_many(
        [
            {
                "_id": ObjectId(GROUP),
                "name": "Alpha Group",
                "documentType": "Well Log",
                "processorId": "test-processor",
            },
            {
                "_id": ObjectId(other_group),
                "name": "Beta Group",
                "documentType": "Permit",
                "processorId": "test-processor",
            },
        ]
    )
    manager.db.records.insert_many(
        [
            {
                "_id": ObjectId(),
                "name": "Well-001",
                "filename": "well-001.pdf",
                "record_group_id": GROUP,
                "dateCreated": 1000,
                "status": "digitized",
                "review_status": "reviewed",
                "attributesList": [
                    {
                        "key": "API Number",
                        "value": "42-001-00001",
                        "raw_text": "4200100001",
                        "confidence": 0.99,
                        "normalized_vertices": [],
                        "subattributes": [],
                    }
                ],
                "image_files": [],
            },
            {
                "_id": ObjectId(),
                "name": "Well-002",
                "filename": "well-002.pdf",
                "record_group_id": other_group,
                "dateCreated": 1001,
                "status": "digitized",
                "review_status": "unreviewed",
                "attributesList": [
                    {
                        "key": "API Number",
                        "value": "42-002-00002",
                        "raw_text": "4200200002",
                        "confidence": 0.95,
                        "normalized_vertices": [],
                        "subattributes": [],
                    }
                ],
                "image_files": [],
            },
        ]
    )
    manager.db.teams.insert_one(
        {
            "name": "TeamA",
            "users": [USER["email"], "teammate@example.com"],
            "project_list": [first, second],
        }
    )
    manager.db.users.insert_many(
        [
            {
                "email": USER["email"],
                "name": USER.get("name", "Exporter"),
                "default_team": "TeamA",
                "roles": {
                    "team": {"TeamA": ["team_member"]},
                    "projects": {},
                    "system": [],
                },
            },
            {
                "email": "teammate@example.com",
                "name": "Teammate",
                "default_team": "TeamA",
                "roles": {
                    "team": {"TeamA": ["team_member"]},
                    "projects": {},
                    "system": [],
                },
            },
        ]
    )

    def fake_fetch_records(
        sort_by=["dateCreated", 1],
        filter_by={},
        page=None,
        records_per_page=None,
        **kwargs,
    ):
        docs = [
            {**doc, "_id": str(doc["_id"])}
            for doc in manager.db.records.find(filter_by or {})
        ]
        return docs, len(docs)

    monkeypatch.setattr(manager, "fetchRecords", fake_fetch_records)
    return str(first), str(second), other_group, str(hidden)


def test_create_and_download_export_job_end_to_end(manager, export_projects):
    from ogrre.routers import router

    first, _, _, _ = export_projects
    background_tasks = BackgroundTasks()

    job = asyncio.run(
        router.create_export_job(
            location="record_group",
            _id=GROUP,
            request=FakeRequest(
                {
                    "columns": ["API Number"],
                    "filter": {},
                    "sort": ["dateCreated", 1],
                    "export_raw_values": False,
                }
            ),
            background_tasks=background_tasks,
            export_csv=True,
            export_json=True,
            output_name="alpha_export",
            user_info=USER,
        )
    )
    job_id = job["job_id"]
    assert job["project_id"] == first
    assert job["project_name"] == "Project Alpha"
    assert job["record_group_id"] == GROUP
    assert job["record_group_name"] == "Alpha Group"

    asyncio.run(background_tasks())

    finished = router.get_export_job(job_id=job_id, user_info=USER)
    assert finished["status"] == "completed"
    assert finished["artifact"]["filename"] == "alpha_export.zip"
    assert finished["artifact"]["record_count"] == 1
    assert finished["artifact"]["size_bytes"] > 0
    assert finished["expires_at"] > time.time()

    dl_payload = router.get_export_job_download(
        job_id=job_id,
        request=FakeRequest(),
        user_info=USER,
    )
    assert dl_payload["filename"] == "alpha_export.zip"
    assert dl_payload["download_url"].endswith(f"/export_jobs/{job_id}/download/file")

    file_resp = router.download_local_export_job_file(job_id=job_id, user_info=USER)
    assert os.path.isfile(file_resp.path)
    with zipfile.ZipFile(file_resp.path) as archive:
        names = archive.namelist()
        assert any(name.endswith(".csv") for name in names)
        assert any(name.endswith(".json") for name in names)
        csv_name = next(name for name in names if name.endswith(".csv"))
        csv_text = archive.read(csv_name).decode("utf-8")
        assert "42-001-00001" in csv_text


def test_export_job_history_mine_vs_team_and_filters(manager, export_projects):
    from ogrre.routers import router

    first, second, other_group, hidden = export_projects
    other_user = {"email": "teammate@example.com", "name": "Teammate"}

    scopes = router.get_export_job_scopes(user_info=USER)
    assert [project["id"] for project in scopes] == [first, second]

    own_job = manager.createExportJob(
        "record_group",
        GROUP,
        USER,
        {"export_csv": True, "columns": ["API Number", "Well Name"]},
        output_name="mine_export",
        job_id="job-mine",
    )
    manager.completeExportJob(
        own_job["job_id"],
        "completed",
        artifact={
            "storage_key": "exports/job-mine/mine_export.zip",
            "filename": "mine_export.zip",
            "size_bytes": 512,
            "record_count": 1,
        },
    )

    team_job = manager.createExportJob(
        "record_group",
        other_group,
        other_user,
        {"export_json": True},
        output_name="team_export",
        job_id="job-team",
    )
    manager.completeExportJob(
        team_job["job_id"],
        "error",
        error="Storage timeout",
    )

    active_job = manager.createExportJob(
        "project",
        first,
        USER,
        {"export_csv": True},
        output_name="active_export",
        job_id="job-active",
    )
    assert active_job["status"] == "queued"

    # Default view_scope is "mine": active_jobs returns in-progress jobs, jobs returns finished history
    mine_res = asyncio.run(
        router.get_export_job_history(FakeRequest({}), user_info=USER)
    )
    assert [j["job_id"] for j in mine_res["jobs"]] == ["job-mine"]
    assert mine_res["jobs"][0]["column_count"] == 2
    assert [j["job_id"] for j in mine_res["active_jobs"]] == ["job-active"]

    # Team view_scope includes teammate exports in accessible projects
    team_res = asyncio.run(
        router.get_export_job_history(
            FakeRequest({"view_scope": "team"}), user_info=USER
        )
    )
    assert {j["job_id"] for j in team_res["jobs"]} == {"job-mine", "job-team"}

    # Search filter matches output_name / requester email
    search_res = asyncio.run(
        router.get_export_job_history(
            FakeRequest({"view_scope": "team", "search": "team_export"}),
            user_info=USER,
        )
    )
    assert [j["job_id"] for j in search_res["jobs"]] == ["job-team"]

    # Cancel active job and rerun failed job
    canceled = router.cancel_export_job("job-active", user_info=USER)
    assert canceled["status"] == "error"
    assert canceled["error"] == "Canceled by user"

    rerun = router.rerun_export_job(
        "job-team",
        request=FakeRequest(),
        background_tasks=BackgroundTasks(),
        user_info=USER,
    )
    assert rerun["job_id"] != "job-team"
    assert rerun["output_name"] == "team_export"

    # Filter by status and requester
    failed_res = asyncio.run(
        router.get_export_job_history(
            FakeRequest(
                {
                    "view_scope": "team",
                    "filter": {
                        "status": {"$in": ["error"]},
                        "request_user.email": {"$regex": "teammate"},
                    },
                }
            ),
            user_info=USER,
        )
    )
    assert [j["job_id"] for j in failed_res["jobs"]] == ["job-team"]

    # Unauthorized project or record group is rejected with 403
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            router.get_export_job_history(
                FakeRequest({"project_id": hidden}), user_info=USER
            )
        )
    assert exc_info.value.status_code == 403

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            router.get_export_job_history(
                FakeRequest({"project_id": first, "record_group_id": other_group}),
                user_info=USER,
            )
        )
    assert exc_info.value.status_code == 403


def test_export_expiration_deletes_artifact_and_blocks_download(
    manager, export_projects
):
    from ogrre.routers import router

    job = manager.createExportJob(
        "record_group",
        GROUP,
        USER,
        {"export_csv": True},
        output_name="expiring_export",
        job_id="job-expiring",
    )
    run_export_job(job["job_id"], data_manager=manager)
    completed = manager.getExportJob(job["job_id"])
    storage_key = completed["artifact"]["storage_key"]
    local_path = storage_api.get_local_export_file_path(storage_key)
    assert os.path.isfile(local_path)

    # Backdate expires_at to the past
    manager.db.export_jobs.update_one(
        {"_id": job["job_id"]},
        {"$set": {"expires_at": time.time() - 10}},
    )
    expired_count = manager.expireExportJobs()
    assert expired_count == 1
    assert not os.path.isfile(local_path)

    updated = manager.getExportJob(job["job_id"])
    assert updated["status"] == "expired"

    with pytest.raises(HTTPException) as exc_info:
        router.get_export_job_download(
            job_id=job["job_id"], request=FakeRequest(), user_info=USER
        )
    assert exc_info.value.status_code == 410

    with pytest.raises(HTTPException) as exc_info:
        router.download_local_export_job_file(job_id=job["job_id"], user_info=USER)
    assert exc_info.value.status_code == 410


def test_export_notifications_and_mark_read(manager, export_projects):
    from ogrre.routers import router

    job = manager.createExportJob(
        "record_group",
        GROUP,
        USER,
        {"export_csv": True},
        output_name="notify_export",
        job_id="job-notify",
    )
    initial = router.get_export_job_notifications(user_info=USER)
    assert initial["active_count"] == 1
    assert initial["unread_count"] == 0

    manager.completeExportJob(
        job["job_id"],
        "completed",
        artifact={
            "storage_key": "exports/job-notify/notify_export.zip",
            "filename": "notify_export.zip",
            "size_bytes": 128,
            "record_count": 1,
        },
    )
    after_complete = router.get_export_job_notifications(user_info=USER)
    assert after_complete["active_count"] == 0
    assert after_complete["unread_count"] == 1
    assert after_complete["notifications"][0]["job_id"] == "job-notify"

    asyncio.run(
        router.mark_export_job_notifications_read(
            FakeRequest({"job_id": "job-notify"}), user_info=USER
        )
    )
    after_read = router.get_export_job_notifications(user_info=USER)
    assert after_read["unread_count"] == 0
    assert after_read["notifications"][0]["notification_read_at"] is not None


def test_export_history_handles_user_without_default_team(manager, export_projects):
    from ogrre.routers import router

    unassigned_user = {
        "iss": "https://accounts.google.com",
        "email": "unassigned@example.com",
        "name": "Unassigned User",
    }
    history = asyncio.run(
        router.get_export_job_history(
            FakeRequest({"view_scope": "mine"}), user_info=unassigned_user
        )
    )
    assert history == {
        "active_jobs": [],
        "active_count": 0,
        "jobs": [],
        "count": 0,
    }
    notifications = router.get_export_job_notifications(user_info=unassigned_user)
    assert notifications == {
        "unread_count": 0,
        "active_count": 0,
        "notifications": [],
    }


def test_kubernetes_export_manifest_matches_backend_deployment():
    job_id = "e" * 32
    with patch.dict(
        os.environ,
        {
            "EXPORT_JOB_CPU_REQUEST": "2",
            "EXPORT_JOB_MEMORY_REQUEST": "8Gi",
            "EXPORT_JOB_ACTIVE_DEADLINE_SECONDS": "3600",
        },
        clear=False,
    ):
        manifest = _kubernetes_export_job_manifest(job_id, "registry.example/ogrre:sha")

    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert manifest["metadata"]["name"] == _export_job_name(job_id)
    assert container["args"] == ["--job-type", "export", "--job-id", job_id]
    assert container["resources"]["requests"]["cpu"] == "2"
    assert container["resources"]["requests"]["memory"] == "8Gi"
    assert (
        manifest["spec"]["template"]["spec"]["serviceAccountName"]
        == "processing-worker"
    )

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

    worker_pod_spec = manifest["spec"]["template"]["spec"]
    assert worker_pod_spec["imagePullSecrets"] == api_pod_spec["imagePullSecrets"]
    assert container["envFrom"] == api_container["envFrom"]


def test_api_key_lifecycle_and_authentication(manager, export_projects):
    from ogrre.routers import router

    created = asyncio.run(
        router.create_api_key(
            FakeRequest({"name": "RRC Nightly Cron", "expires_in_days": 30}),
            user_info=USER,
        )
    )
    assert created["name"] == "RRC Nightly Cron"
    assert created["api_key"].startswith("ogrre_")
    assert created["key_prefix"] == created["api_key"][:12]
    raw_key = created["api_key"]
    key_id = created["key_id"]

    listed = router.list_api_keys(user_info=USER)
    assert len(listed["api_keys"]) == 1
    assert "api_key" not in listed["api_keys"][0]
    assert "key_hash" not in listed["api_keys"][0]
    assert listed["api_keys"][0]["key_id"] == key_id
    assert listed["api_keys"][0]["expired"] is False

    auth_user = router.authenticate(FakeRequest(), token=raw_key)
    assert auth_user["email"] == USER["email"]
    assert auth_user["api_key_id"] == key_id

    revoked = router.revoke_api_key(key_id, user_info=USER)
    assert revoked["revoked"] is True
    assert revoked["revoked_at"] is not None

    with pytest.raises(HTTPException) as exc_info:
        router.authenticate(FakeRequest(), token=raw_key)
    assert exc_info.value.status_code == 401


def test_reviewed_records_endpoint_watermark_and_updates(manager, export_projects):
    from ogrre.routers import router

    # Initial watermark fetch returns the 1 reviewed record (Well-001)
    first_run = router.get_reviewed_records(
        request=FakeRequest(),
        since=None,
        until=None,
        project_id=None,
        record_group_id=None,
        document_type=None,
        page=0,
        page_size=100,
        export_format="json",
        user_info=USER,
    )
    assert first_run["since"] == 0.0
    assert first_run["total_count"] == 1
    assert first_run["has_more"] is False
    assert [row["file"] for row in first_run["records"]] == ["well-001.pdf"]

    # Immediate second run without changes returns 0 records and uses first_run["until"] as since
    second_run = router.get_reviewed_records(
        request=FakeRequest(),
        since=None,
        until=None,
        project_id=None,
        record_group_id=None,
        document_type=None,
        page=0,
        page_size=100,
        export_format="json",
        user_info=USER,
    )
    assert second_run["since"] == pytest.approx(first_run["until"])
    assert second_run["total_count"] == 0
    assert second_run["records"] == []

    # Mark Well-002 as reviewed and update an attribute on already-reviewed Well-001
    well_1 = manager.db.records.find_one({"name": "Well-001"})
    well_2 = manager.db.records.find_one({"name": "Well-002"})
    manager.updateRecord(
        str(well_2["_id"]),
        {"review_status": "reviewed"},
        update_type="review_status",
        user_info=USER,
        forceUpdate=True,
    )
    updated_attrs = list(well_1["attributesList"])
    updated_attrs[0] = {**updated_attrs[0], "value": "42-001-99999"}
    manager.updateRecord(
        str(well_1["_id"]),
        {"attributesList": updated_attrs},
        update_type="attributesList",
        user_info=USER,
        forceUpdate=True,
    )

    refreshed_well_1 = manager.db.records.find_one({"_id": well_1["_id"]})
    refreshed_well_2 = manager.db.records.find_one({"_id": well_2["_id"]})
    assert refreshed_well_1.get("lastUpdated") is not None
    assert refreshed_well_2.get("lastUpdated") is not None

    # Next incremental run picks up both updated reviewed records
    third_run = router.get_reviewed_records(
        request=FakeRequest(),
        since=None,
        until=None,
        project_id=None,
        record_group_id=None,
        document_type=None,
        page=0,
        page_size=100,
        export_format="json",
        user_info=USER,
    )
    assert third_run["since"] == pytest.approx(second_run["until"])
    assert third_run["total_count"] == 2
    returned_by_file = {row["file"]: row for row in third_run["records"]}
    assert set(returned_by_file.keys()) == {"well-001.pdf", "well-002.pdf"}
    assert returned_by_file["well-001.pdf"]["API Number"]["value"] == "42-001-99999"

    # Verify export history tracking in db.export_jobs and rerun behavior
    export_logs = list(
        manager.db.export_jobs.find({"export_source": "reviewed_records_api"})
    )
    assert len(export_logs) == 3
    assert all(log["is_watermark"] is True for log in export_logs)

    history = asyncio.run(
        router.get_export_job_history(
            FakeRequest({"view_scope": "mine"}), user_info=USER
        )
    )
    assert history["count"] == 3

    # Rerunning the first export job should reproduce its exact (since, until] slice (only Well-001)
    rerun_tasks = BackgroundTasks()
    rerun_job = router.rerun_export_job(
        job_id=first_run["job_id"],
        request=FakeRequest(),
        background_tasks=rerun_tasks,
        user_info=USER,
    )
    asyncio.run(rerun_tasks())
    completed_rerun = router.get_export_job(job_id=rerun_job["job_id"], user_info=USER)
    assert completed_rerun["status"] == "completed"
    assert completed_rerun["options"]["incremental_reviewed"] is True
    assert completed_rerun["options"]["since"] == pytest.approx(first_run["since"])
    assert completed_rerun["options"]["until"] == pytest.approx(first_run["until"])
    assert completed_rerun["artifact"]["record_count"] == 1


def test_reviewed_records_endpoint_filters_pagination_and_csv(manager, export_projects):
    from ogrre.routers import router

    well_2 = manager.db.records.find_one({"name": "Well-002"})
    manager.updateRecord(
        str(well_2["_id"]),
        {"review_status": "reviewed"},
        update_type="review_status",
        user_info=USER,
        forceUpdate=True,
    )

    # Filter by document_type="Well Log" should only return Well-001
    filtered = router.get_reviewed_records(
        request=FakeRequest(),
        since="1970-01-01T00:00:00Z",
        until=None,
        project_id=None,
        record_group_id=None,
        document_type="Well Log",
        page=0,
        page_size=100,
        export_format="json",
        user_info=USER,
    )
    assert filtered["total_count"] == 1
    assert [row["file"] for row in filtered["records"]] == ["well-001.pdf"]

    # Pagination with page_size=1 across the 2 reviewed records
    page_0 = router.get_reviewed_records(
        request=FakeRequest(),
        since="0",
        until=None,
        project_id=None,
        record_group_id=None,
        document_type=None,
        page=0,
        page_size=1,
        export_format="json",
        user_info=USER,
    )
    assert page_0["total_count"] == 2
    assert page_0["has_more"] is True
    assert len(page_0["records"]) == 1

    page_1 = router.get_reviewed_records(
        request=FakeRequest(),
        since="0",
        until=str(page_0["until"]),
        project_id=None,
        record_group_id=None,
        document_type=None,
        page=1,
        page_size=1,
        export_format="json",
        user_info=USER,
    )
    assert page_1["total_count"] == 2
    assert page_1["has_more"] is False
    assert len(page_1["records"]) == 1
    assert page_0["records"][0]["file"] != page_1["records"][0]["file"]

    # CSV response format
    csv_resp = router.get_reviewed_records(
        request=FakeRequest(),
        since="0",
        until=None,
        project_id=None,
        record_group_id=None,
        document_type=None,
        page=0,
        page_size=100,
        export_format="csv",
        user_info=USER,
    )
    assert csv_resp.headers["X-Total-Count"] == "2"
    assert csv_resp.headers["X-Has-More"] == "false"
    csv_body = csv_resp.body.decode("utf-8")
    assert "well-001.pdf" in csv_body
    assert "well-002.pdf" in csv_body

