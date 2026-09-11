from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ogrre.tests.test_directory_upload import GROUP, USER, manifest


@pytest.fixture
def client(manager, monkeypatch):
    from ogrre.routers import router

    monkeypatch.setattr(router, "data_manager", manager)
    monkeypatch.setattr(router, "REQUIRE_AUTH", True)
    monkeypatch.setattr(manager, "hasPermission", Mock(return_value=True))
    monkeypatch.setattr(manager, "getUserRecordGroups", Mock(return_value=[GROUP]))
    monkeypatch.setattr(router, "_directory_upload_config", lambda: {"mode": "direct"})
    monkeypatch.setattr(
        router.auth, "parse_allowed_origins", lambda: ["https://frontend.example"]
    )
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[router.authenticate] = lambda: USER
    with TestClient(app) as test_client:
        test_client.cookies.set("ogrre_csrf", "test-csrf")
        test_client.headers["X-CSRF-Token"] = "test-csrf"
        yield test_client


def test_rejects_missing_upload_permission(client, manager):
    manager.hasPermission.return_value = False
    response = client.post(f"/directory_uploads/{GROUP}/sessions", json=manifest())
    assert response.status_code == 403
    assert manager.db.directory_uploads.count_documents({}) == 0


def test_rejects_wrong_record_group(client):
    assert (
        client.post("/directory_uploads/other/sessions", json=manifest()).status_code
        == 403
    )


def test_session_file_requires_owner_and_allowed_origin(client, manager):
    session = manager.createDirectoryUpload(GROUP, USER, manifest())
    endpoint = f"/directory_uploads/{GROUP}/sessions/{session['_id']}/files/0"
    assert (
        client.post(
            endpoint, headers={"origin": "https://untrusted.example"}
        ).status_code
        == 403
    )
    manager.db.directory_uploads.update_one(
        {"_id": session["_id"]}, {"$set": {"user_email": "other"}}
    )
    assert (
        client.post(
            endpoint, headers={"origin": "https://frontend.example"}
        ).status_code
        == 404
    )


def test_bad_manifest_does_not_create_session(client, manager):
    assert (
        client.post(f"/directory_uploads/{GROUP}/sessions", json=[]).status_code == 400
    )
    assert (
        client.post(
            f"/directory_uploads/{GROUP}/sessions", content=b"x" * (2 * 1024 * 1024 + 1)
        ).status_code
        == 413
    )
    assert manager.db.directory_uploads.count_documents({}) == 0


def test_missing_files_do_not_create_job(client, manager, monkeypatch):
    from ogrre.internal import storage_api

    session = manager.createDirectoryUpload(GROUP, USER, manifest())
    monkeypatch.setattr(
        storage_api,
        "verify_directory_upload",
        Mock(side_effect=ValueError("Incomplete file")),
    )
    assert (
        client.post(
            f"/directory_uploads/{GROUP}/sessions/{session['_id']}/finalize"
        ).status_code
        == 400
    )
    assert manager.db.processing_jobs.count_documents({}) == 0
