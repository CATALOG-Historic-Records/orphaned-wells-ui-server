import copy

import pytest
from bson import ObjectId

from ogrre.internal.schema_validation import SchemaError
from ogrre.tests.test_schema_identity import admin
from ogrre.tests.test_schema_management import (
    FIELD,
    GROUP,
    USER,
    client,
    schema_manager,
)
from ogrre.tests.test_attribute_retirement import query_manager, retirement_manager


@pytest.fixture
def data_manager(admin):
    from ogrre.internal import data_manager as module

    return module


@pytest.fixture
def package(admin, monkeypatch, data_manager):
    definitions = {
        "Package well": {
            "Processor Name": "Package well",
            "Processor ID": "package-processor",
            "Model ID": "package-model",
            "attributes": [copy.deepcopy(FIELD)],
        },
        "Second": {
            "Processor Name": "Second",
            "Processor ID": "second",
            "Model ID": "second-model",
            "attributes": [copy.deepcopy(FIELD)],
        },
    }
    monkeypatch.setattr(
        data_manager.processor_api,
        "get_processor_list",
        lambda collaborator: [
            {key: value for key, value in item.items() if key != "attributes"}
            for item in definitions.values()
        ],
    )
    monkeypatch.setattr(
        data_manager.processor_api,
        "get_processor_by_name",
        lambda collaborator, name: copy.deepcopy(definitions[name]),
    )
    return definitions


def preview(manager, mode="add", selected=None, decisions=None):
    return manager.previewRepoSchemaImport(
        {
            "mode": mode,
            "selected": selected or ["Package well"],
            "decisions": decisions or {},
        },
        USER,
    )


def apply(manager, plan):
    return manager.applyRepoSchemaImport({"import_id": plan["import_id"]}, USER)


def test_add_preview_then_apply_creates_owned_schema_once(admin, package):
    before = list(admin.db.processors.find())
    plan = preview(admin)
    assert plan["counts"]["added"] == 1 and plan["can_apply"]
    assert list(admin.db.processors.find()) == before
    assert plan["source"]["collaborator"] == "isgs"
    assert "operations" not in plan
    result = apply(admin, plan)
    assert result["status"] == "complete"
    created = admin.db.processors.find_one({"name": "Package well"})
    assert created["created_by"] == USER["email"]
    assert created["created_by_team"] == "creator-team"
    assert created["repo_origin"]["source_id"] == "Package well"
    assert apply(admin, plan) == result
    assert admin.db.processors.count_documents({"name": "Package well"}) == 1
    assert preview(admin)["counts"]["unchanged"] == 1
    admin.updateProcessorAttribute(
        None, "depth", {"alias": "Database owned"}, USER, schema_id=str(created["_id"])
    )
    assert (
        admin._schemaDocument(str(created["_id"]))["attributes"][0]["alias"]
        == "Database owned"
    )


def test_conflict_requires_explicit_choice_and_destructive_permission(
    client, admin, package
):
    package["Package well"]["Processor ID"] = "extractor"
    original = admin.db.processors.find_one()
    admin.hasPermission.side_effect = (
        lambda email, permission: permission != "manage_schema_destructive"
    )
    plan = preview(admin)
    assert not plan["can_apply"] and plan["counts"]["conflict"] == 1
    assert (
        client.post(
            "/apply_repo_schema_import", json={"import_id": plan["import_id"]}
        ).status_code
        == 409
    )
    request = {
        "mode": "add",
        "selected": ["Package well"],
        "decisions": {
            "Package well": {"action": "replace", "schema_id": str(original["_id"])}
        },
    }
    assert client.post("/preview_repo_schema_import", json=request).status_code == 403
    request["decisions"]["Package well"]["action"] = "keep"
    plan = client.post("/preview_repo_schema_import", json=request).json()
    assert apply(admin, plan)["status"] == "complete"
    assert admin.db.processors.find_one() == original
    assert (
        client.post(
            "/preview_repo_schema_import",
            json={"mode": "replace", "selected": ["Package well"]},
        ).status_code
        == 403
    )


def test_replacement_retains_identity_creator_and_removed_field_data(admin, package):
    original = admin.db.processors.find_one()
    package["Package well"]["Processor ID"] = "extractor"
    package["Package well"]["attributes"] = [{**FIELD, "name": "new_field"}]
    record_id = admin.db.records.insert_one(
        {"record_group_id": GROUP, "attributesList": [{"key": "depth", "value": 12}]}
    ).inserted_id
    plan = preview(
        admin,
        decisions={
            "Package well": {"action": "replace", "schema_id": str(original["_id"])}
        },
    )
    assert plan["entries"][0]["diff"]["retired"] == ["depth"]
    assert plan["affected_groups"][0]["id"] == GROUP
    assert apply(admin, plan)["status"] == "complete"
    stored = admin.db.processors.find_one({"_id": original["_id"]})
    assert stored["name"] == original["name"]  # Renaming remains disabled.
    assert "created_by" not in stored
    assert stored["attributes"][1]["deleted"]
    assert admin.db.record_groups.find_one()["schema_id"] == str(original["_id"])
    admin._ensureRecordGroupsReconciled([GROUP], USER)
    fields = admin.db.records.find_one({"_id": record_id})["attributesList"]
    assert any(
        field["key"] == "depth" and field["value"] == 12 and field["deleted"]
        for field in fields
    )


def test_replace_detaches_every_team_and_archives_before_removal(admin, package):
    schema = admin.db.processors.find_one()
    other_id = admin.db.record_groups.insert_one(
        {"name": "Other team", "team": "other", "schema_id": str(schema["_id"])}
    ).inserted_id
    for group_id in (GROUP, str(other_id)):
        admin.db.records.insert_one(
            {
                "record_group_id": group_id,
                "attributesList": [
                    {"key": "depth", "value": 12},
                    {"key": "old", "value": "preserved", "deleted": True},
                ],
            }
        )
    plan = preview(admin, mode="replace")
    assert plan["counts"]["detached"] == 2
    assert {group["id"] for group in plan["detached_groups"]} == {GROUP, str(other_id)}
    assert apply(admin, plan)["status"] == "complete"
    assert (
        admin.db.deleted_processors.find_one({"_id": schema["_id"]})["attributes"]
        == schema["attributes"]
    )
    assert admin.db.processors.count_documents({}) == 1
    for group in admin.db.record_groups.find():
        assert group["schema_id"] is None
    for record in admin.db.records.find():
        assert [attribute["value"] for attribute in record["attributesList"]] == [
            12,
            "preserved",
        ]
        assert record["attributesList"][1]["deleted"]


@pytest.mark.parametrize("change", ["catalog", "groups", "package", "expiry"])
def test_stale_preview_never_applies(admin, package, change):
    plan = preview(admin)
    if change == "catalog":
        admin.db.processors.update_one({}, {"$set": {"displayName": "Changed"}})
    elif change == "groups":
        admin.db.record_groups.insert_one(
            {
                "name": "New reference",
                "schema_id": str(admin.db.processors.find_one()["_id"]),
            }
        )
    elif change == "package":
        package["Package well"]["Model ID"] = "changed"
    else:
        admin.db.schema_imports.update_one(
            {"_id": plan["import_id"]}, {"$set": {"expires_at": 0}}
        )
    with pytest.raises(SchemaError) as error:
        apply(admin, plan)
    assert error.value.status_code == 409
    assert admin.db.processors.count_documents({"name": "Package well"}) == 0
    assert admin.db.schema_catalog_guard.find_one()["pending_import"] is None


@pytest.mark.parametrize("kind", ["binding", "schema", "remove"])
def test_partial_apply_resumes_after_write_before_progress_without_duplicates(
    admin, package, monkeypatch, kind
):
    plan = preview(admin, mode="replace")
    original = admin._applySchemaImportStep
    fail = [True]

    def interrupted(operation, job, user):
        original(operation, job, user)
        if operation["kind"] == kind and fail[0]:
            fail[0] = False
            raise RuntimeError("Injected interruption after a successful write")

    monkeypatch.setattr(admin, "_applySchemaImportStep", interrupted)
    result = apply(admin, plan)
    assert result["status"] == "partial"
    assert (
        admin.getRepoSchemaImport(USER)["pending_import"]["import_id"]
        == plan["import_id"]
    )
    with pytest.raises(SchemaError):
        admin.createSchema({"name": "Blocked", "documentType": "Well"}, USER)
    with pytest.raises(SchemaError):
        admin.updateRecordGroup(GROUP, {"schema_id": None}, USER)
    package.clear()  # Resume uses the approved snapshot, not a newly installed package.
    assert apply(admin, plan)["status"] == "complete"
    assert admin.db.processors.count_documents({}) == 1
    assert admin.db.record_groups.find_one()["schema_id"] is None
    assert admin.db.schema_catalog_guard.find_one()["pending_import"] is None
    assert admin.db.schema_catalog_guard.find_one()["owner"] is None
    assert (
        admin.db.history.count_documents({"schema_import_id": plan["import_id"]})
        == plan["total_steps"]
    )


def test_guard_blocks_concurrent_catalog_and_binding_writers(
    admin, package, data_manager
):
    token = data_manager._catalog_writer.set(None)
    admin.db.schema_catalog_guard.insert_one(
        {"_id": "catalog", "owner": "another-worker", "pending_import": None}
    )
    try:
        with pytest.raises(SchemaError):
            admin.updateProcessorAttribute(
                "well", "depth", {"alias": "Overwritten"}, USER
            )
        with pytest.raises(SchemaError):
            preview(admin)
        assert admin.db.processors.find_one()["attributes"] == [FIELD]
    finally:
        data_manager._catalog_writer.reset(token)


def test_invalid_missing_and_duplicate_package_definitions_are_visible(admin, package):
    del package["Package well"]["attributes"]
    package["Second"]["attributes"] = [FIELD, FIELD]
    response = admin.getRepoSchemaImport(USER)
    assert all(schema.get("error") for schema in response["schemas"])
    assert not preview(admin)["can_apply"]


def test_multiple_matches_require_target_and_preserved_origin_is_used(admin, package):
    one = admin.db.processors.find_one()
    admin.db.processors.update_one(
        {"_id": one["_id"]},
        {
            "$set": {
                "repo_origin": {"collaborator": "isgs", "source_id": "Package well"}
            }
        },
    )
    two = admin.db.processors.insert_one(
        {"name": "Package well", "attributes": [FIELD]}
    ).inserted_id
    plan = preview(admin)
    assert len(plan["entries"][0]["candidates"]) == 2
    plan = preview(
        admin,
        decisions={"Package well": {"action": "replace", "schema_id": str(one["_id"])}},
    )
    assert plan["can_apply"]
    assert apply(admin, plan)["status"] == "complete"
    assert admin.db.processors.find_one({"_id": two})["name"] == "Package well"
    assert (
        admin.db.processors.find_one({"_id": one["_id"]})["modelId"] == "package-model"
    )


def test_legacy_reference_is_pinned_before_processor_binding_changes(admin, package):
    original = admin.db.processors.find_one()
    admin.db.record_groups.update_one(
        {},
        {
            "$unset": {"schema_id": "", "attributes": ""},
            "$set": {"processorId": "extractor"},
        },
    )
    admin.db.processors.update_one(
        {},
        {
            "$set": {
                "repo_origin": {"collaborator": "isgs", "source_id": "Package well"}
            }
        },
    )
    plan = preview(
        admin,
        decisions={
            "Package well": {"action": "replace", "schema_id": str(original["_id"])}
        },
    )
    assert apply(admin, plan)["status"] == "complete"
    assert admin.db.record_groups.find_one()["schema_id"] == str(original["_id"])
    assert (
        admin.getRecordGroupProcessingConfig(GROUP, USER)["processor_id"]
        == "package-processor"
    )


def test_ambiguous_legacy_groups_block_affected_import(admin, package):
    original = admin.db.processors.find_one()
    admin.db.processors.insert_one({**original, "_id": ObjectId(), "name": "Duplicate"})
    admin.db.record_groups.update_one(
        {}, {"$unset": {"schema_id": ""}, "$set": {"processorId": "extractor"}}
    )
    plan = preview(admin, mode="replace")
    assert not plan["can_apply"] and any(
        "ambiguous" in message for message in plan["errors"]
    )


def test_repo_mode_rejects_import_and_requests_are_validated(
    client, admin, package, monkeypatch, data_manager
):
    assert client.post("/preview_repo_schema_import", json=[]).status_code == 400
    assert (
        client.post(
            "/preview_repo_schema_import", json={"mode": "add", "selected": []}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/apply_repo_schema_import", json={"import_id": "missing", "operations": []}
        ).status_code
        == 400
    )
    monkeypatch.setattr(data_manager, "USE_DB_PROCESSORS", False)
    assert client.get("/get_repo_schema_import").status_code == 409
    assert (
        client.post(
            "/preview_repo_schema_import",
            json={"mode": "add", "selected": ["Package well"]},
        ).status_code
        == 409
    )


def test_safe_manager_cannot_resume_someone_elses_import(admin, package):
    plan = preview(admin)
    admin.hasPermission.side_effect = (
        lambda email, permission: permission != "manage_schema_destructive"
    )
    with pytest.raises(PermissionError):
        admin.applyRepoSchemaImport(
            {"import_id": plan["import_id"]}, {"email": "other@example.com"}
        )


def test_permissions_are_rechecked_after_preview(admin, package):
    plan = preview(admin, mode="replace")
    admin.hasPermission.side_effect = (
        lambda email, permission: permission != "manage_schema_destructive"
    )
    with pytest.raises(PermissionError):
        apply(admin, plan)
    assert admin.db.processors.count_documents({}) == 1


def test_completed_receipt_releases_leftover_pending_import(admin, package):
    plan = preview(admin)
    apply(admin, plan)
    admin.db.schema_catalog_guard.update_one(
        {}, {"$set": {"pending_import": plan["import_id"]}}
    )
    assert apply(admin, plan)["status"] == "complete"
    assert admin.db.schema_catalog_guard.find_one()["pending_import"] is None


def test_activating_missing_legacy_reference_needs_administrator(admin, package):
    admin.db.record_groups.update_one(
        {},
        {
            "$unset": {"schema_id": "", "attributes": ""},
            "$set": {"processorId": "package-processor"},
        },
    )
    admin.hasPermission.side_effect = (
        lambda email, permission: permission != "manage_schema_destructive"
    )
    with pytest.raises(PermissionError):
        preview(admin)
    admin.hasPermission.side_effect = None
    plan = preview(admin)
    assert plan["destructive"] and plan["affected_groups"][0]["id"] == GROUP
    assert apply(admin, plan)["status"] == "complete"
    assert (
        admin.resolveRecordGroupSchema(admin.db.record_groups.find_one(), USER)["name"]
        == "Package well"
    )


def test_standalone_mongo_import_guard_and_resumption(
    query_manager, package, monkeypatch
):
    manager = query_manager
    monkeypatch.setattr(manager, "getDefaultTeamForUser", lambda *args: "creator-team")
    manager.db.records.insert_one(
        {"record_group_id": GROUP, "attributesList": [{"key": "depth", "value": 20}]}
    )
    plan = preview(manager, mode="replace")
    original = manager._applySchemaImportStep

    def interrupt_after_delete(operation, job, user):
        original(operation, job, user)
        if operation["kind"] == "remove":
            raise RuntimeError("Simulated connection loss after delete acknowledgement")

    monkeypatch.setattr(manager, "_applySchemaImportStep", interrupt_after_delete)
    assert apply(manager, plan)["status"] == "partial"
    # A separate manager using the same database must respect the persisted guard.
    second = copy.copy(manager)
    with pytest.raises(SchemaError):
        second.createSchema({"name": "Concurrent", "documentType": "Well"}, USER)
    monkeypatch.setattr(manager, "_applySchemaImportStep", original)
    assert apply(manager, plan)["status"] == "complete"
    assert manager.db.records.find_one()["attributesList"][0]["value"] == 20
    assert manager.db.record_groups.find_one()["schema_id"] is None
    assert manager.db.processors.count_documents({}) == 1
    assert manager.db.deleted_processors.count_documents({}) == 1
