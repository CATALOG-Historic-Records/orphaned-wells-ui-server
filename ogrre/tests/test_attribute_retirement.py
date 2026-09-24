"""Retention tests use mock Mongo; optional query tests use a disposable local DB."""

import copy
import json
import os
import uuid
from urllib.parse import urlparse
from unittest.mock import Mock

import pytest
from bson import ObjectId
from pymongo import MongoClient

from ogrre.internal import util
from ogrre.internal.schema_validation import SchemaError
from ogrre.tests.test_schema_management import (
    FIELD,
    GROUP,
    USER,
    client,
    schema_manager,
)


@pytest.fixture
def retirement_manager(schema_manager, monkeypatch):
    schema_manager.hasPermission.side_effect = None
    schema_manager.hasPermission.return_value = True
    monkeypatch.setattr(schema_manager, "tryLockingRecord", lambda *args: True)
    schema_manager.db.record_groups.update_one(
        {"_id": ObjectId(GROUP)}, {"$set": {"processorId": "extractor"}}
    )
    return schema_manager


def insert_record(manager, attributes, name="test"):
    return manager.db.records.insert_one(
        {
            "record_group_id": GROUP,
            "name": name,
            "filename": name,
            "attributesList": copy.deepcopy(attributes),
        }
    ).inserted_id


def test_repo_reconciliation_preserves_unmatched_nested_and_repeated_values():
    original = [
        {
            "key": "old",
            "value": 12,
            "raw_text": "012",
            "normalized_vertices": [[0, 0]],
            "subattributes": [{"key": "child", "value": "nested"}],
        },
        {"key": "old", "value": 13},
    ]
    result, changed = util.sortRecordAttributes(original, {"attributes": [FIELD]})
    assert changed
    retired = [field for field in result if field.get("deleted")]
    assert [field["value"] for field in retired] == [12, 13]
    assert retired[0]["raw_text"] == "012"
    assert retired[0]["normalized_vertices"] == [[0, 0]]
    assert retired[0]["subattributes"][0]["value"] == "nested"
    assert "deleted" not in original[0]
    repeated, changed = util.sortRecordAttributes(result, {"attributes": [FIELD]})
    assert repeated == result and not changed


def test_unknown_mongo_fields_stay_visible_and_missing_schema_never_retires():
    attributes = [{"key": "unseen", "value": "keep"}]
    for schema, keep in [
        (None, False),
        ({"attributes": []}, True),
        ({"attributes": [FIELD]}, True),
    ]:
        result, _ = util.sortRecordAttributes(
            attributes, schema, keep_all_attributes=keep
        )
        assert (
            next(field for field in result if field["key"] == "unseen").get("deleted")
            is None
        )


def test_retiring_last_field_and_readding_it_does_not_restore_values(
    retirement_manager,
):
    manager = retirement_manager
    record_id = insert_record(manager, [{"key": "depth", "value": 42}])
    manager.updateProcessorAttribute("well", "depth", {}, USER, "delete")
    manager.updateProcessorAttribute("well", "depth", FIELD, USER, "add")
    manager._ensureRecordGroupsReconciled([GROUP], USER)
    record = manager.db.records.find_one({"_id": record_id})
    assert len(record["attributesList"]) == 1
    assert record["attributesList"][0]["value"] == 42
    assert record["attributesList"][0]["deleted"] is True
    assert util.active_attributes(record["attributesList"]) == []


def test_retired_fields_and_descendants_are_skipped_by_cleaning_and_reset(
    retirement_manager, monkeypatch
):
    retired = {
        "key": "depth",
        "value": "12",
        "raw_text": "012",
        "deleted": True,
        "user_added": True,
        "cleaning_error": "old",
        "subattributes": [{"key": "child", "value": "keep", "cleaning_error": "old"}],
    }
    cleaner = Mock(return_value=99)
    monkeypatch.setitem(util.CLEANING_FUNCTIONS, "retirement_test", cleaner)
    value = copy.deepcopy(retired)
    util.cleanRecordAttribute(
        {"depth": {"cleaning_function": "retirement_test"}}, value
    )
    assert value == retired
    cleaner.assert_not_called()
    assert util.searchRecordForErrorsAndTargetKeys({"attributesList": [retired]}) == (
        False,
        {},
    )
    assert retirement_manager.resetRecord("id", {"attributesList": [retired]}, USER)[
        "attributesList"
    ] == [retired]


def test_reconciliation_retries_without_overwriting_an_intervening_edit(
    retirement_manager, monkeypatch
):
    manager = retirement_manager
    record_id = insert_record(manager, [{"key": "depth", "value": 1}])
    original = manager.db.records.find_one({"_id": record_id})
    original_update = manager.db.records.update_one
    calls = 0

    def concurrent_update(query, update, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            original_update({"_id": record_id}, {"$set": {"attributesList.0.value": 2}})
        return original_update(query, update, *args, **kwargs)

    monkeypatch.setattr(manager.db.records, "update_one", concurrent_update)
    result = manager._reconcileRecord(original, user=USER)
    assert calls == 2
    assert result["attributesList"][0]["value"] == 2


def test_stale_indexed_edit_is_rejected_after_schema_reorder(retirement_manager):
    manager = retirement_manager
    second = {**FIELD, "name": "second", "page_order_sort": 2}
    manager.db.processors.update_one({}, {"$set": {"attributes": [FIELD, second]}})
    record_id = insert_record(
        manager, [{"key": "depth", "value": 1}, {"key": "second", "value": 2}]
    )
    original = manager._reconcileRecord(
        manager.db.records.find_one({"_id": record_id}), user=USER
    )
    manager.updateProcessorAttribute("well", "depth", {"page_order_sort": 3}, USER)
    with pytest.raises(SchemaError) as error:
        manager.updateRecord(
            str(record_id),
            {"indexes": [0], "v": {"key": "depth", "value": 999}},
            "attribute",
            user_info=USER,
            calling_function="update_record",
            expected_attribute_revision=original["attribute_revision"],
        )
    assert error.value.status_code == 409
    record = manager.db.records.find_one({"_id": record_id})
    assert [(field["key"], field["value"]) for field in record["attributesList"]] == [
        ("second", 2),
        ("depth", 1),
    ]


def test_full_record_edit_cannot_remove_or_restore_retired_data(retirement_manager):
    manager = retirement_manager
    record_id = insert_record(
        manager,
        [{"key": "old", "value": 42, "deleted": True}, {"key": "depth", "value": 1}],
    )
    record = manager._reconcileRecord(
        manager.db.records.find_one({"_id": record_id}), user=USER
    )
    for attributes in [
        util.active_attributes(record["attributesList"]),
        [{**field, "deleted": False} for field in record["attributesList"]],
    ]:
        with pytest.raises(SchemaError):
            manager.updateRecord(
                str(record_id),
                {"attributesList": attributes},
                "attributesList",
                user_info=USER,
                calling_function="update_record",
                expected_attribute_revision=record["attribute_revision"],
            )
    assert (
        manager.db.records.find_one({"_id": record_id})["attributesList"]
        == record["attributesList"]
    )


def test_reprocessing_keeps_retired_children_in_their_repeated_parent(
    retirement_manager,
):
    previous = [
        {
            "key": "parent",
            "subattributes": [{"key": "old", "value": 1, "deleted": True}],
        },
        {
            "key": "parent",
            "subattributes": [{"key": "old", "value": 2, "deleted": True}],
        },
    ]
    replacement = [
        {"key": "parent", "subattributes": [{"key": "new", "value": 3}]},
        {"key": "parent", "subattributes": []},
    ]
    merged = util.preserve_retired_attributes(previous, replacement)
    assert merged[0]["subattributes"][-1]["value"] == 1
    assert merged[1]["subattributes"][-1]["value"] == 2
    assert util.preserve_retired_attributes(previous, merged) == merged


@pytest.mark.parametrize("existing_occurrences", [0, 1, 2])
def test_preservation_keeps_identical_retired_occurrences(existing_occurrences):
    retired = {
        "key": "material",
        "value": "cement",
        "deleted": True,
        "subattributes": [{"key": "source", "value": "original"}],
    }
    previous = [copy.deepcopy(retired) for _ in range(2)]
    replacement = [
        {"key": "first", "value": 1},
        *[copy.deepcopy(retired) for _ in range(existing_occurrences)],
        {"key": "last", "value": 2},
    ]
    original_inputs = copy.deepcopy((previous, replacement))

    merged = util.preserve_retired_attributes(previous, replacement)

    assert merged == replacement + [retired] * (2 - existing_occurrences)
    assert util.preserve_retired_attributes(previous, merged) == merged
    assert (previous, replacement) == original_inputs


def test_preservation_keeps_identical_retired_children_in_repeated_parents():
    retired = {"key": "material", "value": "cement", "deleted": True}
    previous = [
        {"key": "parent", "subattributes": [copy.deepcopy(retired) for _ in range(2)]}
        for _ in range(2)
    ]
    replacement = [
        {"key": "parent", "subattributes": [{"key": "depth", "value": 1}]},
        {"key": "parent", "subattributes": [copy.deepcopy(retired)]},
    ]
    original_inputs = copy.deepcopy((previous, replacement))

    merged = util.preserve_retired_attributes(previous, replacement)

    assert merged == [
        {
            "key": "parent",
            "subattributes": [{"key": "depth", "value": 1}, retired, retired],
        },
        {"key": "parent", "subattributes": [retired, retired]},
    ]
    assert util.preserve_retired_attributes(previous, merged) == merged
    assert (previous, replacement) == original_inputs


def test_internal_record_replacement_preserves_identical_retired_occurrences(
    retirement_manager,
):
    manager = retirement_manager
    retired = {"key": "material", "value": "cement", "deleted": True}
    record_id = insert_record(manager, [copy.deepcopy(retired) for _ in range(2)])
    replacement = {"attributesList": [{"key": "depth", "value": 42}]}

    for _ in range(2):
        manager.updateRecord(
            str(record_id),
            replacement,
            update_type="record",
            forceUpdate=True,
            user_info=USER,
            calling_function="batch_process_document",
        )
        stored = manager.db.records.find_one({"_id": record_id})["attributesList"]
        assert [field for field in stored if field.get("deleted")] == [retired, retired]
        assert stored[0]["key"] == "depth" and stored[0]["value"] == 42


def test_import_preserves_deleted_marker(retirement_manager):
    result = retirement_manager._normalizeImportedAttribute(
        {
            "key": "old",
            "value": 123,
            "deleted": True,
            "subattributes": [{"key": "child", "deleted": True, "value": "raw"}],
        },
        0,
    )
    assert result["deleted"] and result["subattributes"][0]["deleted"]


def test_successful_edit_updates_revision_and_keeps_retired_values(retirement_manager):
    manager = retirement_manager
    record_id = insert_record(
        manager,
        [{"key": "depth", "value": 1}, {"key": "old", "value": 42, "deleted": True}],
    )
    record = manager._reconcileRecord(
        manager.db.records.find_one({"_id": record_id}), user=USER
    )
    response = manager.updateRecord(
        str(record_id),
        {"indexes": [0], "v": {**record["attributesList"][0], "value": 2}},
        "attribute",
        user_info=USER,
        calling_function="update_record",
        expected_attribute_revision=record["attribute_revision"],
    )
    assert response["attribute_revision"] != record["attribute_revision"]
    assert response["attributesList.0"]["value"] == 2
    assert (
        manager.db.records.find_one({"_id": record_id})["attributesList"][1]
        == record["attributesList"][1]
    )


def test_record_fetch_persists_layout_without_background_write(
    retirement_manager, monkeypatch
):
    manager = retirement_manager
    monkeypatch.setattr(manager, "getRecordIndexes", lambda *args: None)
    record_id = insert_record(
        manager, [{"key": "unknown", "value": 4}, {"key": "depth", "value": 8}]
    )
    manager.db.records.update_one({"_id": record_id}, {"$unset": {"filename": ""}})
    tasks = Mock()
    record, locked = manager.fetchRecordData(
        str(record_id), USER, background_tasks=tasks
    )
    stored = manager.db.records.find_one({"_id": record_id})
    assert not locked
    assert record["attributesList"] == stored["attributesList"]
    assert record["attribute_revision"] == stored["attribute_revision"]
    assert record["attributesList"][0]["key"] == "depth"
    tasks.add_task.assert_not_called()


def test_schema_replacement_retains_removed_definitions_and_unknown_fields(
    retirement_manager,
):
    manager = retirement_manager
    record_id = insert_record(
        manager, [{"key": "depth", "value": 12}, {"key": "unseen", "value": 5}]
    )
    processor = manager.db.processors.find_one()
    manager._saveProcessorChanges(
        processor,
        {"attributes": [{**FIELD, "name": "other"}]},
        USER,
        "test replacement",
    )
    manager._ensureRecordGroupsReconciled([GROUP], USER)
    record = manager.db.records.find_one({"_id": record_id})
    fields = {field["key"]: field for field in record["attributesList"]}
    assert fields["depth"]["value"] == 12 and fields["depth"]["deleted"]
    assert fields["unseen"]["value"] == 5 and not fields["unseen"].get("deleted")


def test_nested_retirement_hides_every_instance_without_modifying_children(
    retirement_manager,
):
    manager = retirement_manager
    parent = {"name": "parent", "data_type": "Parent", "database_data_type": "Table"}
    child = {**FIELD, "name": "parent::child"}
    manager.db.processors.update_one({}, {"$set": {"attributes": [parent, child]}})
    record_id = insert_record(
        manager,
        [
            {
                "key": "parent",
                "subattributes": [
                    {"key": "child", "value": value},
                    {"key": "new_child", "value": "keep"},
                ],
            }
            for value in (1, 2)
        ],
    )
    manager.updateProcessorAttribute("well", "parent::child", {}, USER, "delete")
    manager._ensureRecordGroupsReconciled([GROUP], USER)
    record = manager.db.records.find_one({"_id": record_id})
    for parent_attribute, value in zip(record["attributesList"], (1, 2)):
        children = {field["key"]: field for field in parent_attribute["subattributes"]}
        assert children["child"]["deleted"] and children["child"]["value"] == value
        assert not children["new_child"].get("deleted")


def test_migrated_embedded_schema_retirement_preserves_last_field(retirement_manager):
    from ogrre.migrate_schema_bindings import migrate_schema_bindings

    manager = retirement_manager
    manager.db.record_groups.update_one(
        {}, {"$unset": {"processorId": "", "schema_id": ""}}
    )
    record_id = insert_record(manager, [{"key": "depth", "value": 12}])
    report = migrate_schema_bindings(manager.db, apply=True)
    schema_id = report["changes"][0]["schema_id"]
    manager.updateProcessorAttribute(
        None, "depth", {}, USER, "delete", schema_id=schema_id
    )
    manager._ensureRecordGroupsReconciled([GROUP], USER)
    field = manager.db.records.find_one({"_id": record_id})["attributesList"][0]
    assert field["deleted"] and field["value"] == 12
    assert manager.getRecordGroupSchemaAttributes(GROUP, USER) == []


def test_missing_processor_does_not_apply_stale_embedded_schema(retirement_manager):
    manager = retirement_manager
    manager.db.processors.delete_many({})
    record_id = insert_record(manager, [{"key": "unknown", "value": 12}])
    manager._ensureRecordGroupsReconciled([GROUP], USER)
    record = manager.db.records.find_one({"_id": record_id})
    assert len(record["attributesList"]) == 1
    assert not record["attributesList"][0].get("deleted")


def test_cleaning_cannot_overwrite_concurrent_edit(retirement_manager, monkeypatch):
    manager = retirement_manager
    record_id = insert_record(manager, [{"key": "depth", "value": 1}])
    clean = util.cleanRecords

    def concurrent_edit(schema, records):
        manager.db.records.update_one(
            {"_id": record_id}, {"$set": {"attributesList.0.value": 999}}
        )
        return clean(schema, records)

    monkeypatch.setattr(util, "cleanRecords", concurrent_edit)
    with pytest.raises(SchemaError) as error:
        manager.cleanCollection("record_group", GROUP, USER)
    assert error.value.status_code == 409
    assert (
        manager.db.records.find_one({"_id": record_id})["attributesList"][0]["value"]
        == 999
    )


def test_api_requires_current_revision_and_returns_next_revision(
    client, retirement_manager
):
    manager = retirement_manager
    record_id = insert_record(manager, [{"key": "depth", "value": 1}])
    record = manager._reconcileRecord(
        manager.db.records.find_one({"_id": record_id}), user=USER
    )
    payload = {
        "type": "attribute",
        "data": {"indexes": [0], "v": {**record["attributesList"][0], "value": 2}},
    }
    assert client.post(f"/update_record/{record_id}", json=payload).status_code == 409
    payload["attribute_revision"] = record["attribute_revision"]
    response = client.post(f"/update_record/{record_id}", json=payload)
    assert response.status_code == 200
    assert response.json()["attribute_revision"] != payload["attribute_revision"]
    assert client.post(f"/update_record/{record_id}", json=payload).status_code == 409


def test_schema_change_during_edit_does_not_return_a_different_target(
    retirement_manager, monkeypatch
):
    manager = retirement_manager
    record_id = insert_record(manager, [{"key": "depth", "value": 1}])
    record = manager._reconcileRecord(
        manager.db.records.find_one({"_id": record_id}), user=USER
    )

    def retire_while_cleaning(*args, **kwargs):
        manager.db.processors.update_one({}, {"$set": {"attributes.0.deleted": True}})

    monkeypatch.setattr(manager, "cleanAttribute", retire_while_cleaning)
    with pytest.raises(SchemaError) as error:
        manager.updateRecord(
            str(record_id),
            {"indexes": [0], "v": {**record["attributesList"][0], "value": 2}},
            "attribute",
            field_to_clean=True,
            user_info=USER,
            calling_function="update_record",
            expected_attribute_revision=record["attribute_revision"],
        )
    assert error.value.status_code == 409
    assert (
        manager.db.records.find_one({"_id": record_id})["attributesList"][0]["value"]
        == 1
    )


@pytest.fixture
def query_manager(retirement_manager):
    uri = os.getenv("OGRRE_TEST_MONGO_URI")
    if not uri:
        pytest.skip(
            "Set OGRRE_TEST_MONGO_URI to a disposable local MongoDB for query tests."
        )
    assert urlparse(uri).hostname in {"localhost", "127.0.0.1"}
    client = MongoClient(uri, serverSelectionTimeoutMS=3000)
    db_name = "ogrre_schema_test_" + uuid.uuid4().hex
    db = client[db_name]
    for collection in ("record_groups", "processors"):
        db[collection].insert_many(list(retirement_manager.db[collection].find()))
    retirement_manager.db = db
    try:
        yield retirement_manager
    finally:
        client.drop_database(db_name)
        client.close()


def test_unvisited_retirement_controls_filters_counts_stats_and_exports(
    query_manager, tmp_path
):
    manager = query_manager
    retired = {**FIELD, "name": "old"}
    manager.db.processors.update_one({}, {"$set": {"attributes": [FIELD, retired]}})
    for value in (1, 2, 3):
        insert_record(
            manager,
            [
                {"key": "depth", "value": value},
                {"key": "old", "value": "secret", "cleaning_error": "old error"},
            ],
            str(value),
        )
    manager.updateProcessorAttribute("well", "old", {}, USER, "delete")
    query = {
        "record_group_id": GROUP,
        "attributesList": {"$elemMatch": {"key": "old", "value": "secret"}},
    }
    rows, count = manager.fetchRecords(filter_by=query)
    assert rows == [] and count == 0
    rows, count = manager.fetchRecords(
        filter_by={"record_group_id": GROUP},
        sort_by=["attributesList.depth", 1],
        page=0,
        records_per_page=2,
    )
    assert count == 3 and len(rows) == 2
    assert [row["attributesList"][0]["value"] for row in rows] == [1, 2]
    assert manager.getRecordGroupProgress([GROUP])[GROUP]["error_amt"] == 0
    assert manager.deriveRecordColumnsFromRecordGroups([GROUP]) == ["depth"]
    for export_type in ("csv", "json"):
        path = manager.downloadRecords(
            rows, export_type, USER, GROUP, "record_group", keep_all_columns=True
        )
        text = open(path).read()
        assert "secret" not in text and "old" not in text
    stored = manager.db.records.find_one()
    assert (
        stored["attributesList"][1]["deleted"]
        and stored["attributesList"][1]["value"] == "secret"
    )


def test_recursive_query_prunes_retired_ancestors_and_data_fusion_is_inert(
    query_manager,
):
    manager = query_manager
    manager.db.record_groups.update_one({}, {"$set": {"data_fusion": ["unused"]}})
    insert_record(
        manager,
        [
            {"key": "depth", "value": 8},
            {
                "key": "parent",
                "deleted": True,
                "subattributes": [{"key": "child", "value": "secret"}],
            },
        ],
    )
    rows, count = manager.fetchRecords(filter_by={"record_group_id": GROUP})
    assert count == 1 and rows[0]["attributesList"][0]["value"] == 8
    rows, count = manager.fetchRecords(
        filter_by={
            "record_group_id": GROUP,
            "attributesList.subattributes.value": "secret",
        }
    )
    assert not rows and count == 0


def test_filtered_delete_ignores_retired_values_and_archives_full_records(
    query_manager, monkeypatch
):
    manager = query_manager
    monkeypatch.setattr(manager, "_moveDeletedRecordImages", lambda record: None)
    record_id = insert_record(
        manager,
        [
            {"key": "depth", "value": 8},
            {"key": "old", "value": "secret", "deleted": True},
        ],
    )
    manager.deleteRecordsByRecordGroup(GROUP, {"attributesList.value": "secret"}, USER)
    assert manager.db.records.find_one({"_id": record_id})
    manager.deleteRecordsByRecordGroup(GROUP, {"attributesList.value": 8}, USER)
    assert manager.db.records.find_one({"_id": record_id}) is None
    archived = manager.db.deleted_records.find_one()
    assert archived["attributesList"][1]["value"] == "secret"
    assert archived["attributesList"][1]["deleted"]


def test_export_does_not_create_placeholder_for_readded_retired_path(query_manager):
    manager = query_manager
    insert_record(manager, [{"key": "depth", "value": 12, "deleted": True}])
    records, _ = manager.fetchRecords(filter_by={"record_group_id": GROUP})
    assert records[0]["attributesList"] == []
    path = manager.downloadRecords(
        records, "json", USER, GROUP, "record_group", keep_all_columns=True
    )
    with open(path) as output:
        assert "depth" not in output.read()
