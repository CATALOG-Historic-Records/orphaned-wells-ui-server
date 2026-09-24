"""Preview legacy Mongo schema bindings; --apply asks for confirmation before writing."""

import argparse
import copy
import hashlib
import json
import os
import time
from pathlib import Path

from bson import ObjectId

from ogrre.internal import schema_validation as rules
from ogrre.migrate_schema_permissions import connection_label


def _fields(attributes):
    fields = rules.normalize_fields(attributes or [], strict=False)
    rules.validate_structure(fields)
    return sorted(fields, key=lambda field: field["name"])


def _binding_state(db):
    """Capture schema contents too: IDs alone cannot detect a stale approval."""
    return {
        name: {str(item["_id"]): item for item in db[name].find({})}
        for name in ("record_groups", "processors")
    }


def migrate_schema_bindings(db, apply=False, resolutions=None, expected_state=None):
    if expected_state is not None and _binding_state(db) != expected_state:
        raise RuntimeError(
            "Record groups or schemas changed since the approved preview; preview again."
        )
    resolutions = resolutions or {}
    if not isinstance(resolutions, dict):
        raise ValueError(
            "Resolutions must map record-group IDs to schema IDs, null, or 'embedded'."
        )
    report = {"changes": [], "conflicts": [], "unchanged": 0}
    for group in db.record_groups.find({}):
        group_id = str(group["_id"])
        candidates = []
        embedded = None
        reason = None
        has_resolution = group_id in resolutions
        try:
            if has_resolution:
                target = resolutions[group_id]
                resolved_id = (
                    hashlib.sha256(
                        f"ogrre-group-schema:{group_id}".encode()
                    ).hexdigest()[:24]
                    if target == "embedded"
                    else target
                )
                if (
                    "schema_id" in group
                    and group["schema_id"] == resolved_id
                    and "attributes" not in group
                ):
                    if resolved_id is None or (
                        isinstance(resolved_id, str)
                        and ObjectId.is_valid(resolved_id)
                        and db.processors.find_one({"_id": ObjectId(resolved_id)})
                    ):
                        report["unchanged"] += 1
                        continue
            elif "schema_id" in group:
                target = group["schema_id"]
                if target is not None and (
                    not isinstance(target, str)
                    or not ObjectId.is_valid(target)
                    or db.processors.find_one({"_id": ObjectId(target)}) is None
                ):
                    raise ValueError(
                        "The existing schema reference is invalid or missing."
                    )
                report["unchanged"] += 1
                continue
            elif group.get("processorId"):
                candidates = list(
                    db.processors.find({"processorId": group["processorId"]})
                )
                if len(candidates) != 1:
                    raise ValueError(
                        "The legacy processor ID has no unique schema match."
                    )
                target = str(candidates[0]["_id"])
                if group.get("attributes") and _fields(group["attributes"]) != _fields(
                    candidates[0].get("attributes")
                ):
                    raise ValueError(
                        "Embedded fields differ from the matching catalog schema."
                    )
            elif group.get("attributes"):
                target = "embedded"
            else:
                target = None

            if target == "embedded":
                if not group.get("attributes"):
                    raise ValueError("This group has no embedded schema to preserve.")
                fields = _fields(group["attributes"])
                target = hashlib.sha256(
                    f"ogrre-group-schema:{group_id}".encode()
                ).hexdigest()[:24]
                creator = group.get("creator")
                created_by = (
                    creator.get("email")
                    if isinstance(creator, dict)
                    else creator
                    if isinstance(creator, str)
                    else None
                )
                embedded = {
                    "_id": ObjectId(target),
                    "name": f"record-group-{group_id}",
                    "displayName": group.get("name") or "Imported records",
                    "documentType": group.get("documentType") or "Imported records",
                    "attributes": fields,
                    "created_by": created_by,
                    "created_by_team": group.get("team"),
                    "created_at": group.get("dateCreated"),
                    "migrated_from_record_group": group_id,
                }
                existing = db.processors.find_one({"_id": embedded["_id"]})
                if existing and (
                    existing.get("migrated_from_record_group") != group_id
                    or _fields(existing.get("attributes")) != fields
                ):
                    raise ValueError(
                        "The previously migrated schema changed; choose its schema ID explicitly."
                    )
                name_match = db.processors.find_one({"name": embedded["name"]})
                if name_match and name_match["_id"] != embedded["_id"]:
                    raise ValueError(
                        "The generated schema name already exists; resolve the name conflict first."
                    )
            elif target is not None:
                if not isinstance(target, str) or not ObjectId.is_valid(target):
                    raise ValueError("Choose a valid schema ID, null, or 'embedded'.")
                if db.processors.find_one({"_id": ObjectId(target)}) is None:
                    raise ValueError("The selected schema does not exist.")
        except (ValueError, rules.SchemaError) as error:
            reason = str(error)
        if reason:
            report["conflicts"].append(
                {
                    "record_group_id": group_id,
                    "name": group.get("name"),
                    "reason": reason,
                    "candidate_schema_ids": [str(item["_id"]) for item in candidates],
                }
            )
            continue

        change = {
            "record_group_id": group_id,
            "name": group.get("name"),
            "schema_id": target,
            "create_schema": embedded is not None,
            "detach": target is None,
        }
        if apply:
            now = time.time()
            if embedded:
                db.processors.update_one(
                    {"_id": embedded["_id"]},
                    {
                        "$setOnInsert": {
                            **embedded,
                            "migrated_at": now,
                            "updated_at": now,
                        }
                    },
                    upsert=True,
                )
            query = {"_id": group["_id"]}
            for key in ("schema_id", "processorId", "attributes"):
                query[key] = group[key] if key in group else {"$exists": False}
            result = db.record_groups.update_one(
                query, {"$set": {"schema_id": target}, "$unset": {"attributes": ""}}
            )
            if not result.matched_count:
                change["applied"] = False
                report["conflicts"].append(
                    {
                        **change,
                        "reason": "The group changed during migration. Preview and retry.",
                    }
                )
                continue
            change["applied"] = True
            db.history.insert_one(
                {
                    "action": "migrateSchemaBinding",
                    "timestamp": now,
                    "rg_id": group_id,
                    "query": {"schema_id": target},
                    "previous_state": {
                        key: copy.deepcopy(group.get(key))
                        for key in ("schema_id", "processorId", "attributes")
                    },
                    "notes": "Applied by the schema-binding migration CLI.",
                }
            )
        report["changes"].append(change)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--env",
        type=Path,
        metavar="PATH",
        help="Load this dotenv file instead of discovering .env; file values override existing environment variables.",
    )
    parser.add_argument(
        "--resolutions",
        help="JSON file mapping group IDs to schema IDs, null, or 'embedded'.",
    )
    args = parser.parse_args(argv)
    from dotenv import load_dotenv

    if args.env is not None:
        env_path = args.env.expanduser().resolve()
        if not env_path.is_file():
            parser.error("--env must name an existing file.")
        try:
            loaded = load_dotenv(env_path, override=True)
        except (OSError, UnicodeError):
            parser.error("Unable to read the --env file as UTF-8 text.")
        if not loaded:
            parser.error("The --env file did not load any environment values.")
        print(f"Environment file: {env_path}")
    else:
        load_dotenv()

    if os.getenv("USE_DB_PROCESSORS", "false").lower() not in ("1", "true", "yes"):
        parser.error("Enable USE_DB_PROCESSORS before migrating Mongo schema bindings.")
    resolutions = None
    if args.resolutions:
        try:
            with open(args.resolutions, encoding="utf-8") as source:
                resolutions = json.load(source)
        except (OSError, UnicodeError, ValueError):
            parser.error("--resolutions must name a readable JSON file.")
        if not isinstance(resolutions, dict):
            parser.error(
                "Resolutions must map group IDs to schema IDs, null, or 'embedded'."
            )
    # Connection settings are captured on import, after loading the selected file.
    from ogrre.internal import mongodb_connection

    print("Target database:")
    print(
        json.dumps(
            {
                "connection": connection_label(mongodb_connection.DB_CONNECTION),
                "database": mongodb_connection.DB_NAME,
                "configured_collaborator": os.getenv("COLLABORATOR") or None,
            },
            indent=2,
        )
    )
    print("Scope: all record groups in this database; collaborator is informational.")
    db = mongodb_connection.connectToDatabase()
    state = _binding_state(db) if args.apply else None
    report = migrate_schema_bindings(db, resolutions=resolutions)
    print("Proposed binding changes:")
    print(json.dumps(report, indent=2, default=str))
    if not report["changes"]:
        print(
            "No applicable changes. Resolve the reported conflicts."
            if report["conflicts"]
            else "No changes needed."
        )
        return int(bool(report["conflicts"]))
    if not args.apply:
        print("Preview only. Run with --apply to review and confirm these changes.")
        return int(bool(report["conflicts"]))
    if report["conflicts"]:
        print(
            "Only the listed changes will be applied; unresolved groups will remain unchanged."
        )
    try:
        answer = input(
            f"Apply these changes to {len(report['changes'])} record group(s) in database {json.dumps(db.name)}? [y/N]: "
        )
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() != "y":
        print("\nCancelled. No changes were applied.")
        return 1
    try:
        applied = migrate_schema_bindings(
            db, apply=True, resolutions=resolutions, expected_state=state
        )
    except RuntimeError as error:
        print(f"Migration stopped: {error}")
        return 1
    print("Migration result:")
    print(json.dumps(applied, indent=2, default=str))
    return int(bool(applied["conflicts"]))


if __name__ == "__main__":
    raise SystemExit(main())
