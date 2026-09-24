"""Preview or apply one bounded batch of an existing group's schema to its records."""

import argparse
import json
import os

from bson import ObjectId

from ogrre.migrate_schema_permissions import connection_label


def reconcile_batch(
    manager, group_id, batch_size=100, apply=False, expected_revision=None
):
    if not ObjectId.is_valid(group_id):
        raise ValueError("Provide a valid record-group ID.")
    if not 1 <= batch_size <= 1000:
        raise ValueError("Batch size must be between 1 and 1000.")
    # Use the same catalog guard as explicit schema changes. No schema writes
    # or record scans happen merely because the app or a page starts.
    with manager._schemaCatalogWrite():
        group = manager.db.record_groups.find_one({"_id": ObjectId(group_id)})
        if group is None:
            raise ValueError("Record group not found.")
        schema = manager.resolveRecordGroupSchema(group, strict=True)
        if schema is None:
            raise ValueError("This record group has no schema to apply.")
        state = manager._schemaState(schema)
        if expected_revision is not None and state[2] != expected_revision:
            raise ValueError("The schema changed. Preview this batch again.")
        query = {
            "record_group_id": group_id,
            "attribute_schema_revision": {"$ne": state[2]},
        }
        before = manager.db.records.count_documents(query)
        if apply:
            manager._ensureRecordGroupsReconciled([group_id], limit=batch_size)
        remaining = manager.db.records.count_documents(query) if apply else before
        report = {
            "record_group_id": group_id,
            "schema_revision": state[2],
            "batch_size": batch_size,
            "pending_before": before,
            "remaining": remaining,
            "complete": remaining == 0,
        }
        if apply and before != remaining:
            manager.recordHistory(
                "reconcileSchemaRecords", None, rg_id=group_id, notes=report
            )
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record_group_id")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv()
    from ogrre.internal import mongodb_connection
    from ogrre.internal.data_manager import data_manager

    print(
        json.dumps(
            {
                "connection": connection_label(mongodb_connection.DB_CONNECTION),
                "database": mongodb_connection.DB_NAME,
                "collaborator": os.getenv("COLLABORATOR"),
            },
            indent=2,
        )
    )
    try:
        report = reconcile_batch(data_manager, args.record_group_id, args.batch_size)
        print(json.dumps(report, indent=2))
        if not args.apply or report["complete"]:
            return 0
        try:
            answer = input(
                "Apply this batch to the displayed database and group? [y/N]: "
            )
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() != "y":
            print("Cancelled. No record changes were applied.")
            return 1
        report = reconcile_batch(
            data_manager,
            args.record_group_id,
            args.batch_size,
            apply=True,
            expected_revision=report["schema_revision"],
        )
        print(json.dumps(report, indent=2))
        if not report["complete"]:
            print("More records remain. Run another batch to continue.")
        return 0
    except ValueError as error:
        print(f"Unable to apply schema: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
