"""Unavailable schema bindings must not block access to existing records."""

import copy
import csv
from unittest.mock import Mock

import pytest
from bson import ObjectId

from ogrre.internal import util
from ogrre.internal.schema_validation import SchemaError
from ogrre.tests.test_attribute_retirement import query_manager, retirement_manager
from ogrre.tests.test_schema_management import (
    FIELD,
    GROUP,
    PROCESSOR,
    USER,
    client,
    schema_manager,
)


@pytest.mark.parametrize("database_mode", [False, True])
def test_complete_read_path_works_in_both_schema_modes(
    query_manager, monkeypatch, database_mode
):
    from ogrre.internal import data_manager

    manager = query_manager
    manager.hasPermission.side_effect = None
    manager.hasPermission.return_value = True
    monkeypatch.setattr(data_manager, "USE_DB_PROCESSORS", database_mode)
    monkeypatch.setattr(data_manager, "get_document_image", lambda *args: "test-image")

    repo_processor = {
        "Processor Name": PROCESSOR["name"],
        "Processor ID": PROCESSOR["processorId"],
        "Model ID": PROCESSOR["modelId"],
        "displayName": PROCESSOR["displayName"],
        "documentType": PROCESSOR["documentType"],
        "attributes": copy.deepcopy(PROCESSOR["attributes"]),
    }
    monkeypatch.setattr(
        data_manager.processor_api,
        "get_processor_list",
        lambda collaborator: [copy.deepcopy(repo_processor)],
    )
    monkeypatch.setattr(
        data_manager.processor_api,
        "get_processor_by_id",
        lambda collaborator, processor_id: copy.deepcopy(repo_processor)
        if collaborator == "isgs" and processor_id == PROCESSOR["processorId"]
        else None,
    )

    schema_id = manager.db.processors.find_one()["_id"]
    valid_group_id = GROUP
    schema_less_group_id = str(ObjectId())
    unavailable_group_id = str(ObjectId())
    manager.db.record_groups.update_one(
        {"_id": ObjectId(valid_group_id)},
        {
            "$set": {
                "name": "Valid group",
                "processorId": PROCESSOR["processorId"],
                "schema_id": str(schema_id),
            }
        },
    )
    manager.db.record_groups.insert_many(
        [
            {
                "_id": ObjectId(schema_less_group_id),
                "name": "Schema-less group",
                "processorId": None,
                "schema_id": None,
            },
            {
                "_id": ObjectId(unavailable_group_id),
                "name": "Unavailable group",
                "processorId": "missing-processor",
                "schema_id": str(ObjectId()),
            },
        ]
    )
    group_ids = [valid_group_id, schema_less_group_id, unavailable_group_id]
    project_id = manager.db.projects.insert_one(
        {"name": "Loading project", "record_groups": group_ids}
    ).inserted_id
    manager.db.teams.insert_one(
        {"name": "team", "project_list": [project_id], "users": [USER["email"]]}
    )
    monkeypatch.setattr(manager, "getUserProjectList", lambda *args: [project_id])
    monkeypatch.setattr(manager, "getUserRecordGroups", lambda *args: group_ids)
    monkeypatch.setattr(manager, "getDefaultTeamForUser", lambda *args: "team")
    monkeypatch.setattr(
        manager,
        "getProjectFromRecordGroup",
        lambda *args: manager.fetchProject(str(project_id)),
    )

    attribute_trees = [
        [{"key": "depth", "value": 1}],
        [None, {"key": "depth", "value": 2, "subattributes": [None]}],
        [{"key": "depth", "value": 3}],
    ]
    record_ids = []
    for index, (group_id, attributes) in enumerate(zip(group_ids, attribute_trees)):
        record_ids.append(
            manager.db.records.insert_one(
                {
                    "record_group_id": group_id,
                    "name": f"record-{index}",
                    "filename": f"record-{index}.json",
                    "dateCreated": index + 1,
                    "attributesList": attributes,
                }
            ).inserted_id
        )

    projects = manager.fetchProjects(USER)
    assert [project["name"] for project in projects] == ["Loading project"]
    project = manager.fetchRecordGroups(str(project_id), USER)
    assert {group["_id"] for group in project["record_groups"]} == set(group_ids)

    expected_schema = {
        valid_group_id: True,
        schema_less_group_id: False,
        unavailable_group_id: False,
    }
    for group_id in group_ids:
        _, group = manager.fetchRecordGroupData(group_id, USER)
        assert group["schema_source"] == ("database" if database_mode else "repo")
        assert group["has_schema"] is expected_schema[group_id]
        rows, count = manager.fetchRecordsByRecordGroup(USER, group_id, filter_by={})
        assert count == len(rows) == 1
        columns = manager.fetchColumnData("record_group", group_id, USER)["columns"]
        assert {"depth", "record_notes"} <= set(columns)

    for fetch in (
        lambda: manager.fetchRecordsByProject(USER, str(project_id), filter_by={}),
        lambda: manager.fetchRecordsByTeam(USER, filter_by={}),
    ):
        rows, count = fetch()
        assert count == len(rows) == 3

    for record_id in record_ids:
        record, locked = manager.fetchRecordData(str(record_id), USER)
        assert not locked
        assert record["record_group_id"] in group_ids
        assert record["attributesList"][0]["key"] == "depth"

    schema = manager.getSchema(USER)
    assert schema["source"] == ("database" if database_mode else "repo")
    assert schema["read_only"] is not database_mode
    assert len(schema["processors"]) == 1


@pytest.fixture(
    params=["repo", "deleted", "invalid", "legacy_missing", "ambiguous", "embedded"]
)
def unavailable_group(request, schema_manager, monkeypatch):
    from ogrre.internal import data_manager

    manager = schema_manager
    manager.hasPermission.side_effect = None
    manager.hasPermission.return_value = True
    monkeypatch.setattr(manager, "tryLockingRecord", lambda *args: True)
    monkeypatch.setattr(manager, "getDefaultTeamForUser", lambda *args: "team")
    monkeypatch.setattr(manager, "userCanAccessProject", lambda *args: True)
    monkeypatch.setattr(data_manager, "USE_DB_PROCESSORS", request.param != "repo")
    monkeypatch.setattr(data_manager, "get_document_image", lambda *args: "test-image")
    monkeypatch.setattr(
        data_manager.processor_api,
        "get_processor_by_id",
        lambda collaborator, processor_id: copy.deepcopy(PROCESSOR)
        if collaborator == "isgs" and processor_id == "extractor"
        else None,
    )
    schema_id = manager.db.record_groups.find_one()["schema_id"]
    valid_id = str(ObjectId())
    manager.db.record_groups.insert_one(
        {
            "_id": ObjectId(valid_id),
            "name": "Available group",
            "schema_id": schema_id,
            "processorId": "extractor",
        }
    )
    if request.param == "repo":
        update = {"$set": {"processorId": "unavailable"}}
    elif request.param in {"deleted", "invalid"}:
        update = {
            "$set": {
                "schema_id": str(ObjectId()) if request.param == "deleted" else "bad-id"
            }
        }
    else:
        update = {"$unset": {"schema_id": ""}}
        if request.param in {"legacy_missing", "ambiguous"}:
            update["$set"] = {"processorId": "unavailable"}
        if request.param == "ambiguous":
            manager.db.processors.insert_many(
                [
                    {**copy.deepcopy(PROCESSOR), "processorId": "unavailable"}
                    for _ in range(2)
                ]
            )
    manager.db.record_groups.update_one({"_id": ObjectId(GROUP)}, update)
    group_ids = [valid_id, GROUP]
    monkeypatch.setattr(manager, "getUserRecordGroups", lambda *args: group_ids)
    project_id = manager.db.projects.insert_one(
        {"name": "Mixed project", "record_groups": group_ids}
    ).inserted_id
    monkeypatch.setattr(
        manager,
        "getProjectFromRecordGroup",
        lambda *args: manager.fetchProject(str(project_id)),
    )
    attributes = util.normalize_record_attribute_tree(
        [
            {"key": "depth", "value": 42, "raw_text": "042"},
            {"key": "parent", "subattributes": [{"key": "child", "value": "keep"}]},
            {"key": "retired", "value": "hidden", "deleted": True},
        ]
    )
    records = []
    for index in range(3):
        record = {
            "record_group_id": GROUP,
            "filename": f"record-{index}.json",
            "attributesList": copy.deepcopy(attributes),
        }
        record["_id"] = manager.db.records.insert_one(record).inserted_id
        records.append(record)
    # Query pipelines have separate Mongo tests; prepare only the opened record.
    monkeypatch.setattr(manager.db.records, "aggregate", Mock(return_value=[]))
    monkeypatch.setattr(
        manager,
        "getRecordIndexes",
        Mock(return_value=None),
    )
    return manager, str(project_id), records


def test_unavailable_schema_allows_project_group_record_columns_and_export(
    unavailable_group,
):
    manager, project_id, records = unavailable_group
    original_group = manager.db.record_groups.find_one({"_id": ObjectId(GROUP)})
    result = manager.fetchRecordGroups(project_id, USER)
    assert {group["_id"] for group in result["record_groups"]} == set(
        result["project"]["record_groups"]
    )
    _, group = manager.fetchRecordGroupData(GROUP, USER)
    assert group["has_schema"] is False and group["can_process"] is False
    assert group["active_schema_id"] is None and group["schema_name"] is None
    record, _ = manager.fetchRecordData(
        str(records[0]["_id"]),
        USER,
        page_state={"location": "project", "group_id": project_id},
    )
    assert record["has_schema"] is False
    assert record["attributesList"] == records[0]["attributesList"]
    for location, scope in [("project", project_id), ("record_group", GROUP)]:
        columns = manager.fetchColumnData(location, scope, USER)["columns"]
        assert {"depth", "parent::child", "record_notes"} <= set(columns)
        assert "retired" not in columns
    grouped = manager.organizeRecordsByDocumentType(records)
    assert grouped == {"Group": records}
    path = manager.downloadRecords(
        records, "csv", USER, project_id, "project", keep_all_columns=True
    )
    with open(path, newline="") as exported:
        rows = list(csv.DictReader(exported))
    assert [row["file"] for row in rows] == [record["filename"] for record in records]
    assert [row["depth"] for row in rows] == ["42"] * 3
    assert "retired" not in rows[0]
    assert manager.db.record_groups.find_one({"_id": ObjectId(GROUP)}) == original_group
    for original in records:
        stored = manager.db.records.find_one({"_id": original["_id"]})
        assert stored["attributesList"] == original["attributesList"]


def test_unavailable_schema_allows_record_edits_but_blocks_processing(
    unavailable_group,
):
    manager, _, records = unavailable_group
    record, _ = manager.fetchRecordData(str(records[0]["_id"]), USER)
    manager.updateRecord(
        record["_id"],
        {"indexes": [0], "v": {**record["attributesList"][0], "value": 43}},
        update_type="attribute",
        user_info=USER,
        calling_function="update_record",
        expected_attribute_revision=record["attribute_revision"],
    )
    stored = manager.db.records.find_one({"_id": records[0]["_id"]})
    assert stored["attributesList"][0]["value"] == 43
    assert stored["attributesList"][1:] == records[0]["attributesList"][1:]
    with pytest.raises(SchemaError):
        manager.getRecordGroupProcessingConfig(GROUP, USER)
    with pytest.raises(SchemaError):
        manager.cleanCollection("record_group", GROUP, USER)
    with pytest.raises(SchemaError):
        manager.previewRecordGroupSchema(GROUP, {"mode": "generate"}, USER)


def test_collaborator_switch_restores_schema_without_changing_binding(
    schema_manager, monkeypatch
):
    from ogrre.internal import data_manager

    manager = schema_manager
    monkeypatch.setattr(data_manager, "USE_DB_PROCESSORS", False)
    monkeypatch.setattr(
        data_manager.processor_api,
        "get_processor_by_id",
        lambda collaborator, processor_id: copy.deepcopy(PROCESSOR)
        if collaborator == "isgs"
        else None,
    )
    manager.db.record_groups.update_one({}, {"$set": {"processorId": "extractor"}})
    group = manager.db.record_groups.find_one()
    record_id = manager.db.records.insert_one(
        {"record_group_id": GROUP, "attributesList": [{"key": "depth", "value": 7}]}
    ).inserted_id
    for collaborator, has_schema in [("isgs", True), ("osage", False), ("isgs", True)]:
        user = {**USER, "collaborator": collaborator}
        manager._ensureRecordGroupsReconciled([GROUP], user)
        assert manager._recordGroupSchemaInfo(group, user)["has_schema"] is has_schema
        stored = manager.db.records.find_one({"_id": record_id})
        assert stored["attributesList"][0]["value"] == 7
        assert not stored["attributesList"][0].get("deleted")
        assert manager.db.record_groups.find_one() == group


@pytest.mark.parametrize("database_mode", [False, True])
def test_new_invalid_bindings_remain_rejected(
    schema_manager, monkeypatch, database_mode
):
    from ogrre.internal import data_manager

    manager = schema_manager
    manager.hasPermission.side_effect = None
    manager.hasPermission.return_value = True
    monkeypatch.setattr(data_manager, "USE_DB_PROCESSORS", database_mode)
    monkeypatch.setattr(
        data_manager.processor_api, "get_processor_by_id", lambda *args: None
    )
    monkeypatch.setattr(manager, "userCanAccessProject", lambda *args: True)
    monkeypatch.setattr(manager, "getDefaultTeamForUser", lambda *args: "team")
    binding = (
        {"schema_id": str(ObjectId())}
        if database_mode
        else {"processorId": "unavailable"}
    )
    before = list(manager.db.record_groups.find())
    with pytest.raises(SchemaError):
        manager.updateRecordGroup(GROUP, binding, USER)
    with pytest.raises(SchemaError):
        manager.createRecordGroup(
            {"project_id": str(ObjectId()), "name": "Invalid", **binding}, USER
        )
    assert list(manager.db.record_groups.find()) == before


@pytest.mark.parametrize(
    "failure",
    [
        SchemaError("Unexpected validation failure"),
        RuntimeError("Database unavailable"),
    ],
)
def test_unrelated_failures_are_not_hidden(schema_manager, monkeypatch, failure):
    monkeypatch.setattr(schema_manager, "_schemaDocument", Mock(side_effect=failure))
    with pytest.raises(type(failure), match=str(failure)):
        schema_manager.resolveRecordGroupSchema(
            schema_manager.db.record_groups.find_one(), USER
        )


def test_project_route_does_not_resolve_schemas(client, schema_manager, monkeypatch):
    from ogrre.internal import data_manager
    from ogrre.routers import router

    manager = schema_manager
    manager.collaborator = "osage"
    monkeypatch.setattr(data_manager, "USE_DB_PROCESSORS", False)
    monkeypatch.setattr(
        manager,
        "resolveRecordGroupSchema",
        Mock(side_effect=AssertionError("Project loading must not resolve schemas")),
    )
    manager.db.record_groups.update_one({}, {"$set": {"processorId": "extractor"}})
    project_id = manager.db.projects.insert_one(
        {"name": "Project", "record_groups": [GROUP]}
    ).inserted_id
    record_id = manager.db.records.insert_one(
        {"record_group_id": GROUP, "attributesList": [{"key": "depth", "value": 7}]}
    ).inserted_id
    monkeypatch.setattr(manager.db.records, "aggregate", Mock(return_value=[]))
    client.app.dependency_overrides[router.authenticate] = lambda: {
        **USER,
        "collaborator": "isgs",
    }
    response = client.get(f"/get_record_groups/{project_id}")
    assert response.status_code == 200
    assert len(response.json()["record_groups"]) == 1
    assert manager.db.records.find_one({"_id": record_id})["attributesList"] == [
        {"key": "depth", "value": 7}
    ]
