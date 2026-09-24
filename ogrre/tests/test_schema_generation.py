"""Schema generation is bounded, explicit, additive, and retryable."""

import copy
import os
import uuid
from unittest.mock import Mock

import pytest
from bson import BSON, ObjectId

from ogrre.internal import schema_inference as inference
from ogrre.internal.schema_validation import SchemaError
from ogrre.tests.test_schema_management import GROUP, USER, client, schema_manager


def attr(key, value=None, **kwargs):
    return {"key": key, "value": value, **kwargs}


@pytest.mark.parametrize(
    "values, expected",
    [
        ([True, False], ("Checkbox", "bool")),
        ([1, 3], ("Number", "int")),
        ([1, 3.5], ("Number", "float")),
        (["12", "3.5"], ("Number", "float")),
        (["0012", "3"], ("Plain text", "str")),
        (["2024-01-02", "03/04/2024"], ("Plain text", "str")),
        ([None, ""], ("Plain text", "str")),
        ([True, 2, "text"], ("Plain text", "str")),
    ],
)
def test_conservative_types_do_not_change_values(values, expected):
    records = [{"attributesList": [attr("value", value)]} for value in values]
    original = copy.deepcopy(records)
    fields, notes, warnings = inference.infer_fields(records)
    assert (fields[0]["data_type"], fields[0]["database_data_type"]) == expected
    assert records == original
    assert "cleaning_function" not in fields[0]
    if expected == ("Plain text", "str"):
        assert notes["value"]


def test_nested_repeated_and_retired_fields():
    records = [
        {
            "attributesList": [
                None,
                attr("retired", "hidden", deleted=True),
                attr(
                    "hidden_parent",
                    subattributes=[attr("child", "hidden")],
                    deleted=True,
                ),
                attr("layers", subattributes=[attr("depth", 1)]),
                attr("layers", subattributes=[attr("depth", 2.5)]),
                attr("unvisited_retired", "hidden"),
            ]
        }
    ]
    fields, _, _ = inference.infer_fields(
        records, [{"name": "unvisited_retired", "deleted": True}]
    )
    assert [field["name"] for field in fields] == ["layers", "layers::depth"]
    assert fields[0]["database_data_type"] == "Table"
    assert fields[0]["occurrence"] == "Optional multiple"
    assert fields[1]["database_data_type"] == "float"


def test_legacy_subattributes_use_the_same_paths_as_record_consumers():
    fields, _, _ = inference.infer_fields(
        [
            {
                "attributesList": [
                    attr("depth", 12, isSubattribute=True, parentAttribute="layers"),
                    attr(
                        "layers::color",
                        "red",
                        isSubattribute=True,
                        topLevelAttribute="layers",
                    ),
                ]
            }
        ]
    )
    assert [field["name"] for field in fields] == [
        "layers",
        "layers::depth",
        "layers::color",
    ]
    assert fields[0]["data_type"] == "Parent"


def test_inference_limits_report_partial_coverage(monkeypatch):
    monkeypatch.setattr(inference, "MAX_FIELDS", 2)
    fields, _, warnings = inference.infer_fields(
        [{"attributesList": [attr(str(i), i) for i in range(4)]}]
    )
    assert len(fields) == 2 and any("2 fields" in warning for warning in warnings)
    monkeypatch.setattr(inference, "MAX_DEPTH", 1)
    fields, _, warnings = inference.infer_fields(
        [{"attributesList": [attr("parent", subattributes=[attr("child", 1)])]}]
    )
    assert len(fields) == 1 and any("nesting" in warning for warning in warnings)
    monkeypatch.setattr(inference, "MAX_NODES", 1)
    _, _, warnings = inference.infer_fields(
        [{"attributesList": [attr("a"), attr("b")]}]
    )
    assert any("attribute instances" in warning for warning in warnings)


class SampleCursor(list):
    def close(self):
        pass


@pytest.fixture
def generation(schema_manager, monkeypatch):
    manager = schema_manager
    monkeypatch.setenv("SCHEMA_INFERENCE_MAX_RECORDS", "1000")
    manager.db.record_groups.update_one(
        {"_id": ObjectId(GROUP)},
        {"$set": {"schema_id": None}, "$unset": {"attributes": ""}},
    )
    manager.db.records.insert_one(
        {"record_group_id": GROUP, "attributesList": [attr("depth", 12)]}
    )
    monkeypatch.setattr(manager, "getDefaultTeamForUser", lambda *args: "source-team")
    # Mongomock lacks $bsonSize; emulate just the server projection here. The
    # native Mongo test below exercises the full pipeline and index hint.
    def aggregate(pipeline, **kwargs):
        records = (
            manager.db.records.find(pipeline[0]["$match"])
            .sort("_id", 1)
            .limit(pipeline[2]["$limit"])
        )
        return SampleCursor(
            {
                "_id": record["_id"],
                "size": len(BSON.encode(record)),
                "attributesList": record.get("attributesList", [])
                if len(BSON.encode(record)) <= inference.MAX_RECORD_BYTES
                else [],
            }
            for record in records
        )

    monkeypatch.setattr(manager.db.records, "aggregate", Mock(side_effect=aggregate))
    return manager


def preview(manager, mode="generate", **kwargs):
    return manager.previewRecordGroupSchema(GROUP, {"mode": mode, **kwargs}, USER)


def request_for(result):
    return {
        "preview_id": result["preview_id"],
        "fields": copy.deepcopy(result["fields"]),
        **(
            {"name": result["name"], "documentType": result["documentType"]}
            if result["mode"] == "generate"
            else {}
        ),
    }


def test_generate_attaches_shared_processor_free_schema_without_record_changes(
    generation,
):
    manager = generation
    before = list(manager.db.records.find())
    result = preview(manager)
    assert result["sampled_records"] == 1
    assert manager.db.record_groups.find_one()["schema_id"] is None
    assert manager.db.processors.count_documents({}) == 1
    request = request_for(result)
    request["fields"][0]["alias"] = "Depth"
    applied = manager.applyRecordGroupSchema(GROUP, request, USER)
    assert applied["has_schema"] and not applied["can_process"]
    schema = manager.db.processors.find_one({"_id": ObjectId(applied["schema_id"])})
    assert schema["created_by_team"] == "source-team"
    assert schema["inference_source"]["sampled_records"] == 1
    assert schema["attributes"][0]["alias"] == "Depth"
    assert not schema.get("processorId") and not schema.get("modelId")
    assert list(manager.db.records.find()) == before
    assert (
        manager.applyRecordGroupSchema(GROUP, request, USER)["schema_id"]
        == applied["schema_id"]
    )
    assert manager.db.history.count_documents({"action": "generateSchema"}) == 1
    manager.db.records.aggregate.assert_called_with(
        inference.sample_pipeline(GROUP, 1000),
        hint=inference.INDEX,
        maxTimeMS=5000,
        batchSize=10,
    )


def test_extension_only_adds_new_fields_and_respects_unmaterialized_retirement(
    generation,
):
    manager = generation
    schema = manager.db.processors.find_one()
    old_fields = [
        {
            "name": "layers",
            "data_type": "Parent",
            "database_data_type": "Table",
            "page_order_sort": 1,
        },
        {
            "name": "retired",
            "deleted": True,
            "data_type": "Plain text",
            "database_data_type": "str",
        },
    ]
    manager.db.processors.update_one({}, {"$set": {"attributes": old_fields}})
    manager.db.record_groups.update_one({}, {"$set": {"schema_id": str(schema["_id"])}})
    manager.db.records.update_one(
        {},
        {
            "$set": {
                "attributesList": [
                    attr("layers", subattributes=[attr("new", 3)]),
                    attr("retired", "keep hidden"),
                ]
            }
        },
    )
    result = preview(manager, "extend")
    assert [field["name"] for field in result["fields"]] == ["layers::new"]
    manager.applyRecordGroupSchema(GROUP, request_for(result), USER)
    assert manager.db.processors.find_one()["attributes"][:2] == old_fields
    assert preview(manager, "extend")["fields"] == []


@pytest.mark.parametrize("change", ["group", "schema", "record", "expired"])
def test_stale_previews_cannot_apply(generation, change):
    manager = generation
    result = preview(manager)
    if change == "group":
        manager.db.record_groups.update_one(
            {}, {"$set": {"schema_id": str(manager.db.processors.find_one()["_id"])}}
        )
    elif change == "schema":
        result = preview(manager)
        manager.db.record_groups.update_one(
            {}, {"$set": {"schema_id": str(manager.db.processors.find_one()["_id"])}}
        )
        result = preview(manager, "extend")
        manager.db.processors.update_one({}, {"$set": {"displayName": "Changed"}})
    elif change == "record":
        manager.db.records.update_one({}, {"$set": {"attributesList.0.value": 99}})
    else:
        manager.db.schema_generations.update_one({}, {"$set": {"expires_at": 0}})
    with pytest.raises(SchemaError) as error:
        manager.applyRecordGroupSchema(GROUP, request_for(result), USER)
    assert error.value.status_code == 409
    assert manager.db.processors.count_documents({}) == 1


def test_retry_after_schema_write_before_binding_does_not_duplicate(
    generation, monkeypatch
):
    manager = generation
    result = preview(manager)
    request = request_for(result)
    with monkeypatch.context() as patch:
        patch.setattr(
            manager.db.record_groups,
            "update_one",
            Mock(side_effect=RuntimeError("connection interrupted")),
        )
        with pytest.raises(RuntimeError):
            manager.applyRecordGroupSchema(GROUP, request, USER)
    assert manager.db.processors.count_documents({}) == 2
    assert manager.db.record_groups.find_one()["schema_id"] is None
    manager.applyRecordGroupSchema(GROUP, request, USER)
    assert manager.db.processors.count_documents({}) == 2
    assert manager.db.record_groups.find_one()["schema_id"] == result["preview_id"]


def test_concurrent_generation_previews_cannot_replace_each_other(generation):
    first, second = preview(generation), preview(generation)
    generation.applyRecordGroupSchema(GROUP, request_for(first), USER)
    with pytest.raises(SchemaError):
        generation.applyRecordGroupSchema(GROUP, request_for(second), USER)
    assert generation.db.processors.count_documents({}) == 2
    assert generation.db.record_groups.find_one()["schema_id"] == first["preview_id"]


def test_retry_never_recreates_an_archived_generated_schema(generation, monkeypatch):
    result = preview(generation)
    request = request_for(result)
    with monkeypatch.context() as patch:
        patch.setattr(
            generation.db.record_groups,
            "update_one",
            Mock(side_effect=RuntimeError("interrupted")),
        )
        with pytest.raises(RuntimeError):
            generation.applyRecordGroupSchema(GROUP, request, USER)
    query = {"_id": ObjectId(result["preview_id"])}
    generation.db.deleted_processors.insert_one(
        generation.db.processors.find_one(query)
    )
    generation.db.processors.delete_one(query)
    with pytest.raises(SchemaError, match="deleted"):
        generation.applyRecordGroupSchema(GROUP, request, USER)
    assert generation.db.processors.find_one(query) is None


def test_total_byte_budget_and_configured_record_limit(generation, monkeypatch):
    generation.db.records.insert_one(
        {"record_group_id": GROUP, "attributesList": [attr("later", 2)]}
    )
    first = generation.db.records.find_one()
    monkeypatch.setattr(inference, "MAX_SAMPLE_BYTES", len(BSON.encode(first)))
    result = preview(generation)
    assert result["sample_capped"] and result["sampled_records"] == 1
    monkeypatch.setenv("SCHEMA_INFERENCE_MAX_RECORDS", "1")
    assert preview(generation)["record_limit"] == 1
    with pytest.raises(SchemaError):
        preview(generation, record_limit=2)


def test_sampling_limits_and_oversized_records(generation, monkeypatch):
    manager = generation
    manager.db.records.insert_one(
        {"record_group_id": GROUP, "attributesList": [attr("later", 1)]}
    )
    result = preview(manager, record_limit=1)
    assert result["sample_capped"] and result["sampled_records"] == 1
    assert [field["name"] for field in result["fields"]] == ["depth"]
    monkeypatch.setattr(inference, "MAX_RECORD_BYTES", 1)
    result = preview(manager)
    assert result["oversized_records"] == 2 and not result["fields"]


def test_mode_permission_group_scope_empty_group_and_ownership(generation, monkeypatch):
    from ogrre.internal import data_manager

    manager = generation
    result = preview(manager)
    with pytest.raises(PermissionError):
        manager.applyRecordGroupSchema(
            GROUP, request_for(result), {"email": "other@example.com"}
        )
    with monkeypatch.context() as patch:
        patch.setattr(data_manager, "USE_DB_PROCESSORS", False)
        with pytest.raises(SchemaError):
            preview(manager)
        with pytest.raises(SchemaError):
            manager.applyRecordGroupSchema(GROUP, request_for(result), USER)
    with monkeypatch.context() as patch:
        patch.setattr(manager, "hasPermission", lambda *args: False)
        with pytest.raises(PermissionError):
            preview(manager)
    with monkeypatch.context() as patch:
        patch.setattr(manager, "getUserRecordGroups", lambda *args: [])
        with pytest.raises(PermissionError):
            preview(manager)
    manager.db.records.delete_many({})
    with pytest.raises(SchemaError, match="Add records"):
        preview(manager)


def test_routes_validate_payloads_and_apply(generation, client):
    path = f"/record_groups/{GROUP}/schema"
    assert client.post(path + "/preview", json={"mode": []}).status_code == 400
    assert (
        client.post(path + "/preview", json={"record_limit": 10001}).status_code == 400
    )
    assert client.post(path + "/preview", json={"filter": {}}).status_code == 400
    assert (
        client.post(
            path + "/apply",
            content='{"junk":"' + "x" * inference.MAX_PAYLOAD_BYTES + '"}',
        ).status_code
        == 413
    )
    result = client.post(path + "/preview", json={}).json()
    request = request_for(result)
    request["fields"][0]["deleted"] = True
    assert client.post(path + "/apply", json=request).status_code == 400
    request = request_for(result)
    request["fields"][0]["name"] = {"$ne": None}
    assert client.post(path + "/apply", json=request).status_code == 400
    assert client.post(path + "/apply", json=request_for(result)).status_code == 200


def test_native_mongo_sampling_uses_bounded_indexed_pipeline(generation):
    uri = os.getenv("OGRRE_TEST_MONGO_URI")
    if not uri:
        pytest.skip("Set OGRRE_TEST_MONGO_URI to a disposable local MongoDB.")
    if not uri.startswith(("mongodb://127.0.0.1:", "mongodb://localhost:")):
        pytest.skip("Schema-generation integration tests require a local MongoDB.")
    from pymongo import MongoClient

    client = MongoClient(uri, serverSelectionTimeoutMS=3000)
    db = client[f"ogrre_schema_generation_test_{uuid.uuid4().hex}"]
    try:
        db.records.create_index(
            [("record_group_id", 1), ("_id", 1)], name=inference.INDEX
        )
        db.records.insert_many(
            [
                {"record_group_id": GROUP, "attributesList": [attr("depth", 3)]},
                {
                    "record_group_id": GROUP,
                    "attributesList": [
                        attr("huge", "x" * (inference.MAX_RECORD_BYTES + 1))
                    ],
                },
                {
                    "record_group_id": "other",
                    "attributesList": [attr("not_in_sample", 9)],
                },
            ]
        )
        generation.db = db
        records, coverage = generation._sampleSchemaRecords(GROUP, 10)
        assert len(records) == 1 and coverage["oversized_records"] == 1
        assert coverage["examined_records"] == 2 and not coverage["sample_capped"]
        _, coverage = generation._sampleSchemaRecords(GROUP, 1)
        assert coverage["sample_capped"]
        db.record_groups.insert_one(
            {"_id": ObjectId(GROUP), "name": "Native sample", "schema_id": None}
        )
        stored = list(db.records.find())
        result = preview(generation)
        applied = generation.applyRecordGroupSchema(GROUP, request_for(result), USER)
        assert applied["has_schema"] and not applied["can_process"]
        assert list(db.records.find()) == stored
        assert (
            generation.applyRecordGroupSchema(GROUP, request_for(result), USER)[
                "schema_id"
            ]
            == applied["schema_id"]
        )
    finally:
        client.drop_database(db.name)
        client.close()
