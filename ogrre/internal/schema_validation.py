"""Shared schema normalization and mutation validation; no database side effects."""

import math
import copy


DESTRUCTIVE_PERMISSION = "manage_schema_destructive"
SAFE_FIELD_UPDATES = {
    "alias",
    "cleaning_function",
    "page_order_sort",
    "data_type",
    "database_data_type",
}
FIELD_UPDATES = SAFE_FIELD_UPDATES | {"name"}
FIELD_KEYS = FIELD_UPDATES | {
    "occurrence",
    "grouping",
    "accepted_range",
    "field_specific_notes",
    "model_enabled",
}
TYPE_OPTIONS = {
    "Checkbox": {"bool"},
    "Plain text": {"str", "int", "float", "date", "bool"},
    "Datetime": {"date", "str"},
    "Parent": {"Table"},
    "Number": {"int", "float", "str"},
    "Address": {"str"},
}
TYPE_ALIASES = {key.lower(): key for key in TYPE_OPTIONS}


class SchemaError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


class SchemaLookupError(SchemaError):
    """A schema binding has no unambiguous definition in the active source."""


def field_name(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise SchemaError(
            "Field names must be non-empty strings without surrounding spaces."
        )
    if any(not part or part != part.strip() for part in value.split("::")):
        raise SchemaError(
            "Use parent::child for nested field paths; empty path segments are invalid."
        )
    if any(char in value for char in ("\x00", ".", "$")):
        raise SchemaError("Field names cannot contain '.', '$', or null characters.")
    return value


def normalize_fields(fields, strict=True):
    if not isinstance(fields, list):
        raise SchemaError("Schema attributes must be an array of objects.")
    result = []
    for item in fields:
        if not isinstance(item, dict):
            raise SchemaError("Every schema field must be an object.")
        normalized = {}
        for key, value in item.items():
            canonical = key.strip().lower().replace(" ", "_")
            if canonical == "google_data_type":
                canonical = "data_type"
            if canonical == "deleted":
                if value is True:
                    normalized["deleted"] = True
                continue
            if canonical in FIELD_KEYS and value is not None and value != "":
                if (
                    canonical == "data_type"
                    and "data_type" in item
                    and key != "data_type"
                ):
                    continue
                normalized[canonical] = value
        if "name" in normalized and strict:
            field_name(normalized["name"])
        if isinstance(normalized.get("data_type"), str):
            value = normalized["data_type"].replace("_", " ").strip().lower()
            normalized["data_type"] = TYPE_ALIASES.get(value, normalized["data_type"])
        if isinstance(normalized.get("database_data_type"), str):
            value = normalized["database_data_type"].strip().lower()
            normalized["database_data_type"] = "Table" if value == "table" else value
        if "page_order_sort" in normalized:
            value = normalized["page_order_sort"]
            try:
                order = float(value)
                if (
                    isinstance(value, bool)
                    or not math.isfinite(order)
                    or not order.is_integer()
                    or order <= 0
                ):
                    raise ValueError()
                normalized["page_order_sort"] = int(order)
            except (ValueError, TypeError, OverflowError):
                if strict:
                    raise SchemaError("Page order must be an integer greater than 0.")
                normalized.pop("page_order_sort", None)
        result.append(normalized)
        children = item.get("subattributes") or []
        if children:
            if not normalized.get("name"):
                raise SchemaError("A parent field must have a name.")
            for child in normalize_fields(children, strict=strict):
                prefix = normalized["name"] + "::"
                child_name = child.get("name", "")
                child["name"] = (
                    child_name if child_name.startswith(prefix) else prefix + child_name
                )
                result.append(child)
    return result


def validate_field(field, cleaning_functions, require_types=True):
    field_name(field.get("name"))
    for key in FIELD_KEYS - {"page_order_sort", "name"}:
        if key in field and not isinstance(field[key], str):
            raise SchemaError(f"{key} must be a string.")
    if require_types:
        data_type = field.get("data_type")
        database_type = field.get("database_data_type")
        if (
            data_type not in TYPE_OPTIONS
            or database_type not in TYPE_OPTIONS[data_type]
        ):
            raise SchemaError(
                f"Unsupported data type combination for '{field['name']}'."
            )
    cleaning = field.get("cleaning_function")
    if cleaning and cleaning not in cleaning_functions:
        raise SchemaError(f"Unknown cleaning function: {cleaning}.")


def validate_structure(fields):
    by_name = {}
    for field in fields:
        name = field_name(field.get("name"))
        if name in by_name:
            raise SchemaError(f"Duplicate schema field: {name}.", 409)
        by_name[name] = field
    for name in by_name:
        if "::" not in name:
            continue
        parent = by_name.get(name.rsplit("::", 1)[0])
        if not parent or parent.get("data_type") != "Parent":
            raise SchemaError(
                f"Field '{name}' requires a Parent field at its parent path."
            )


def validate_fields(fields, cleaning_functions, require_types=True):
    fields = normalize_fields(fields)
    for field in fields:
        if field.get("deleted"):
            raise SchemaError("Use the field-removal action to retire schema fields.")
        # Import packages may omit type hints, but supplied hints must be valid.
        data_type = field.get("data_type")
        database_type = field.get("database_data_type")
        validate_field(
            field,
            cleaning_functions,
            require_types=require_types
            or (data_type is not None and database_type is not None),
        )
        if data_type is not None and data_type not in TYPE_OPTIONS:
            raise SchemaError(f"Unsupported data type for '{field.get('name')}'.")
        if database_type is not None and database_type not in set().union(
            *TYPE_OPTIONS.values()
        ):
            raise SchemaError(
                f"Unsupported database data type for '{field.get('name')}'."
            )
    validate_structure(fields)
    return fields


def retain_retired_fields(previous, replacement):
    """Persist removed paths, including descendants, alongside active definitions."""
    result = copy.deepcopy(replacement)
    names = {field["name"] for field in replacement}
    for field in normalize_fields(previous or [], strict=False):
        if field["name"] not in names:
            result.append({**field, "deleted": True})
    return result
