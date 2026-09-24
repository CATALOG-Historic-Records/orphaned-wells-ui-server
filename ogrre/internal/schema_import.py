"""Pure package-import planning and diffs; persistence belongs to DataManager."""

import copy
from functools import wraps

from bson import ObjectId

from ogrre.internal import schema_validation as rules


METADATA = ("displayName", "documentType", "processorId", "modelId", "parser_type")
GROUP_FIELDS = ("schema_id", "processorId", "attributes", "name", "team", "project_id")


def catalog_write(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._schemaCatalogWrite():
            return method(self, *args, **kwargs)

    return guarded


def group_snapshot(group):
    return {
        key: copy.deepcopy(group[key]) for key in ("_id", *GROUP_FIELDS) if key in group
    }


def group_label(group):
    return {
        "id": str(group["_id"]),
        "name": group.get("name") or "Unnamed record group",
        "team": group.get("team"),
    }


def differences(before, after):
    old = {
        f["name"]: f
        for f in rules.normalize_fields(before.get("attributes", []), strict=False)
        if not f.get("deleted")
    }
    new = {f["name"]: f for f in after.get("attributes", [])}
    return {
        "added": sorted(new.keys() - old.keys()),
        "retired": sorted(old.keys() - new.keys()),
        "changed": [
            {"name": name, "before": old[name], "after": new[name]}
            for name in sorted(old.keys() & new.keys())
            if old[name] != new[name]
        ],
        "metadata": [
            {"name": key, "before": before.get(key), "after": after.get(key)}
            for key in METADATA
            if (before.get(key) or None) != (after.get(key) or None)
        ],
    }


def same_definition(before, after):
    return not any(differences(before, after).values())


def build_plan(package, catalog, groups, request, creator, now):
    mode = request.get("mode")
    selected = request.get("selected")
    decisions = request.get("decisions", {})
    if mode not in ("add", "replace"):
        raise rules.SchemaError("Choose Add or Replace.")
    if (
        not isinstance(selected, list)
        or not selected
        or any(not isinstance(key, str) for key in selected)
        or len(set(selected)) != len(selected)
    ):
        raise rules.SchemaError("Select at least one schema, without duplicates.")
    available = {item["source_id"]: item for item in package["schemas"]}
    if set(selected) - available.keys():
        raise rules.SchemaError(
            "The selected package schemas are no longer available.", 409
        )
    if not isinstance(decisions, dict) or set(decisions) - set(selected):
        raise rules.SchemaError("Provide decisions only for selected schemas.")
    for decision in decisions.values():
        if (
            not isinstance(decision, dict)
            or set(decision) - {"action", "schema_id"}
            or decision.get("action") not in ("keep", "replace")
        ):
            raise rules.SchemaError(
                "Choose Keep existing or Use repo version for each conflict."
            )
        if "schema_id" in decision and not isinstance(decision["schema_id"], str):
            raise rules.SchemaError("Choose an existing schema for this conflict.")
        if mode == "replace" and decision["action"] == "keep":
            raise rules.SchemaError(
                "Replace uses the selected repo versions. Use Add to keep existing definitions."
            )

    entries, errors, changes, retained = [], [], [], set()
    by_id = {str(schema["_id"]): schema for schema in catalog}
    legacy = {}
    for group in groups:
        if "schema_id" not in group and group.get("processorId"):
            legacy[str(group["_id"])] = [
                str(schema["_id"])
                for schema in catalog
                if schema.get("processorId") == group["processorId"]
            ]

    def users(schema_id):
        return [
            group_label(group)
            for group in groups
            if group.get("schema_id") == schema_id
            or schema_id in legacy.get(str(group["_id"]), [])
        ]

    for key in selected:
        source = available[key]
        definition = source.get("definition")
        if not definition:
            errors.append(f"{source['name']}: {source.get('error', 'Invalid schema.')}")
            continue
        matches = [
            schema
            for schema in catalog
            if (
                schema.get("repo_origin", {}).get("collaborator")
                == package["source"]["collaborator"]
                and schema.get("repo_origin", {}).get("source_id") == key
            )
            or schema.get("name") == definition["name"]
            or (
                definition.get("processorId")
                and schema.get("processorId") == definition["processorId"]
            )
        ]
        entry = {
            "source_id": key,
            "name": definition.get("displayName") or definition["name"],
            "action": "added",
            "candidates": [],
            "groups": [],
        }
        for schema in matches:
            schema_id = str(schema["_id"])
            entry["candidates"].append(
                {
                    "schema_id": schema_id,
                    "name": schema.get("displayName") or schema["name"],
                    "diff": differences(schema, definition),
                    "groups": users(schema_id),
                }
            )
        decision = decisions.get(key)
        target = None
        if matches:
            if (
                len(matches) == 1
                and same_definition(matches[0], definition)
                and not decision
            ):
                target, entry["action"] = matches[0], "unchanged"
            elif not decision:
                entry["action"] = "conflict"
                retained.update(str(item["_id"]) for item in matches)
            else:
                target = next(
                    (
                        schema
                        for schema in matches
                        if str(schema["_id"]) == decision.get("schema_id")
                    ),
                    None,
                )
                if target is None:
                    raise rules.SchemaError(
                        "Choose which existing schema to keep or replace."
                    )
                entry["action"] = "kept" if decision["action"] == "keep" else "updated"
        elif decision:
            raise rules.SchemaError(
                "This schema has no conflict to resolve. Refresh the preview.", 409
            )
        if target is not None:
            target_id = str(target["_id"])
            if target_id in retained:
                errors.append(
                    f"More than one selected schema targets {target['name']}. Select one repo version for it."
                )
            retained.add(target_id)
            entry.update(
                schema_id=target_id,
                groups=users(target_id),
                diff=differences(target, definition),
            )
            if entry["action"] == "updated":
                # Internal names remain immutable, including processor-ID matches.
                after = {
                    **target,
                    **definition,
                    "name": target["name"],
                    "attributes": rules.retain_retired_fields(
                        target.get("attributes"), definition["attributes"]
                    ),
                }
                changes.append({"kind": "schema", "before": target, "after": after})
        elif entry["action"] == "added":
            after = {**definition, **creator, "_id": ObjectId()}
            entry["schema_id"] = str(after["_id"])
            entry["diff"] = differences({}, definition)
            retained.add(entry["schema_id"])
            changes.append({"kind": "schema", "before": None, "after": after})
        if entry["action"] in ("added", "updated"):
            changes[-1]["after"].update(
                repo_origin={**package["source"], "source_id": key},
                imported_at=now,
                updated_at=now,
                lastUpdated=now,
            )
        entries.append(entry)

    removed = [
        schema
        for schema in catalog
        if mode == "replace" and str(schema["_id"]) not in retained
    ]
    removed_ids = {str(schema["_id"]) for schema in removed}
    affected_ids = removed_ids | {
        str(op["before"]["_id"]) for op in changes if op["before"]
    }
    changed_pids = {schema.get("processorId") for schema in removed}
    for op in changes:
        changed_pids.update(
            (op["after"].get("processorId"), (op["before"] or {}).get("processorId"))
        )
    final_catalog = {
        **by_id,
        **{str(op["after"]["_id"]): op["after"] for op in changes},
    }
    for key in removed_ids:
        final_catalog.pop(key)
    bindings, affected, detaching = [], {}, []
    for group in groups:
        group_id = str(group["_id"])
        current_id = group.get("schema_id")
        target_id = current_id
        binding_needed = False
        if "schema_id" in group:
            if current_id in affected_ids:
                affected[group_id] = group_label(group)
            if current_id in removed_ids:
                target_id, binding_needed = None, True
        elif group.get("processorId") in changed_pids and group.get("processorId"):
            matches = legacy.get(group_id, [])
            if len(matches) > 1:
                errors.append(
                    f"Resolve the ambiguous schema for record group {group.get('name', group_id)} before importing."
                )
                continue
            if matches:
                current_id = matches[0]
                if group.get("attributes") and not same_definition(
                    {**by_id[current_id], "attributes": group["attributes"]},
                    by_id[current_id],
                ):
                    errors.append(
                        f"Migrate the different embedded schema for record group {group.get('name', group_id)} before importing."
                    )
                    continue
                target_id = None if current_id in removed_ids else current_id
            else:
                new_matches = [
                    key
                    for key, schema in final_catalog.items()
                    if schema.get("processorId") == group["processorId"]
                ]
                if len(new_matches) != 1:
                    errors.append(
                        f"Select a schema or detach record group {group.get('name', group_id)} before importing."
                    )
                    continue
                target_id = new_matches[0]
            binding_needed = True
        if binding_needed:
            bindings.append(
                {
                    "kind": "binding",
                    "before": group,
                    "after": {**group, "schema_id": target_id},
                }
            )
            affected[group_id] = group_label(group)
            if target_id is None:
                detaching.append(group_label(group))

    reconcile = [
        {"kind": "reconcile", "group_id": key}
        for key in affected
        if next(
            (
                group.get("schema_id") or (legacy.get(key) or [None])[0]
                for group in groups
                if str(group["_id"]) == key
            ),
            None,
        )
        in by_id
    ]
    early_bindings = [
        op
        for op in bindings
        if op["after"]["schema_id"] is None or op["after"]["schema_id"] in by_id
    ]
    late_bindings = [op for op in bindings if op not in early_bindings]
    operations = (
        reconcile
        + early_bindings
        + changes
        + late_bindings
        + [{"kind": "remove", "before": schema} for schema in removed]
        + [{"kind": "reconcile", "group_id": key} for key in affected]
    )
    return {
        "source": package["source"],
        "mode": mode,
        "entries": entries,
        "removed": [
            {
                "schema_id": str(schema["_id"]),
                "name": schema.get("displayName") or schema["name"],
                "groups": users(str(schema["_id"])),
            }
            for schema in removed
        ],
        "affected_groups": list(affected.values()),
        "detached_groups": detaching,
        "errors": errors,
        "can_apply": not errors
        and all(entry["action"] != "conflict" for entry in entries),
        "counts": {
            **{
                action: sum(entry["action"] == action for entry in entries)
                for action in ("added", "updated", "unchanged", "kept", "conflict")
            },
            "removed": len(removed),
            "detached": len(detaching),
        },
        "destructive": mode == "replace"
        or any(entry["action"] == "updated" for entry in entries)
        or bool(late_bindings),
        "operations": operations,
    }
