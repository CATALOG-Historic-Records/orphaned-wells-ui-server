"""Bounded, conservative suggestions from stored attributes; never converts values."""

import math
import re
from collections import Counter

from ogrre.internal import schema_validation as rules


MAX_RECORD_BYTES = 256 * 1024
MAX_SAMPLE_BYTES = 8 * 1024 * 1024
MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_FIELDS = 500
MAX_DEPTH = 12
MAX_NODES = 100_000
INDEX = "schema_inference_records"


def sample_pipeline(group_id, limit):
    # The compound index supplies scope/order before any document is inspected.
    return [
        {"$match": {"record_group_id": group_id}},
        {"$sort": {"_id": 1}},
        {"$limit": limit + 1},
        {
            "$project": {
                "_id": 1,
                "size": {"$bsonSize": "$$ROOT"},
                "attributesList": {
                    "$cond": [
                        {"$lte": [{"$bsonSize": "$$ROOT"}, MAX_RECORD_BYTES]},
                        {"$ifNull": ["$attributesList", []]},
                        [],
                    ]
                },
            }
        },
    ]


def value_kind(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    if isinstance(value, bool):
        return "bool", None
    if isinstance(value, int):
        return "int", None
    if isinstance(value, float) and math.isfinite(value):
        return "float", None
    if isinstance(value, str):
        text = value.strip()
        if re.match(r"^[+-]?0\d", text):
            return "str", "Leading-zero values were kept as text."
        if text.lower() in {"true", "false"}:
            return "bool", "Boolean-looking text; review the suggested type."
        if re.fullmatch(r"[+-]?(?:0|[1-9]\d*)", text):
            return "int", "Numeric-looking text; review the suggested type."
        if re.fullmatch(r"[+-]?(?:\d+\.\d*|\d*\.\d+|\d+)(?:[eE][+-]?\d+)?", text):
            return "float", "Numeric-looking text; review the suggested type."
        if re.match(r"^\d{1,4}[-/]\d{1,2}[-/]\d{1,4}", text):
            return "str", "Date-like text was not interpreted as a date."
    return "str", None


def infer_fields(records, existing=()):
    known = {
        field["name"]: field
        for field in rules.normalize_fields(existing or [], strict=False)
    }
    retired = {name for name, field in known.items() if field.get("deleted")}
    observations = {}
    warnings = set()
    nodes = 0
    exhausted = object()

    def observe(name):
        if name not in observations:
            if len(observations) >= MAX_FIELDS:
                warnings.add(
                    f"Discovery stopped at {MAX_FIELDS} fields; other fields remain visible in records."
                )
                return None
            observations[name] = {"kinds": set(), "notes": set(), "multiple": False}
        return observations[name]

    for record in records:
        counts = Counter()
        attributes = record.get("attributesList") or []
        if not isinstance(attributes, list):
            warnings.add("A record with invalid stored attributes was skipped.")
            continue
        stack = [(iter(attributes), "", 1)]
        while stack and nodes < MAX_NODES:
            iterator, parent, depth = stack[-1]
            attribute = next(iterator, exhausted)
            if attribute is exhausted:
                stack.pop()
                continue
            nodes += 1
            if not isinstance(attribute, dict) or attribute.get("deleted"):
                continue
            key = attribute.get("key")
            if not isinstance(key, str) or not key or len(key) > 256:
                warnings.add(
                    "Some attributes have invalid or oversized field names and were skipped."
                )
                continue
            # Match the canonical paths used by existing record consumers,
            # including legacy subattributes stored at the top level.
            prefix = parent
            if not prefix and attribute.get("isSubattribute"):
                prefix = str(
                    attribute.get("parentAttribute")
                    or attribute.get("topLevelAttribute")
                    or ""
                )
            name = (
                key
                if not prefix or key == prefix or key.startswith(prefix + "::")
                else prefix + "::" + key
            )
            parts = name.split("::")
            if depth > MAX_DEPTH or len(parts) > MAX_DEPTH or len(name) > 1024:
                warnings.add(f"Fields beyond {MAX_DEPTH} nesting levels were skipped.")
                continue
            try:
                rules.field_name(name)
            except rules.SchemaError:
                warnings.add(
                    "Some field names cannot be used in a schema and were skipped."
                )
                continue
            paths = ["::".join(parts[:end]) for end in range(1, len(parts) + 1)]
            if any(path in retired for path in paths):
                continue
            if any(
                path in known and known[path].get("data_type") != "Parent"
                for path in paths[:-1]
            ):
                warnings.add(
                    "Some nested fields conflict with an existing non-Parent field; edit its type before adding them."
                )
                continue
            for path in paths[:-1]:
                ancestor = observe(path)
                if ancestor is not None:
                    ancestor["kinds"].add("parent")
            observation = observe(name)
            if observation is None:
                continue
            counts[name] += 1
            observation["multiple"] |= counts[name] > 1
            value = attribute.get(
                "value", attribute.get("normalized_value", attribute.get("raw_text"))
            )
            kind, note = value_kind(value)
            if kind:
                observation["kinds"].add(kind)
            if note:
                observation["notes"].add(note)
            children = attribute.get("subattributes") or []
            if isinstance(children, list) and children:
                observation["kinds"].add("parent")
                stack.append((iter(children), name, depth + 1))
        if nodes >= MAX_NODES:
            warnings.add(f"Discovery stopped after {MAX_NODES:,} attribute instances.")
            break

    fields, notes = [], {}
    order = max(
        (field.get("page_order_sort", 0) for field in known.values()), default=0
    )
    for name, observation in observations.items():
        if name in known:
            continue
        kinds, messages = observation["kinds"], observation["notes"]
        if "parent" in kinds:
            data_type, database_type = "Parent", "Table"
            if kinds - {"parent"}:
                messages.add(
                    "Both nested and scalar values were observed; review this Parent field. Stored values are unchanged."
                )
        elif kinds and kinds <= {"int", "float"}:
            data_type, database_type = "Number", "float" if "float" in kinds else "int"
        elif kinds == {"bool"}:
            data_type, database_type = "Checkbox", "bool"
        else:
            data_type, database_type = "Plain text", "str"
            if not kinds:
                messages.add(
                    "Only empty values were observed; text is a placeholder type."
                )
            elif len(kinds) > 1:
                messages.add("Conflicting value types were kept as text.")
        order += 1
        fields.append(
            {
                "name": name,
                "data_type": data_type,
                "database_data_type": database_type,
                "page_order_sort": order,
                "occurrence": "Optional multiple"
                if observation["multiple"]
                else "Optional single",
            }
        )
        if messages:
            notes[name] = sorted(messages)
    return fields, notes, sorted(warnings)
