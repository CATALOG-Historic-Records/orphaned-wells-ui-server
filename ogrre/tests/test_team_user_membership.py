"""Adding users must use the same membership source as the admin user list."""

import copy
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


ADMIN = {"email": "admin@example.com"}
EMAIL = "member@example.com"


@pytest.fixture
def team_client(manager, monkeypatch):
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    from ogrre.routers import router

    monkeypatch.setattr(router, "data_manager", manager)
    monkeypatch.setattr(router, "REQUIRE_AUTH", True)
    manager.hasPermission = Mock(return_value=True)
    manager.db.users.insert_one({**ADMIN, "default_team": "isgs"})
    manager.db.teams.insert_one({"name": "isgs", "users": [ADMIN["email"]]})
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[router.authenticate] = lambda: ADMIN
    with TestClient(app) as client:
        client.cookies.set("ogrre_csrf", "team-test-csrf")
        client.headers["X-CSRF-Token"] = "team-test-csrf"
        yield client


@pytest.mark.parametrize("default_team", ["isgs", "other-team"])
@pytest.mark.parametrize("existing_roles", [[], ["team_lead"]])
def test_add_repairs_missing_membership_and_lists_user(
    team_client, manager, default_team, existing_roles
):
    roles = {"system": ["developer"], "team": {"other-team": ["team_member"]}}
    if existing_roles:
        roles["team"]["isgs"] = existing_roles
    manager.db.users.insert_one(
        {"email": EMAIL, "default_team": default_team, "roles": copy.deepcopy(roles)}
    )
    assert EMAIL not in [user["email"] for user in team_client.get("/get_users").json()]

    response = team_client.post(f"/add_user/{EMAIL}", json={})
    assert response.status_code == 200
    assert response.json() == "success"
    assert EMAIL in [user["email"] for user in team_client.get("/get_users").json()]
    assert manager.db.users.count_documents({"email": EMAIL}) == 1
    user = manager.db.users.find_one({"email": EMAIL})
    assert user["default_team"] == default_team
    assert user["roles"] == {
        **roles,
        "team": {**roles["team"], "isgs": existing_roles or ["team_member"]},
    }
    manager.recordHistory.assert_called_once_with(
        "addUser", user=ADMIN["email"], query={"email": EMAIL, "team": "isgs"}
    )

    # Duplicate detection agrees with the list, even for another default team.
    assert team_client.post(f"/add_user/{EMAIL}", json={}).status_code == 406
    assert manager.db.teams.find_one({"name": "isgs"})["users"].count(EMAIL) == 1


def test_new_user_is_created_and_listed(team_client, manager):
    assert team_client.post(f"/add_user/{EMAIL}", json={}).status_code == 200
    assert EMAIL in [user["email"] for user in team_client.get("/get_users").json()]
    user = manager.db.users.find_one({"email": EMAIL})
    assert user["default_team"] == "isgs"
    assert user["roles"]["team"]["isgs"] == ["team_member"]


def test_add_requires_permission_before_repairing_membership(team_client, manager):
    manager.db.users.insert_one({"email": EMAIL, "default_team": "isgs"})
    manager.hasPermission.return_value = False
    assert team_client.post(f"/add_user/{EMAIL}", json={}).status_code == 403
    assert EMAIL not in manager.db.teams.find_one({"name": "isgs"})["users"]
    manager.recordHistory.assert_not_called()
