"""Binding CLI safeguards use isolated Mongo mocks, never runtime databases."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import mongomock
import pytest
from bson import ObjectId

from ogrre import migrate_schema_bindings as migration


@pytest.fixture
def cli_db(monkeypatch):
    import dotenv
    from ogrre.internal import mongodb_connection

    db = mongomock.MongoClient().binding_migration_tests
    db.record_groups.insert_one(
        {"name": "Imported records", "attributes": [{"name": "depth"}]}
    )
    db.records.insert_one({"attributesList": [{"key": "depth", "value": "123"}]})
    monkeypatch.setattr(dotenv, "load_dotenv", lambda: False)
    monkeypatch.setattr(mongodb_connection, "connectToDatabase", lambda: db)
    monkeypatch.setattr(mongodb_connection, "DB_NAME", db.name)
    monkeypatch.setattr(
        mongodb_connection,
        "DB_CONNECTION",
        "mongodb://private-user:private-password@selected-host:27017/uri-database?token=private-token",
    )
    monkeypatch.setenv("COLLABORATOR", "test-collaborator")
    monkeypatch.setenv("USE_DB_PROCESSORS", "true")
    return db


def test_preview_identifies_target_without_writing_or_prompting(
    cli_db, monkeypatch, capsys
):
    before = migration._binding_state(cli_db)
    prompt = Mock(side_effect=AssertionError("Preview must not prompt"))
    monkeypatch.setattr("builtins.input", prompt)
    assert migration.main([]) == 0
    output = capsys.readouterr().out
    for expected in (
        "mongodb://selected-host:27017",
        cli_db.name,
        "test-collaborator",
        "Preview only",
    ):
        assert expected in output
    for secret in ("private-user", "private-password", "uri-database", "private-token"):
        assert secret not in output
    assert migration._binding_state(cli_db) == before
    assert cli_db.history.count_documents({}) == 0
    prompt.assert_not_called()


def test_apply_requires_preview_and_approval_and_preserves_records(
    cli_db, monkeypatch, capsys
):
    before = migration._binding_state(cli_db)
    records = list(cli_db.records.find())

    def approve(prompt):
        assert cli_db.name in prompt
        output = capsys.readouterr().out
        assert "Proposed binding changes:" in output
        assert '"create_schema": true' in output
        assert migration._binding_state(cli_db) == before
        assert cli_db.history.count_documents({}) == 0
        return "y"

    monkeypatch.setattr("builtins.input", approve)
    assert migration.main(["--apply"]) == 0
    group = cli_db.record_groups.find_one()
    assert "attributes" not in group
    assert cli_db.processors.find_one({"_id": ObjectId(group["schema_id"])})
    assert cli_db.history.count_documents({"action": "migrateSchemaBinding"}) == 1
    assert list(cli_db.records.find()) == records


@pytest.mark.parametrize("answer", ["", "n", "yes", EOFError(), KeyboardInterrupt()])
def test_cancelled_apply_never_writes(cli_db, monkeypatch, capsys, answer):
    before = migration._binding_state(cli_db)
    prompt = (
        Mock(side_effect=answer)
        if isinstance(answer, BaseException)
        else Mock(return_value=answer)
    )
    monkeypatch.setattr("builtins.input", prompt)
    assert migration.main(["--apply"]) == 1
    assert "Cancelled. No changes were applied." in capsys.readouterr().out
    assert migration._binding_state(cli_db) == before
    assert cli_db.history.count_documents({}) == 0


@pytest.mark.parametrize("change", ["group_fields", "new_group", "schema_fields"])
def test_changed_database_invalidates_confirmation(cli_db, monkeypatch, capsys, change):
    schema_id = cli_db.processors.insert_one(
        {"name": "Existing", "attributes": [{"name": "depth"}]}
    ).inserted_id

    def approve(prompt):
        if change == "group_fields":
            # The summary's IDs and create_schema flag remain identical.
            cli_db.record_groups.update_one(
                {}, {"$set": {"attributes.0.alias": "Changed"}}
            )
        elif change == "new_group":
            cli_db.record_groups.insert_one({"name": "New"})
        else:
            cli_db.processors.update_one(
                {"_id": schema_id}, {"$set": {"attributes.0.alias": "Changed"}}
            )
        return "y"

    monkeypatch.setattr("builtins.input", approve)
    assert migration.main(["--apply"]) == 1
    assert "changed since the approved preview" in capsys.readouterr().out
    assert cli_db.record_groups.count_documents({"schema_id": {"$exists": True}}) == 0
    assert cli_db.processors.count_documents({}) == 1
    assert cli_db.history.count_documents({}) == 0


def test_no_remaining_changes_does_not_prompt(cli_db, monkeypatch, capsys):
    migration.migrate_schema_bindings(cli_db, apply=True)
    prompt = Mock(side_effect=AssertionError("No changes must not prompt"))
    monkeypatch.setattr("builtins.input", prompt)
    assert migration.main(["--apply"]) == 0
    assert "No changes needed." in capsys.readouterr().out
    assert cli_db.history.count_documents({}) == 1
    prompt.assert_not_called()


def test_conflicts_are_reported_and_explicit_resolution_is_confirmed(
    cli_db, monkeypatch, capsys, tmp_path
):
    conflict = cli_db.record_groups.insert_one(
        {"name": "Missing schema", "processorId": "missing"}
    ).inserted_id
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert migration.main(["--apply"]) == 1
    assert "unresolved groups will remain unchanged" in capsys.readouterr().out
    assert "schema_id" not in cli_db.record_groups.find_one({"_id": conflict})
    assert cli_db.history.count_documents({}) == 1
    resolutions = tmp_path / "resolutions.json"
    resolutions.write_text(json.dumps({str(conflict): None}))
    assert migration.main(["--apply", "--resolutions", str(resolutions)]) == 0
    assert cli_db.record_groups.find_one({"_id": conflict})["schema_id"] is None
    assert cli_db.history.count_documents({}) == 2


@pytest.mark.parametrize("apply", [False, True])
def test_explicit_env_loads_before_connection_settings(tmp_path, apply):
    env_file = tmp_path / ".env.isgs"
    env_file.write_text(
        "DB_CONNECTION=mongodb://selected-user:selected-password@selected-host:27017\n"
        "DB_NAME=selected_database\nCOLLABORATOR=selected_collaborator\nUSE_DB_PROCESSORS=true\n"
    )
    script = """
import sys
from unittest.mock import patch
import mongomock
from ogrre.migrate_schema_bindings import main

client = mongomock.MongoClient()
db = client.selected_database
db.record_groups.insert_one({'name': 'Test group'})
with patch('pymongo.mongo_client.MongoClient', return_value=client) as connect, patch('builtins.input', return_value='y') as prompt:
    assert main(sys.argv[1:]) == 0
    assert connect.call_args.args[0] == 'mongodb://selected-user:selected-password@selected-host:27017'
    applying = '--apply' in sys.argv
    assert prompt.call_count == int(applying)
    assert db.history.count_documents({}) == int(applying)
"""
    env = {
        **os.environ,
        "DB_CONNECTION": "mongodb://wrong-host:27017",
        "DB_NAME": "wrong_database",
        "COLLABORATOR": "wrong_collaborator",
        "USE_DB_PROCESSORS": "false",
        "PYTHONPATH": os.pathsep.join(
            [str(Path(__file__).resolve().parents[2]), *sys.path]
        ),
    }
    env.pop("PYTHON_DOTENV_DISABLED", None)
    args = ["--env", ".env.isgs", "--apply"] if apply else ["--env=.env.isgs"]
    result = subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    for expected in (
        str(env_file),
        "selected-host",
        "selected_database",
        "selected_collaborator",
    ):
        assert expected in result.stdout
    for hidden in (
        "wrong-host",
        "wrong_database",
        "wrong_collaborator",
        "selected-user",
        "selected-password",
    ):
        assert hidden not in result.stdout


@pytest.mark.parametrize("kind", ["missing", "directory", "empty", "invalid_utf8"])
def test_invalid_env_stops_before_connecting(tmp_path, monkeypatch, kind):
    from ogrre.internal import mongodb_connection

    env_file = tmp_path / ".env.test"
    if kind == "directory":
        env_file.mkdir()
    elif kind == "empty":
        env_file.write_text("")
    elif kind == "invalid_utf8":
        env_file.write_bytes(b"\xff")
    connect = Mock(side_effect=AssertionError("Invalid env must not connect"))
    monkeypatch.setattr(mongodb_connection, "connectToDatabase", connect)
    with pytest.raises(SystemExit) as error:
        migration.main(["--env", str(env_file), "--apply"])
    assert error.value.code == 2
    connect.assert_not_called()


def test_repo_mode_stops_before_connecting(cli_db, monkeypatch):
    from ogrre.internal import mongodb_connection

    connect = Mock(side_effect=AssertionError("Repo mode must not connect"))
    monkeypatch.setattr(mongodb_connection, "connectToDatabase", connect)
    monkeypatch.setenv("USE_DB_PROCESSORS", "false")
    with pytest.raises(SystemExit) as error:
        migration.main(["--apply"])
    assert error.value.code == 2
    connect.assert_not_called()
