"""Page queries read stored data without preparing entire groups for editing."""

import copy
from unittest.mock import Mock

import pytest

from ogrre.internal import util
from ogrre.reconcile_schema_records import reconcile_batch
from ogrre.tests.test_attribute_retirement import (
    FIELD,
    GROUP,
    USER,
    insert_record,
    query_manager,
    retirement_manager,
    schema_manager,
)


@pytest.mark.parametrize("container", [None, "bad", 42, {"key": "invalid"}])
def test_malformed_attribute_containers_are_ignored(container):
    assert list(util.iter_attribute_tree(container)) == []
    assert util.active_attributes(container) == []
    result, _ = util.sortRecordAttributes(container, None)
    assert result == []


def test_normalization_keeps_valid_nested_and_retired_values():
    original = [
        None,
        {
            "key": "depth",
            "value": "keep",
            "subattributes": [
                None,
                3,
                {"key": "child", "value": 7, "subattributes": "bad"},
            ],
        },
        {"key": "retired", "deleted": True, "subattributes": [None]},
    ]
    before = copy.deepcopy(original)
    result, changed = util.sortRecordAttributes(original, {"attributes": [FIELD]}, True)
    assert changed and original == before
    assert result[0]["value"] == "keep"
    assert result[0]["subattributes"][0]["value"] == 7
    assert result[0]["subattributes"][0]["subattributes"] == []
    assert result[1] == original[2]
    again, changed = util.sortRecordAttributes(result, {"attributes": [FIELD]}, True)
    assert again == result and not changed


def test_lists_statistics_navigation_and_columns_do_not_prepare_records(
    query_manager, monkeypatch
):
    manager = query_manager
    project_id = manager.db.projects.insert_one(
        {"name": "Project", "record_groups": [GROUP]}
    ).inserted_id
    trees = [
        None,
        "bad",
        42,
        {"key": "invalid"},
        [None, 4, "bad", {"key": "depth", "value": {"bad": 1}, "subattributes": 4}],
        [{"key": "depth", "value": "9" * 100, "cleaning_error": "invalid number"}],
        [
            {
                "key": "parent",
                "subattributes": [
                    None,
                    {
                        "key": "child",
                        "subattributes": [
                            None,
                            {"key": "leaf", "cleaning_error": "bad"},
                        ],
                    },
                ],
            }
        ],
        [
            {
                "key": "retired",
                "deleted": True,
                "subattributes": [{"key": "child", "cleaning_error": "hidden"}],
            }
        ],
        [{"key": "depth", "value": 12, "subattributes": [], "cleaning_error": ""}],
    ]
    for index, tree in enumerate(trees):
        record_id = insert_record(manager, tree, str(index))
        manager.db.records.update_one(
            {"_id": record_id},
            {
                "$set": {
                    "has_errors": index % 2
                    == 0,  # Intentionally stale/incorrect caches.
                    "review_status": "reviewed" if index < 2 else "unreviewed",
                }
            },
        )
    before = list(manager.db.records.find().sort("_id", 1))
    monkeypatch.setattr(manager.db, "records", manager.db.records)
    for method in (
        "resolveRecordGroupSchema",
        "_ensureRecordGroupsReconciled",
        "_reconcileRecord",
    ):
        monkeypatch.setattr(
            manager,
            method,
            Mock(side_effect=AssertionError("Read attempted schema preparation")),
        )
    for method in ("update_one", "update_many", "replace_one", "bulk_write"):
        monkeypatch.setattr(
            manager.db.records,
            method,
            Mock(side_effect=AssertionError("Read attempted record write")),
        )

    stats = manager.getRecordGroupProgress([GROUP])[GROUP]
    assert stats == {
        "_id": GROUP,
        "total_amt": len(trees),
        "reviewed_amt": 2,
        "error_amt": 2,
    }
    project = manager.fetchRecordGroups(str(project_id), USER)
    assert (
        project["project"]["name"] == "Project" and len(project["record_groups"]) == 1
    )
    for sort in (["dateCreated", 1], ["attributesList.depth", 1]):
        rows, count = manager.fetchRecords(
            filter_by={"record_group_id": GROUP},
            sort_by=sort,
            page=0,
            records_per_page=3,
        )
        assert len(rows) == 3 and count == len(trees)
        manager.getRecordIndexes(rows[0], {"record_group_id": GROUP}, sort, USER)
        assert rows[0]["rank"] >= 1
    rows, count = manager.fetchRecords(
        filter_by={
            "record_group_id": GROUP,
            "attributesList": {"$elemMatch": {"key": "depth", "value": 12}},
        }
    )
    assert len(rows) == count == 1
    projected, count = manager.fetchRecords(
        filter_by={"record_group_id": GROUP},
        include_attribute_fields={
            "topLevelFields": ["filename"],
            "attributesList": ["key", "subattributes"],
            "subattributes": ["key", "value"],
        },
    )
    assert len(projected) == count == len(trees)
    assert set(manager.deriveRecordColumnsFromRecordGroups([GROUP])) == {
        "depth",
        "parent",
        "parent::child",
        "parent::child::leaf",
    }
    assert list(manager.db.records.find().sort("_id", 1)) == before


def test_opening_record_prepares_only_that_record_and_keeps_edit_indexes(
    query_manager, monkeypatch
):
    from ogrre.internal import data_manager

    manager = query_manager
    image_url = Mock(return_value="test-image")
    monkeypatch.setattr(data_manager, "get_document_image", image_url)
    selected = insert_record(
        manager,
        [
            None,
            {
                "key": "depth",
                "value": "12",
                "subattributes": [None, {"key": "child", "value": "keep"}],
            },
        ],
    )
    sibling = insert_record(manager, [None, {"key": "depth", "value": "untouched"}])
    sibling_before = manager.db.records.find_one({"_id": sibling})
    monkeypatch.setattr(manager.db, "records", manager.db.records)
    updates = Mock(wraps=manager.db.records.update_one)
    monkeypatch.setattr(manager.db.records, "update_one", updates)
    monkeypatch.setattr(
        manager,
        "_ensureRecordGroupsReconciled",
        Mock(side_effect=AssertionError("Opened sibling records")),
    )
    record, locked = manager.fetchRecordData(str(selected), USER)
    assert not locked
    image_url.assert_called_once_with(GROUP, str(selected), "test")
    assert record["img_urls"] == ["test-image"]
    assert record["attributesList"][0]["subattributes"][0]["value"] == "keep"
    assert record["attribute_revision"]
    assert updates.call_count == 1
    assert updates.call_args.args[0]["_id"] == selected
    assert manager.db.records.find_one({"_id": sibling}) == sibling_before
    manager.fetchRecordData(str(selected), USER)
    assert updates.call_count == 1


def test_schema_maintenance_is_explicit_bounded_and_resumable(retirement_manager):
    manager = retirement_manager
    manager.db.processors.update_one(
        {}, {"$set": {"attributes": [{**FIELD, "deleted": True}]}}
    )
    for i in range(3):
        insert_record(manager, [{"key": "depth", "value": i}])
    before = list(manager.db.records.find())
    preview = reconcile_batch(manager, GROUP, batch_size=2)
    assert preview["remaining"] == 3 and not preview["complete"]
    assert list(manager.db.records.find()) == before
    first = reconcile_batch(
        manager,
        GROUP,
        batch_size=2,
        apply=True,
        expected_revision=preview["schema_revision"],
    )
    assert first["remaining"] == 1 and not first["complete"]
    last = reconcile_batch(manager, GROUP, batch_size=2, apply=True)
    assert last["remaining"] == 0 and last["complete"]
    applied = list(manager.db.records.find())
    assert [record["attributesList"][0]["value"] for record in applied] == [0, 1, 2]
    assert all(record["attributesList"][0]["deleted"] for record in applied)
    reconcile_batch(manager, GROUP, batch_size=2, apply=True)
    assert list(manager.db.records.find()) == applied
    manager.db.processors.update_one({}, {"$set": {"attributes": [FIELD]}})
    with pytest.raises(ValueError, match="schema changed"):
        reconcile_batch(
            manager, GROUP, apply=True, expected_revision=preview["schema_revision"]
        )
    assert list(manager.db.records.find()) == applied


def test_records_created_during_retirement_receive_the_final_schema(
    retirement_manager, monkeypatch
):
    manager = retirement_manager
    reconcile = manager._ensureRecordGroupsReconciled
    inserted = []

    def insert_after_first_pass(*args, **kwargs):
        reconcile(*args, **kwargs)
        if not inserted:
            inserted.append(
                manager.createRecord(
                    {
                        "record_group_id": GROUP,
                        "attributesList": [{"key": "depth", "value": 9}],
                    },
                    USER,
                )
            )

    monkeypatch.setattr(
        manager, "_ensureRecordGroupsReconciled", insert_after_first_pass
    )
    manager.updateProcessorAttribute("well", "depth", {}, USER, "delete")
    record = manager.db.records.find_one()
    assert record["attributesList"][0]["deleted"]
    assert record["attributesList"][0]["value"] == 9


@pytest.mark.parametrize("include_retired_value", [False, True])
def test_new_records_never_get_placeholders_for_retired_fields(
    retirement_manager, include_retired_value
):
    manager = retirement_manager
    manager.db.processors.update_one(
        {}, {"$set": {"attributes": [FIELD, {**FIELD, "name": "old", "deleted": True}]}}
    )
    attributes = [{"key": "depth", "value": 12}]
    if include_retired_value:
        attributes.append({"key": "old", "value": "from an old export"})
    manager.createRecord({"record_group_id": GROUP, "attributesList": attributes}, USER)
    record = manager.db.records.find_one()
    retired = [item for item in record["attributesList"] if item["key"] == "old"]
    if include_retired_value:
        assert len(retired) == 1 and retired[0]["deleted"]
        assert retired[0]["value"] == "from an old export"
    else:
        assert retired == []
    assert util.active_attributes(record["attributesList"])[0]["value"] == 12
