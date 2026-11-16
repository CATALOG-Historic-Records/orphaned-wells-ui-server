# API Keys and Incremental Reviewed Record Exports

## Overview

Collaborators and automated scripts can authenticate with personal API keys (`Bearer ogrre_...`) and fetch newly reviewed or updated reviewed records through `GET /reviewed_records`. Runs are recorded in `db.export_jobs` alongside UI exports so watermark state is tracked automatically and past runs can be re-downloaded as ZIP archives from the UI.

## Personal API keys

### Storage and authentication

- Raw keys use the `ogrre_` prefix (`ogrre_<token>`) and are returned only once when created.
- Only the SHA-256 digest (`key_hash`) and display prefix (`key_prefix`, first 12 characters) are stored in `db.api_keys`.
- Each key document records:
  - `_id` / `key_id`
  - `name` (1–100 characters)
  - `key_prefix` and `key_hash`
  - `user_email`, `team`, and `collaborator`
  - `created_at`, `expires_at`, `last_used_at`, and `revoked_at`
- `authenticate` in `ogrre/routers/router.py` checks `Authorization: Bearer <token>`. When `<token>` starts with `ogrre_`, `data_manager.authenticateApiKey` validates that the key exists, is not revoked, is not expired, and belongs to an existing user in `db.users`, then updates `last_used_at` and resolves the user's current permissions and team context.

### Endpoints

All API key management endpoints require authentication (`Depends(authenticate)`):

| Endpoint | Purpose |
| --- | --- |
| `GET /api_keys` | List active (non-revoked) API keys for the authenticated user, sorted newest first. Never returns `key_hash` or the raw key. |
| `POST /api_keys` | Create a new API key from `{"name": "<label>", "expires_in_days": 30 \| 90 \| 365}` (1–365 days, default `90`). Returns the plaintext `api_key` once. |
| `POST /api_keys/{key_id}/revoke` | Soft-revoke a key owned by the caller by setting `revoked_at`. |

Creation and revocation events are recorded in `db.history` (`createApiKey` and `revokeApiKey`).

## Incremental reviewed records export (`GET /reviewed_records`)

### Record update timestamps

- `data_manager.updateRecord` sets top-level `"lastUpdated": time.time()` on `db.records` whenever a record is updated (including attribute edits, cleaning, and review status changes).
- `"lastUpdated"` is included in `util.RECORD_METADATA_FIELDS` so top-level record updates persist it rather than routing it into `attributesList`.
- For older records created before `lastUpdated` was populated, time-window queries fall back to `dateCreated` when `lastUpdated` is missing or `None`.

### Query parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `since` | float / ISO-8601 string | Last watermark `until` (or `0`) | Exclusive lower bound (`> since`) on `lastUpdated` (fallback `dateCreated`). |
| `until` | float / ISO-8601 string | Request time (`now`) | Inclusive upper bound (`<= until`) on `lastUpdated` (fallback `dateCreated`). |
| `project_id` | string (`ObjectId`) | All accessible projects in team | Restrict results and watermark tracking to a single authorized project. |
| `record_group_id` | string (`ObjectId`) | All accessible groups in scope | Restrict results and watermark tracking to a single authorized record group. |
| `document_type` | string | All document types | Restrict results and watermark tracking to record groups matching `documentType`. |
| `page` | int (`>= 0`) | `0` | Zero-based page index. |
| `page_size` | int (`1–1000`) | `100` | Maximum records returned per page. |
| `format` | `"json"` \| `"csv"` | `"json"` | Response format (`application/json` or `text/csv` attachment). |

### Watermark behavior and `db.export_jobs` integration

1. **Automatic watermark lookup**:
   - When `since` is omitted, `fetchReviewedRecordsExport` queries `db.export_jobs` for the most recent document matching:
     - `request_user.email`
     - `team`
     - `location` (`"team"`, `"project"`, or `"record_group"`) and `target_id`
     - `export_source: "reviewed_records_api"`
     - `options.document_types`
     - `is_watermark: True`
   - `since` defaults to that job's `options.until` timestamp, or `0.0` on the first run for that user and scope.
2. **Recording runs in `db.export_jobs`**:
   - On `page == 0`, `fetchReviewedRecordsExport` inserts a summary document into `db.export_jobs` with:
     - `export_source: "reviewed_records_api"`
     - `status: "expired"` and `stage: "completed"` (since data was streamed directly in the HTTP response rather than stored as a GCS ZIP artifact)
     - `is_watermark: True` when `until` was omitted (advances the watermark to `now`), or `False` when a custom `until` bound was supplied (prevents historical backfill queries from moving the live watermark backward)
     - `options.since`, `options.until`, and `options.record_ids` (the exact list of matched record IDs across the window)
3. **Deterministic re-runs from the UI**:
   - Because runs are stored in `db.export_jobs` with `options.record_ids`, clicking **Run again** (`POST /export_jobs/{job_id}/rerun`) queues a background export job that packages the exact same set of records into a downloadable ZIP archive, even if those records have been edited again since the original API call.
