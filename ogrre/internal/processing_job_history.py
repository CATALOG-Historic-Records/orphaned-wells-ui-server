"""Bounded queries and public summaries for upload history."""

import math
import re

ACTIVE_STATUSES = ["queued", "dispatched", "running"]
TERMINAL_STATUSES = ["completed", "completed_with_errors", "error"]


def history_query(body):
    if not isinstance(body, dict):
        raise ValueError("Expected a history query object")
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
    filters = body.get("filter", {})
    if not isinstance(filters, dict):
        raise ValueError("Expected a filter object")
    query = {}
    for key, value in filters.items():
        if key in ("status", "source_type"):
            choices = TERMINAL_STATUSES if key == "status" else ["directory", "gcs"]
            if (
                not isinstance(value, dict)
                or set(value) != {"$in"}
                or not isinstance(value["$in"], list)
                or any(item not in choices for item in value["$in"])
            ):
                raise ValueError(f"Invalid {key} filter")
            if key == "status":
                query[key] = value
            elif len(set(value["$in"])) == 1:
                query["input.upload_session_id"] = {
                    "$exists": value["$in"][0] == "directory"
                }
            elif not value["$in"]:
                query["_id"] = {"$in": []}
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
                raise ValueError("Invalid uploader filter")
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
            raise ValueError(f"Unsupported history filter: {key}")
    query.setdefault("status", {"$in": TERMINAL_STATUSES})
    return pagination, query


def summary_projection():
    fields = [
        "job_id",
        "record_group_id",
        "status",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "last_progress_at",
        "stage",
        "attempt",
        "error",
        "batches_total",
        "batches_completed",
        "request_user.email",
        "request_user.name",
        "input.bucket_name",
        "input.prefix",
        "input.upload_session_id",
        "input.upload_expires_at",
        "summary.total_submitted",
        "summary.total_succeeded",
        "summary.total_failed",
        "summary.total_skipped_duplicates",
    ]
    return {
        **dict.fromkeys(fields, 1),
        "source_type": {
            "$cond": [
                {"$ifNull": ["$input.upload_session_id", False]},
                "directory",
                "gcs",
            ]
        },
        "file_count": {
            "$cond": [
                {"$isArray": "$input.documents"},
                {"$size": "$input.documents"},
                {
                    "$ifNull": [
                        "$file_count",
                        {
                            "$cond": [
                                {"$gt": ["$summary.total_submitted", 0]},
                                "$summary.total_submitted",
                                None,
                            ]
                        },
                    ]
                },
            ]
        },
    }
