"""Only explicit retirement/reintroduction propagates safe field edits to records."""

from unittest.mock import Mock

import pytest

from ogrre.tests.test_attribute_retirement import (
    insert_record,
    retirement_manager,
    schema_manager,
    FIELD,
    GROUP,
    USER,
)


@pytest.mark.parametrize(
    "updates",
    [
        {"alias": "Depth"},
        {"page_order_sort": 2},
        {"cleaning_function": "string_to_float"},
    ],
)
def test_safe_field_saves_never_read_groups_or_records(
    retirement_manager, monkeypatch, updates
):
    manager = retirement_manager
    record_id = insert_record(manager, [{"key": "depth", "value": "12"}])
    before = manager.db.records.find_one({"_id": record_id})
    with monkeypatch.context() as patch:
        patch.setattr(
            manager,
            "_groupsUsingSchema",
            Mock(side_effect=AssertionError("Unexpected record-group scan")),
        )
        patch.setattr(
            manager,
            "_ensureRecordGroupsReconciled",
            Mock(side_effect=AssertionError("Unexpected record reconciliation")),
        )
        patch.setattr(
            manager.db.records,
            "find",
            Mock(side_effect=AssertionError("Unexpected record scan")),
        )
        manager.updateProcessorAttribute("well", "depth", updates, USER)
    assert manager.db.records.find_one({"_id": record_id}) == before
    field = manager.db.processors.find_one()["attributes"][0]
    assert all(field[key] == value for key, value in updates.items())


def test_retirement_survives_safe_edits_before_reintroduction(
    retirement_manager, monkeypatch
):
    manager = retirement_manager
    record_id = insert_record(manager, [{"key": "depth", "value": "keep forever"}])
    reconcile = Mock(wraps=manager._ensureRecordGroupsReconciled)
    monkeypatch.setattr(manager, "_ensureRecordGroupsReconciled", reconcile)
    manager.updateProcessorAttribute("well", "depth", {}, USER, "delete")
    assert reconcile.call_count == 2
    reconcile.reset_mock()
    manager.updateProcessorAttribute(
        "well", "new", {**FIELD, "name": "new"}, USER, "add"
    )
    manager.updateProcessorAttribute("well", "new", {"alias": "New alias"}, USER)
    reconcile.assert_not_called()
    assert manager.db.records.find_one({"_id": record_id})["attributesList"][0].get(
        "deleted"
    )
    manager.updateProcessorAttribute("well", "depth", FIELD, USER, "add")
    reconcile.assert_called_once_with([GROUP], USER)
    record = manager._reconcileRecord(
        manager.db.records.find_one({"_id": record_id}), user=USER
    )
    old = next(
        attribute
        for attribute in record["attributesList"]
        if attribute["key"] == "depth"
    )
    assert old["value"] == "keep forever" and old["deleted"]


def test_permission_check_fetches_user_and_roles_once(schema_manager, monkeypatch):
    manager = schema_manager
    monkeypatch.delattr(manager, "hasPermission")
    manager.db.users.insert_one(
        {
            "email": USER["email"],
            "default_team": "team",
            "roles": {"system": ["sys_admin"]},
        }
    )
    manager.db.roles.insert_one(
        {"id": "sys_admin", "category": "system", "permissions": ["manage_schema"]}
    )
    user_query = Mock(wraps=manager.db.users.find_one)
    role_query = Mock(wraps=manager.db.roles.find)
    monkeypatch.setattr(manager.db.users, "find_one", user_query)
    monkeypatch.setattr(manager.db.roles, "find", role_query)
    assert manager.hasPermission(USER["email"], "manage_schema")
    user_query.assert_called_once_with({"email": USER["email"]})
    assert role_query.call_count == 1
    assert not manager.hasPermission("missing@example.com", "manage_schema")
