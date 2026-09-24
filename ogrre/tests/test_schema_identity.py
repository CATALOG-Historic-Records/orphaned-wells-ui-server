import copy
import csv
import io
import json
from unittest.mock import Mock

import pytest
from bson import ObjectId
from fastapi import BackgroundTasks
from starlette.datastructures import UploadFile

from ogrre.internal import util
from ogrre.internal.schema_validation import SchemaError
from ogrre.migrate_schema_bindings import migrate_schema_bindings
from ogrre.tests.test_schema_management import (
    FIELD,
    GROUP,
    PROCESSOR,
    USER,
    client,
    schema_manager,
)
from ogrre.tests.test_attribute_retirement import query_manager, retirement_manager


@pytest.fixture
def admin(schema_manager, monkeypatch):
    schema_manager.hasPermission.side_effect = None
    schema_manager.hasPermission.return_value = True
    monkeypatch.setattr(
        schema_manager, "getDefaultTeamForUser", lambda *args: "creator-team"
    )
    return schema_manager


def create_schema(manager, name="Shared", **metadata):
    return manager.createSchema(
        {
            "name": name,
            "displayName": name,
            "documentType": "Imported",
            "attributes": [FIELD],
            **metadata,
        },
        USER,
    )


def test_processor_free_schema_has_identity_and_provenance(client, admin):
    response = client.post(
        "/create_schema", json={"name": "Empty", "documentType": "Imported"}
    )
    assert response.status_code == 200
    created = response.json()
    assert ObjectId.is_valid(created["schema_id"])
    assert created["attributes"] == [] and not created["can_process"]
    assert created["created_by"] == USER["email"]
    assert created["created_by_team"] == "creator-team"
    other_team = {"email": "other@example.com", "default_team": "other-team"}
    assert created["schema_id"] in [
        schema["schema_id"] for schema in admin.getSchema(other_team)["processors"]
    ]


def test_shared_schema_edits_apply_to_all_groups_without_copying_fields(
    admin, monkeypatch
):
    schema = create_schema(admin)
    second = str(ObjectId())
    admin.db.record_groups.insert_one(
        {"_id": ObjectId(second), "name": "Second", "schema_id": None}
    )
    monkeypatch.setattr(admin, "getUserRecordGroups", lambda user: [GROUP, second])
    for group_id in (GROUP, second):
        admin.updateRecordGroup(group_id, {"schema_id": schema["schema_id"]}, USER)
    admin.updateProcessorAttribute(
        None, "depth", {"alias": "Shared depth"}, USER, schema_id=schema["schema_id"]
    )
    for group_id in (GROUP, second):
        assert (
            admin.getRecordGroupSchemaAttributes(group_id, USER)[0]["alias"]
            == "Shared depth"
        )
    assert "attributes" not in admin.db.record_groups.find_one(
        {"_id": ObjectId(second)}
    )


def test_same_processor_can_back_distinct_schemas_and_metadata_changes_preserve_references(
    admin,
):
    first = create_schema(admin, "First", processorId="same", modelId="one")
    second = create_schema(admin, "Second", processorId="same", modelId="two")
    admin.connectRecordGroupProcessor(GROUP, None, USER, schema_id=second["schema_id"])
    assert admin.getRecordGroupProcessingConfig(GROUP, USER)["model_id"] == "two"
    with pytest.raises(SchemaError) as error:
        admin.getMongoProcessorByID("same")
    assert error.value.status_code == 409
    admin.updateProcessor(
        {
            "schema_id": second["schema_id"],
            "processorId": "new",
            "modelId": "new-model",
        },
        USER,
    )
    assert admin.db.record_groups.find_one()["schema_id"] == second["schema_id"]
    assert admin.getRecordGroupProcessingConfig(GROUP, USER)["model_id"] == "new-model"
    assert admin._schemaDocument(second["schema_id"])["attributes"] == [FIELD]
    assert admin._schemaDocument(first["schema_id"])["modelId"] == "one"


def test_safe_manager_can_create_schema_but_cannot_attach_processor_or_change_binding(
    schema_manager,
):
    schema = create_schema(schema_manager)
    with pytest.raises(PermissionError):
        schema_manager.updateProcessor(
            {"schema_id": schema["schema_id"], "processorId": "extractor"}, USER
        )
    with pytest.raises(PermissionError):
        schema_manager.updateRecordGroup(
            GROUP, {"schema_id": schema["schema_id"]}, USER
        )


def test_detach_preserves_records_and_suppresses_legacy_fallback(admin):
    admin.db.record_groups.update_one({}, {"$set": {"processorId": "extractor"}})
    attributes = [
        {"key": "depth", "value": 12},
        {"key": "old", "value": 20, "deleted": True},
    ]
    record_id = admin.db.records.insert_one(
        {"record_group_id": GROUP, "attributesList": attributes}
    ).inserted_id
    response = admin.updateRecordGroup(GROUP, {"schema_id": None}, USER)
    assert not response["has_schema"] and not response["can_process"]
    assert admin.getRecordGroupSchemaAttributes(GROUP, USER) == []
    stored = admin.db.records.find_one({"_id": record_id})["attributesList"]
    assert [attribute["value"] for attribute in stored] == [12, 20]
    assert stored[1]["deleted"]
    with pytest.raises(SchemaError):
        admin.getRecordGroupProcessingConfig(GROUP, USER)


@pytest.mark.parametrize("database_mode", [False, True])
@pytest.mark.parametrize(
    "document_type, expected_name",
    [("Imported wells", "Imported wells"), (None, "Group")],
)
def test_schema_less_csv_export_preserves_all_group_records(
    admin, monkeypatch, database_mode, document_type, expected_name
):
    from ogrre.internal import data_manager

    monkeypatch.setattr(data_manager, "USE_DB_PROCESSORS", database_mode)
    admin.db.record_groups.update_one(
        {"_id": ObjectId(GROUP)},
        {"$set": {"schema_id": None, "documentType": document_type}},
    )
    records = [
        {
            "_id": ObjectId(),
            "record_group_id": GROUP,
            "filename": f"well-{index}.json",
            "attributesList": [{"key": "depth", "value": index}],
        }
        for index in range(3)
    ]

    grouped = admin.organizeRecordsByDocumentType(records)

    assert grouped == {expected_name: records}
    assert admin.getProcessorByRecordGroupID(GROUP, user=USER) == (None, None, [])
    path = admin.downloadRecords(
        grouped[expected_name], "csv", USER, GROUP, "project", keep_all_columns=True
    )
    with open(path, newline="") as exported:
        rows = list(csv.DictReader(exported))
    assert [row["file"] for row in rows] == [record["filename"] for record in records]
    assert [row["depth"] for row in rows] == ["0", "1", "2"]


def test_attached_schema_export_name_uses_schema_name(admin):
    admin.db.record_groups.update_one({}, {"$set": {"documentType": "Group type"}})

    assert (
        admin.getProcessorByRecordGroupID(GROUP, returnNameOnly=True, user=USER)
        == PROCESSOR["name"]
    )


def test_repo_mode_ignores_mongo_and_embedded_fields(admin, monkeypatch):
    from ogrre.internal import data_manager

    monkeypatch.setattr(data_manager, "USE_DB_PROCESSORS", False)
    monkeypatch.setattr(
        data_manager.processor_api,
        "get_processor_by_id",
        lambda *args: {
            "Processor ID": "repo",
            "Model ID": "repo-model",
            "attributes": [{**FIELD, "name": "repo_only"}],
        },
    )
    group = admin.db.record_groups.find_one()
    assert admin.resolveRecordGroupSchema(group, USER) is None
    admin.db.record_groups.update_one({}, {"$set": {"processorId": "repo"}})
    assert [
        field["name"] for field in admin.getRecordGroupSchemaAttributes(GROUP, USER)
    ] == ["repo_only"]
    assert admin.getRecordGroupProcessingConfig(GROUP, USER)["model_id"] == "repo-model"
    with pytest.raises(SchemaError):
        create_schema(admin)


def test_missing_repo_configuration_never_pairs_old_id_with_default_model(
    admin, monkeypatch
):
    from ogrre.internal import data_manager

    monkeypatch.setattr(data_manager, "USE_DB_PROCESSORS", False)
    monkeypatch.setattr(
        data_manager.processor_api, "get_processor_by_id", lambda *args: None
    )
    admin.db.record_groups.update_one({}, {"$set": {"processorId": "missing"}})
    with pytest.raises(SchemaError) as error:
        admin.getRecordGroupProcessingConfig(GROUP, USER)
    assert error.value.status_code == 409
    default = data_manager.DEFAULT_PROCESSORS[0]
    admin.db.record_groups.update_one(
        {}, {"$set": {"processorId": default["Processor ID"]}}
    )
    config = admin.getRecordGroupProcessingConfig(GROUP, USER)
    assert (
        config["model_id"] == default["Model ID"] and config["using_default_processor"]
    )


def test_empty_mongo_catalog_does_not_offer_repo_default(admin):
    admin.db.processors.delete_many({})
    assert admin.fetchProcessors(USER)["processor_list"] == []


def test_new_groups_store_shared_identity_and_reject_conflicting_bindings(
    admin, monkeypatch
):
    project_id = str(admin.db.projects.insert_one({"name": "Project"}).inserted_id)
    monkeypatch.setattr(admin, "userCanAccessProject", lambda *args: True)
    schema = create_schema(admin)
    group_id = admin.createRecordGroup(
        {
            "name": "Selected schema",
            "project_id": project_id,
            "schema_id": schema["schema_id"],
        },
        USER,
    )
    group = admin.db.record_groups.find_one({"_id": ObjectId(group_id)})
    assert group["schema_id"] == schema["schema_id"]
    assert "attributes" not in group and "processorId" not in group
    with pytest.raises(SchemaError):
        admin.createRecordGroup(
            {
                "name": "Conflicting",
                "project_id": project_id,
                "schema_id": None,
                "processorId": "extractor",
            },
            USER,
        )
    assert admin.db.record_groups.count_documents({"name": "Conflicting"}) == 0


@pytest.mark.parametrize("include_schema", [False, True])
def test_json_import_preserves_supplied_schema_without_automatically_generating_one(
    admin, monkeypatch, include_schema
):
    project_id = str(admin.db.projects.insert_one({"name": "Project"}).inserted_id)
    monkeypatch.setattr(admin, "userCanAccessProject", lambda *args: True)
    package = {
        "records": [
            {"name": "Imported well", "attributesList": [{"key": "depth", "value": 12}]}
        ]
    }
    if include_schema:
        package["schema"] = {"documentType": "Well", "fields": [FIELD]}
    summary = admin.createRecordGroupFromJsonImport(
        project_id,
        {"record_group": {"name": "Import"}, "import_package": package},
        USER,
    )
    group = admin.db.record_groups.find_one(
        {"_id": ObjectId(summary["record_group_id"])}
    )
    assert summary["created_count"] == 1
    assert "attributes" not in group
    assert (
        admin.db.records.find_one({"record_group_id": str(group["_id"])})[
            "attributesList"
        ][0]["value"]
        == 12
    )
    if include_schema:
        schema = admin._schemaDocument(group["schema_id"])
        assert schema["attributes"] == [FIELD]
        assert schema["created_by_team"] == "creator-team"
        assert not schema.get("processorId")
    else:
        assert group["schema_id"] is None


def test_processor_free_schema_cleans_and_discovers_unknown_fields(admin, monkeypatch):
    cleaner = Mock(return_value=25)
    monkeypatch.setitem(util.CLEANING_FUNCTIONS, "identity_test_cleaner", cleaner)
    schema = create_schema(
        admin, attributes=[{**FIELD, "cleaning_function": "identity_test_cleaner"}]
    )
    admin.updateRecordGroup(GROUP, {"schema_id": schema["schema_id"]}, USER)
    record_id = admin.db.records.insert_one(
        {
            "record_group_id": GROUP,
            "attributesList": [
                {"key": "depth", "value": "25"},
                {"key": "new", "value": 3},
            ],
        }
    ).inserted_id
    admin.cleanCollection("record_group", GROUP, USER)
    assert (
        admin.db.records.find_one({"_id": record_id})["attributesList"][0]["value"]
        == 25
    )
    assert set(admin.deriveRecordColumnsFromRecordGroups([GROUP], USER)) == {
        "depth",
        "new",
    }
    with pytest.raises(SchemaError):
        admin.createBatchProcessingJob(GROUP, USER, "bucket")
    assert admin.db.processing_jobs.count_documents({}) == 0


def test_processing_jobs_capture_selected_parser_and_metadata(admin):
    schema_id = admin.db.record_groups.find_one()["schema_id"]
    first = admin.createBatchProcessingJob(GROUP, USER, "bucket")
    admin.updateProcessor(
        {"schema_id": schema_id, "parser_type": "form_parser", "modelId": "new"}, USER
    )
    second = admin.createBatchProcessingJob(GROUP, USER, "bucket")
    assert not first["processing_config"]["using_default_processor"]
    assert second["processing_config"]["using_default_processor"]
    assert (
        admin.getProcessingJob(first["job_id"])["processing_config"]["model_id"]
        == "model"
    )
    assert second["processing_config"]["model_id"] == "new"


@pytest.mark.parametrize("subprocess", [False, True])
def test_single_processing_passes_selected_configuration_to_worker(
    admin, monkeypatch, tmp_path, subprocess
):
    from ogrre.internal import image_handling

    schema_id = admin.db.record_groups.find_one()["schema_id"]
    admin.updateProcessor({"schema_id": schema_id, "parser_type": "form_parser"}, USER)
    monkeypatch.setattr(image_handling, "PROCESS_IMAGE_IN_SUBPROCESS", subprocess)
    monkeypatch.setattr(admin, "createRecord", lambda *args: "record")
    tasks = BackgroundTasks()
    image_handling.process_document(
        GROUP,
        USER,
        tasks,
        str(tmp_path / "image.png"),
        ".png",
        "image",
        admin,
        "image/png",
        str(tmp_path / "image.png"),
    )
    work = tasks.tasks[-1]
    assert work.kwargs["using_default_processor"] is True
    assert work.kwargs["processor_id"] == "extractor"


def test_replacement_preserves_schema_id_and_creator(admin):
    schema = create_schema(admin)
    upload = UploadFile(
        filename="schema.json",
        file=io.BytesIO(json.dumps([{**FIELD, "alias": "Updated"}]).encode()),
    )
    result = admin.uploadProcessorSchema(
        upload,
        {"name": "Shared", "documentType": "Imported"},
        USER,
        schema_id=schema["schema_id"],
    )
    assert result["schema_id"] == schema["schema_id"]
    stored = admin._schemaDocument(schema["schema_id"])
    assert (
        stored["created_by_team"] == "creator-team"
        and stored["attributes"][0]["alias"] == "Updated"
    )


def test_migration_is_previewable_idempotent_and_preserves_embedded_data(admin):
    admin.db.record_groups.update_one(
        {}, {"$unset": {"schema_id": ""}, "$set": {"team": "original-team"}}
    )
    before = admin.db.record_groups.find_one()
    preview = migrate_schema_bindings(admin.db)
    assert (
        preview["changes"][0]["create_schema"]
        and admin.db.record_groups.find_one() == before
    )
    applied = migrate_schema_bindings(admin.db, apply=True)
    migrated_id = applied["changes"][0]["schema_id"]
    assert admin._schemaDocument(migrated_id)["attributes"] == [FIELD]
    assert admin._schemaDocument(migrated_id)["created_by_team"] == "original-team"
    assert admin._schemaDocument(migrated_id)["created_by"] is None
    assert not migrate_schema_bindings(admin.db, apply=True)["changes"]
    assert "attributes" not in admin.db.record_groups.find_one()


def test_migration_requires_conflict_resolution_and_never_guesses(admin):
    admin.db.record_groups.update_one(
        {}, {"$unset": {"schema_id": ""}, "$set": {"processorId": "extractor"}}
    )
    admin.db.processors.insert_one({**copy.deepcopy(PROCESSOR), "name": "Duplicate"})
    report = migrate_schema_bindings(admin.db, apply=True)
    assert (
        not report["changes"]
        and len(report["conflicts"][0]["candidate_schema_ids"]) == 2
    )
    assert "schema_id" not in admin.db.record_groups.find_one()
    assert migrate_schema_bindings(
        admin.db, apply=True, resolutions={GROUP: "embedded"}
    )["changes"]
    assert not migrate_schema_bindings(
        admin.db, apply=True, resolutions={GROUP: "embedded"}
    )["changes"]


def test_migration_detects_divergent_embedded_schema_and_allows_explicit_detach(admin):
    admin.db.record_groups.update_one(
        {},
        {
            "$unset": {"schema_id": ""},
            "$set": {
                "processorId": "extractor",
                "attributes": [{**FIELD, "alias": "Different"}],
            },
        },
    )
    assert migrate_schema_bindings(admin.db)["conflicts"]
    migrate_schema_bindings(admin.db, apply=True, resolutions={GROUP: None})
    assert (
        admin.resolveRecordGroupSchema(admin.db.record_groups.find_one(), USER) is None
    )


def test_migration_does_not_overwrite_concurrent_group_changes(admin, monkeypatch):
    admin.db.record_groups.update_one(
        {}, {"$unset": {"schema_id": ""}, "$set": {"processorId": "extractor"}}
    )
    original = admin.db.record_groups.update_one

    def intervene(query, update, *args, **kwargs):
        original({"_id": ObjectId(GROUP)}, {"$set": {"schema_id": None}})
        return original(query, update, *args, **kwargs)

    monkeypatch.setattr(admin.db.record_groups, "update_one", intervene)
    assert migrate_schema_bindings(admin.db, apply=True)["conflicts"]
    assert admin.db.record_groups.find_one()["schema_id"] is None


def test_bound_schema_controls_unvisited_queries_and_export_when_processor_ids_overlap(
    query_manager,
):
    manager = query_manager
    second = manager.db.processors.insert_one(
        {
            **copy.deepcopy(PROCESSOR),
            "name": "Second",
            "attributes": [
                {**FIELD, "alias": "Measured depth"},
                {**FIELD, "name": "retired", "deleted": True},
            ],
        }
    ).inserted_id
    manager.updateRecordGroup(GROUP, {"schema_id": str(second)}, USER)
    manager.createRecord(
        {
            "name": "Test",
            "filename": "test",
            "record_group_id": GROUP,
            "attributesList": [
                {"key": "depth", "value": 12},
                {"key": "retired", "value": "hidden"},
            ],
        },
        USER,
    )
    rows, count = manager.fetchRecordsByRecordGroup(USER, GROUP)
    assert count == 1 and rows[0]["attributesList"][0]["alias"] == "Measured depth"
    assert len(rows[0]["attributesList"]) == 1
    path = manager.downloadRecords(
        rows, "csv", USER, GROUP, "record_group", keep_all_columns=True
    )
    with open(path) as output:
        exported = output.read()
    assert "Measured depth" in exported and "hidden" not in exported
