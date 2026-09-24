"""Preview schema role changes; --apply asks for confirmation before writing."""

import argparse
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from pymongo.errors import PyMongoError
from pymongo.uri_parser import split_hosts

from ogrre.internal.schema_validation import DESTRUCTIVE_PERMISSION


def migrate_schema_permissions(db, apply=False, expected_changes=None):
    changes = []
    updates = []
    for role in db.roles.find({}):
        before = role.get("permissions") or []
        permissions = set(before)
        if role.get("category") == "system" or (
            role.get("category") == "team" and role.get("id") == "team_lead"
        ):
            permissions.add("manage_schema")
        if role.get("category") == "system" and role.get("id") == "sys_admin":
            permissions.add(DESTRUCTIVE_PERMISSION)
        else:
            permissions.discard(DESTRUCTIVE_PERMISSION)
        if permissions == set(before):
            continue
        after = sorted(permissions)
        changes.append(
            {
                "id": role.get("id"),
                "category": role.get("category"),
                "before": before,
                "after": after,
            }
        )
        updates.append((role, after))
    if expected_changes is not None and changes != expected_changes:
        raise RuntimeError(
            "Role changes differ from the approved preview; preview again."
        )
    if apply:
        for role, after in updates:
            query = {
                "_id": role["_id"],
                "id": role.get("id"),
                "category": role.get("category"),
                "permissions": role["permissions"]
                if "permissions" in role
                else {"$exists": False},
            }
            if not db.roles.update_one(
                query, {"$set": {"permissions": after}}
            ).matched_count:
                raise RuntimeError(
                    "A role changed during migration; preview and retry."
                )
            db.history.insert_one(
                {
                    "action": "migrateSchemaPermissions",
                    "timestamp": time.time(),
                    "query": {
                        "role_id": role.get("id"),
                        "category": role.get("category"),
                        "permissions": after,
                    },
                    "previous_state": {"permissions": role.get("permissions") or []},
                    "notes": "Applied by the schema-permission migration CLI.",
                }
            )
    return changes


def connection_label(connection):
    """Show the configured hosts without user info, URI paths, or query options."""
    if not connection:
        return "(not configured)"
    if re.fullmatch(r"[A-Za-z0-9.-]+", connection):
        return f"mongodb+srv://{connection}.mongodb.net"
    try:
        parsed = urlsplit(connection)
        hosts = parsed.netloc.rsplit("@", 1)[-1]
        if parsed.scheme not in {"mongodb", "mongodb+srv"} or not hosts:
            return "(unrecognized connection; details hidden)"
        split_hosts(hosts, default_port=None)
        return f"{parsed.scheme}://{hosts}"
    except (ValueError, PyMongoError):
        return "(unrecognized connection; details hidden)"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--env",
        type=Path,
        metavar="PATH",
        help="Load this dotenv file instead of discovering .env; file values override existing environment variables.",
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
    print("Scope: all roles in this database; collaborator is informational.")
    db = mongodb_connection.connectToDatabase()
    changes = migrate_schema_permissions(db)
    print("Proposed role changes:")
    print(json.dumps(changes, indent=2))
    if not changes:
        print("No changes needed.")
        return 0
    if not args.apply:
        print("Preview only. Run with --apply to review and confirm these changes.")
        return 0
    try:
        answer = input(
            f"Apply these changes to database {json.dumps(db.name)}? [y/N]: "
        )
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() != "y":
        print("\nCancelled. No changes were applied.")
        return 1
    try:
        applied = migrate_schema_permissions(db, apply=True, expected_changes=changes)
    except RuntimeError as error:
        print(f"Migration stopped: {error}")
        return 1
    print(f"Applied changes to {len(applied)} role(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
