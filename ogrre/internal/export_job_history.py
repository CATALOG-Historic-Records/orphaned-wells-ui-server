"""Bounded queries and public summaries for export history."""

import math
import re

ACTIVE_STATUSES = ["queued", "dispatched", "running"]
TERMINAL_STATUSES = ["completed", "error", "expired"]
ALL_STATUSES = ACTIVE_STATUSES + TERMINAL_STATUSES


def export_history_query(body):
    if not isinstance(body, dict):
        raise ValueError("Expected an export history query object")
    pagination = {}
    for key, default, maximum in (
        ("page", 0, 100000),
        ("active_page", 0, 100000),
        ("page_size", 25, 100),
    ):
        value = body.get(key, default)
        if (
            type(value) is not int
            or value < (1 if key == "page_size" else 0)
            or value > maximum
        ):
            raise ValueError(f"Invalid {key}")
        pagination[key] = value

    view_scope = body.get("view_scope", "mine")
    if view_scope not in ("mine", "team"):
        raise ValueError("Invalid view_scope")

    filters = body.get("filter", {})
    if not isinstance(filters, dict):
        raise ValueError("Expected a filter object")
    query = {}
    for key, value in filters.items():
        if key == "status":
            if (
                not isinstance(value, dict)
                or set(value) != {"$in"}
                or not isinstance(value["$in"], list)
                or any(item not in ALL_STATUSES for item in value["$in"])
            ):
                raise ValueError("Invalid status filter")
            query[key] = value
        elif key == "request_user.email":
            if isinstance(value, str) and len(value) <= 320:
                query[key] = value
            elif (
                isinstance(value, dict)
                and set(value) == {"$regex"}
                and isinstance(value["$regex"], str)
                and len(value["$regex"]) <= 320
            ):
                query[key] = {"$regex": re.escape(value["$regex"]), "$options": "i"}
            else:
                raise ValueError("Invalid requester filter")
        elif key == "created_at":
            if (
                not isinstance(value, dict)
                or not value
                or not set(value) <= {"$gte", "$gt", "$lt"}
                or any(
                    type(item) not in (int, float) or not math.isfinite(item)
                    for item in value.values()
                )
            ):
                raise ValueError("Invalid submission date filter")
            query[key] = value
        else:
            raise ValueError(f"Unsupported export history filter: {key}")
    query.setdefault("status", {"$in": TERMINAL_STATUSES})
    raw_search = body.get("search")
    if raw_search is not None:
        if not isinstance(raw_search, str) or len(raw_search) > 320:
            raise ValueError("Invalid search query")
        cleaned_search = raw_search.strip()
        if cleaned_search:
            pattern = {"$regex": re.escape(cleaned_search), "$options": "i"}
            query["$or"] = [
                {"output_name": pattern},
                {"artifact.filename": pattern},
                {"project_name": pattern},
                {"record_group_name": pattern},
                {"request_user.email": pattern},
                {"request_user.name": pattern},
            ]
    return pagination, query, view_scope


def summary_projection():
    fields = [
        "job_id",
        "job_type",
        "status",
        "stage",
        "status_text",
        "progress",
        "team",
        "location",
        "target_id",
        "project_id",
        "project_name",
        "record_group_id",
        "record_group_name",
        "output_name",
        "scope",
        "request_user.email",
        "request_user.name",
        "options.export_csv",
        "options.export_json",
        "options.export_images",
        "options.export_embedded_pdfs",
        "options.export_types",
        "options.reconstruct_original_doc",
        "options.export_raw_values",
        "options.document_types",
        "artifact.filename",
        "artifact.size_bytes",
        "artifact.record_count",
        "created_at",
        "updated_at",
        "started_at",
        "last_progress_at",
        "completed_at",
        "expires_at",
        "notification_read_at",
        "error",
        "attempt",
    ]
    return {
        **dict.fromkeys(fields, 1),
        "column_count": {
            "$cond": [
                {"$isArray": "$options.columns"},
                {"$size": "$options.columns"},
                0,
            ]
        },
    }
