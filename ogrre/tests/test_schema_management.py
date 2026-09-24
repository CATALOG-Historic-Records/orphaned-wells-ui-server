import copy
import io
import json
from unittest.mock import Mock

import pytest
from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from ogrre.internal import schema_validation as rules
from ogrre.migrate_schema_permissions import migrate_schema_permissions


USER = {"email": "lead@example.com"}
GROUP = "a" * 24
FIELD = {
    "name": "depth",
    "data_type": "Plain text",
    "database_data_type": "float",
    "page_order_sort": 1,
}
PROCESSOR = {
    "name": "well",
    "displayName": "Well",
    "processorId": "extractor",
    "modelId": "model",
    "documentType": "Well",
    "attributes": [FIELD],
}


@pytest.fixture
def schema_manager(manager, monkeypatch):
    from ogrre.internal import data_manager, util

    del manager.getRecordGroupProcessingConfig

    monkeypatch.setattr(data_manager, "USE_DB_PROCESSORS", True)
    monkeypatch.setattr(data_manager, "REQUIRE_AUTH", True)
    monkeypatch.setattr(
        manager,
        "hasPermission",
        Mock(
            side_effect=lambda email, permission: permission
            != rules.DESTRUCTIVE_PERMISSION
        ),
    )
    monkeypatch.setattr(manager, "getUserRecordGroups", Mock(return_value=[GROUP]))
    monkeypatch.setattr(manager, "getProjectFromRecordGroup", Mock(return_value={}))
    monkeypatch.setattr(util, "generate_file_url", Mock(return_value=None))
    schema_id = manager.db.processors.insert_one(copy.deepcopy(PROCESSOR)).inserted_id
    manager.db.record_groups.insert_one(
        {
            "_id": ObjectId(GROUP),
            "name": "Group",
            "attributes": [copy.deepcopy(FIELD)],
            "schema_id": str(schema_id),
        }
    )
    return manager


@pytest.fixture
def client(schema_manager, monkeypatch):
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    from ogrre.routers import router

    monkeypatch.setattr(router, "data_manager", schema_manager)
    monkeypatch.setattr(router, "REQUIRE_AUTH", True)
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[router.authenticate] = lambda: USER
    with TestClient(app) as client:
        client.cookies.set("ogrre_csrf", "schema-test-csrf")
        client.headers["X-CSRF-Token"] = "schema-test-csrf"
        yield client


def edit(client, updates=None, operation="update", field="depth"):
    return client.post(
        "/update_processor_attribute",
        json={
            "processor_name": "well",
            "field_name": field,
            "updates": updates or {},
            "operation": operation,
        },
    )


def test_safe_updates_normalize_order_and_preserve_metadata(client, schema_manager):
    schema_manager.db.processors.update_one(
        {"name": "well"},
        {
            "$set": {
                "attributes.0.field_specific_notes": "keep this",
                "attributes.0.occurrence": "Optional multiple",
            }
        },
    )
    assert edit(client, {"alias": "Depth", "page_order_sort": "10"}).status_code == 200
    field = schema_manager.db.processors.find_one()["attributes"][0]
    assert field["page_order_sort"] == 10
    assert field["alias"] == "Depth"
    assert field["field_specific_notes"] == "keep this"
    assert field["occurrence"] == "Optional multiple"


@pytest.mark.parametrize(
    "updates,operation",
    [
        ({"name": "renamed"}, "update"),
        ({"name": None}, "update"),
        ({}, "delete"),
    ],
)
def test_safe_role_cannot_rename_or_remove(client, schema_manager, updates, operation):
    before = schema_manager.db.processors.find_one()
    response = edit(client, updates, operation)
    assert response.status_code in (400, 403)
    assert schema_manager.db.processors.find_one() == before


@pytest.mark.parametrize(
    "updates",
    [
        {"data_type": "Number"},
        {"database_data_type": "int"},
        {"data_type": "Checkbox", "database_data_type": "bool"},
    ],
)
def test_schema_managers_can_change_types(client, schema_manager, updates):
    assert edit(client, updates).status_code == 200
    field = schema_manager.db.processors.find_one()["attributes"][0]
    assert all(field[key] == value for key, value in updates.items())
    assert not field.get("deleted")


def test_type_edits_cannot_invalidate_existing_children(client, schema_manager):
    parent = {**FIELD, "data_type": "Parent", "database_data_type": "Table"}
    schema_manager.db.processors.update_one(
        {}, {"$set": {"attributes": [parent, {**FIELD, "name": "depth::value"}]}}
    )
    before = schema_manager.db.processors.find_one()
    assert (
        edit(client, {"data_type": "Number", "database_data_type": "int"}).status_code
        == 400
    )
    assert schema_manager.db.processors.find_one() == before


def test_admin_can_change_types_and_delete_but_cannot_rename(client, schema_manager):
    schema_manager.hasPermission.side_effect = None
    schema_manager.hasPermission.return_value = True
    assert edit(client, {"name": "renamed"}).status_code == 400
    assert (
        edit(
            client, {"data_type": "Checkbox", "database_data_type": "bool"}
        ).status_code
        == 200
    )
    assert edit(client, operation="delete").status_code == 200
    assert schema_manager.db.processors.find_one()["attributes"][0]["deleted"] is True
    assert schema_manager.getSchema(USER)["processors"][0]["attributes"] == []
    assert edit(client, operation="delete").status_code == 404


def test_add_requires_valid_unique_paths_and_parent_types(client):
    assert edit(client, {**FIELD, "name": "new_field"}, "add").status_code == 200
    assert edit(client, {**FIELD, "name": "new_field"}, "add").status_code == 409
    assert edit(client, {**FIELD, "name": "missing::child"}, "add").status_code == 400
    assert edit(client, {**FIELD, "name": "depth::child"}, "add").status_code == 400


@pytest.mark.parametrize(
    "updates",
    [
        {"page_order_sort": "bad"},
        {"page_order_sort": True},
        {"page_order_sort": -1},
        {"cleaning_function": "not_a_function"},
        {"alias": {"$ne": None}},
        {"unexpected": "value"},
        {"data_type": "Checkbox", "database_data_type": "float"},
        {"database_data_type": "unsupported"},
    ],
)
def test_bad_updates_are_rejected_without_writes(client, schema_manager, updates):
    before = schema_manager.db.processors.find_one()
    assert edit(client, updates).status_code == 400
    assert schema_manager.db.processors.find_one() == before


@pytest.mark.parametrize(
    "endpoint",
    [
        "/update_processor_attribute",
        "/update_processor",
        f"/update_record_group/{GROUP}",
        f"/connect_record_group_processor/{GROUP}",
    ],
)
def test_mutation_bodies_must_be_objects(client, endpoint):
    assert client.post(endpoint, json=[]).status_code == 400
    assert client.post(endpoint, content="not json").status_code == 400


def test_repo_mode_reads_package_and_rejects_mongo_mutations(
    client, schema_manager, monkeypatch
):
    from ogrre.internal import data_manager

    monkeypatch.setattr(data_manager, "USE_DB_PROCESSORS", False)
    monkeypatch.setattr(schema_manager, "getCollaboratorForUser", lambda user: "isgs")
    monkeypatch.setattr(
        data_manager.processor_api,
        "get_processor_list",
        lambda collaborator: [{"Processor ID": "repo-id"}],
    )
    monkeypatch.setattr(
        data_manager.processor_api,
        "get_processor_by_id",
        lambda *args: {
            "Processor Name": "Repo Well",
            "Processor ID": "repo-id",
            "Model ID": "repo-model",
            "attributes": [
                {
                    "Name": "field",
                    "Google Data Type": "Plain_text",
                    "Page Order Sort": "2",
                }
            ],
        },
    )
    body = client.get("/get_schema").json()
    assert body["source"] == "repo" and body["read_only"]
    assert body["processors"][0]["name"] == "Repo Well"
    assert body["processors"][0]["attributes"][0]["data_type"] == "Plain text"
    assert edit(client, {"alias": "Denied"}).status_code == 409
    assert client.post("/delete_processor/well").status_code == 409
    assert (
        client.post(
            "/update_processor", json={"name": "well", "displayName": "Denied"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/upload_sample_image/well",
            files={"file": ("sample.png", b"image", "image/png")},
        ).status_code
        == 409
    )


def test_metadata_binding_and_schema_delete_need_specific_permission(
    client, schema_manager
):
    assert (
        client.post(
            "/update_processor", json={"name": "well", "displayName": "Updated"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/update_processor", json={"name": "well", "modelId": "other"}
        ).status_code
        == 403
    )
    assert client.post("/delete_processor/well").status_code == 403
    schema_manager.hasPermission.side_effect = None
    schema_manager.hasPermission.return_value = True
    assert client.post("/delete_processor/well").status_code == 409
    assert (
        client.post(
            f"/update_record_group/{GROUP}", json={"schema_id": None}
        ).status_code
        == 200
    )
    assert client.post("/delete_processor/well").status_code == 200
    assert schema_manager.db.deleted_processors.find_one()["attributes"] == [FIELD]


def test_auth_disabled_cannot_authorize_destructive_changes(
    client, schema_manager, monkeypatch
):
    from ogrre.internal import data_manager

    monkeypatch.setattr(data_manager, "REQUIRE_AUTH", False)
    schema_manager.hasPermission.side_effect = None
    schema_manager.hasPermission.return_value = True
    assert edit(client, operation="delete").status_code == 403


def test_import_and_generic_group_updates_cannot_replace_schema(client, schema_manager):
    package = {
        "records": [{"name": "New", "fields": {"depth": 10}}],
        "schema": {"fields": [{**FIELD, "name": "replacement"}]},
    }
    for endpoint, payload in [
        (f"/import_json_records/{GROUP}", package),
        (f"/update_record_group/{GROUP}", {"attributes": package["schema"]["fields"]}),
        (f"/update_record_group/{GROUP}", {"processorId": "extractor"}),
        (f"/connect_record_group_processor/{GROUP}", {"processorId": "extractor"}),
    ]:
        assert client.post(endpoint, json=payload).status_code == 403
    assert schema_manager.db.record_groups.find_one()["attributes"] == [FIELD]
    assert schema_manager.db.records.count_documents({}) == 0
    assert (
        client.post(f"/update_record_group/{GROUP}", json={"team": "other"}).status_code
        == 400
    )
    assert (
        client.post(
            f"/update_record_group/{'b' * 24}", json={"name": "Denied"}
        ).status_code
        == 403
    )


def test_upload_is_additive_for_safe_role_and_preserves_metadata(
    client, schema_manager
):
    metadata = {key: value for key, value in PROCESSOR.items() if key != "attributes"}
    files = {
        "file": (
            "schema.json",
            json.dumps(
                [
                    {
                        **FIELD,
                        "alias": "Depth",
                        "accepted_range": "0-100",
                        "field_specific_notes": "Note",
                    }
                ]
            ),
            "application/json",
        )
    }
    assert (
        client.post(
            "/upload_processor_schema", params=metadata, files=files
        ).status_code
        == 403
    )
    metadata.update(name="new", processorId="new-id")
    assert (
        client.post(
            "/upload_processor_schema", params=metadata, files=files
        ).status_code
        == 200
    )
    assert (
        schema_manager.db.processors.find_one({"name": "new"})["attributes"][0]["alias"]
        == "Depth"
    )


def test_file_formats_normalize_equivalently():
    from ogrre.internal import util

    row = {
        "Name": "field",
        "Alias": "Field",
        "Google Data Type": "Plain_text",
        "Database Data Type": "str",
        "Page Order Sort": "10",
        "Accepted Range": "A-Z",
        "Field Specific Notes": "Keep",
    }
    csv_data = ",".join(row) + "\n" + ",".join(row.values()) + "\n"
    uploaded = UploadFile(filename="schema.csv", file=io.BytesIO(csv_data.encode()))
    assert util.convert_csv_to_dict(uploaded) == util.format_schema_json(
        json.dumps([row])
    )
    field = util.convert_csv_to_dict(uploaded)[0]
    assert field["page_order_sort"] == 10 and field["field_specific_notes"] == "Keep"


def test_ambiguous_catalog_lookup_is_not_arbitrary(schema_manager):
    schema_manager.db.processors.insert_one(copy.deepcopy(PROCESSOR))
    with pytest.raises(rules.SchemaError) as error:
        schema_manager.getMongoProcessorByID("extractor")
    assert error.value.status_code == 409


def test_role_migration_is_explicit_idempotent_and_admin_only(schema_manager):
    for category, role_id in [
        ("system", "sys_admin"),
        ("system", "system_member"),
        ("team", "team_lead"),
        ("team", "team_member"),
    ]:
        schema_manager.db.roles.insert_one(
            {
                "id": role_id,
                "category": category,
                "permissions": ["review_record", rules.DESTRUCTIVE_PERMISSION],
            }
        )
    assert migrate_schema_permissions(schema_manager.db)
    assert (
        schema_manager.db.roles.count_documents({"permissions": "manage_schema"}) == 0
    )
    migrate_schema_permissions(schema_manager.db, apply=True)
    assert not migrate_schema_permissions(schema_manager.db, apply=True)
    assert (
        schema_manager.db.roles.count_documents({"permissions": "manage_schema"}) == 3
    )
    assert (
        schema_manager.db.roles.count_documents(
            {"permissions": rules.DESTRUCTIVE_PERMISSION}
        )
        == 1
    )
    with pytest.raises(ValueError):
        schema_manager.updateRolePermissions(
            "team_lead", "team", [rules.DESTRUCTIVE_PERMISSION]
        )


def test_manual_delete_only_accepts_user_added_attributes(schema_manager):
    record_id = schema_manager.db.records.insert_one(
        {"attributesList": [{"key": "original"}, {"key": "extra", "user_added": True}]}
    ).inserted_id
    with pytest.raises(PermissionError):
        schema_manager._updateRecordAttributesForFieldOperation(
            record_id, {"fieldID": {"indexes": [0]}}, "deleteField"
        )
    update = schema_manager._updateRecordAttributesForFieldOperation(
        record_id, {"fieldID": {"indexes": [1]}}, "deleteField"
    )
    assert [field["key"] for field in update["attributesList"]] == ["original"]


def test_sorter_rejects_duplicate_definitions_but_keeps_repeated_record_values():
    from ogrre.internal import util

    attributes = [{"key": "depth", "value": 1}, {"key": "depth", "value": 2}]
    with pytest.raises(rules.SchemaError):
        util.sortRecordAttributes(attributes, {"attributes": [FIELD, FIELD]})
    result, _ = util.sortRecordAttributes(
        attributes, {"attributes": [{**FIELD, "page_order_sort": "10"}]}
    )
    assert [field["value"] for field in result] == [1, 2]


@pytest.mark.parametrize(
    "content",
    [
        "",
        "Name,Name\nx,y\n",
        "Name,Google Data Type\nx,str,extra\n",
        'Name,Google Data Type\n"unterminated,str',
    ],
)
def test_malformed_csv_uploads_return_validation_errors(client, content):
    metadata = {key: value for key, value in PROCESSOR.items() if key != "attributes"}
    metadata.update(name="new", processorId="new-id")
    response = client.post(
        "/upload_processor_schema",
        params=metadata,
        files={"file": ("schema.csv", content, "text/csv")},
    )
    assert response.status_code == 400


def test_csv_preserves_multiline_notes():
    from ogrre.internal import util

    uploaded = UploadFile(
        filename="schema.csv",
        file=io.BytesIO(
            b'Name,Google Data Type,Database Data Type,Field Specific Notes\ndepth,Plain text,float,"First line\nSecond line"\n'
        ),
    )
    assert (
        util.convert_csv_to_dict(uploaded)[0]["field_specific_notes"]
        == "First line\nSecond line"
    )


def test_new_group_schema_requires_permission_before_creation(
    client, schema_manager, monkeypatch
):
    monkeypatch.setattr(schema_manager, "userCanAccessProject", lambda *args: True)
    schema_manager.hasPermission.side_effect = (
        lambda email, permission: permission == "create_record_group"
    )
    response = client.post(
        "/add_record_group",
        json={"project_id": "b" * 24, "name": "New group", "attributes": [FIELD]},
    )
    assert response.status_code == 403
    assert schema_manager.db.record_groups.count_documents({}) == 1


@pytest.mark.parametrize(
    "field",
    [
        {"name": "depth", "data_type": []},
        {"name": "depth", "data_type": "Unknown"},
        {"name": "depth", "database_data_type": "Unknown"},
        {"name": "depth", "data_type": "Parent", "database_data_type": "str"},
    ],
)
def test_import_schema_rejects_invalid_optional_type_hints(schema_manager, field):
    with pytest.raises(rules.SchemaError):
        schema_manager._getImportPackageSchemaFields({"schema": {"fields": [field]}})


def test_only_system_admin_can_receive_destructive_permission_at_runtime(
    schema_manager,
):
    for category, role in [("team", "team_lead"), ("system", "sys_admin")]:
        schema_manager.db.roles.insert_one(
            {
                "id": role,
                "category": category,
                "permissions": ["manage_schema", rules.DESTRUCTIVE_PERMISSION],
            }
        )
    user = {"default_team": "test", "roles": {"team": {"test": ["team_lead"]}}}
    assert rules.DESTRUCTIVE_PERMISSION not in schema_manager.getUserPermissions(user)
    user["roles"]["system"] = ["sys_admin"]
    assert rules.DESTRUCTIVE_PERMISSION in schema_manager.getUserPermissions(user)


def test_schema_update_does_not_overwrite_an_intervening_edit(schema_manager):
    original = schema_manager.db.processors.find_one()
    schema_manager.db.processors.update_one(
        {"_id": original["_id"]}, {"$set": {"displayName": "Updated elsewhere"}}
    )
    with pytest.raises(rules.SchemaError) as error:
        schema_manager._saveProcessorChanges(original, {"attributes": []}, USER, "test")
    assert error.value.status_code == 409
    assert schema_manager.db.processors.find_one()["attributes"] == [FIELD]


def test_admin_removing_parent_retires_descendant_definitions(client, schema_manager):
    schema_manager.hasPermission.side_effect = None
    schema_manager.hasPermission.return_value = True
    parent = {"name": "address", "data_type": "Parent", "database_data_type": "Table"}
    child = {**FIELD, "name": "address::zip"}
    schema_manager.db.processors.update_one(
        {"name": "well"}, {"$set": {"attributes": [FIELD, parent, child]}}
    )
    assert edit(client, operation="delete", field="address").status_code == 200
    fields = schema_manager.db.processors.find_one()["attributes"]
    assert fields == [FIELD, {**parent, "deleted": True}, {**child, "deleted": True}]
    assert schema_manager.getSchema(USER)["processors"][0]["attributes"] == [FIELD]


def test_safe_edit_can_clear_optional_order(client, schema_manager):
    assert edit(client, {"page_order_sort": None}).status_code == 200
    assert (
        "page_order_sort"
        not in schema_manager.db.processors.find_one()["attributes"][0]
    )
