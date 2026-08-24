---
name: ogrre-backend-api-route
description: Add or update OGRRE backend API behavior. Use when implementing FastAPI routes in ogrre/routers/router.py, adding data_manager service methods, enforcing permissions, accepting filters or request bodies, deleting/updating/fetching records, or wiring new backend functionality for the React frontend.
---

# OGRRE Backend API Route

## Workflow

1. Inspect nearby routes in `ogrre/routers/router.py` and the matching business logic in `ogrre/internal/data_manager.py`.
2. Keep routes thin: authenticate, check permission, parse/validate request data, call `data_manager`, and return a small JSON response.
3. Add business logic to `data_manager` using the existing collection and history patterns.
4. Use `Depends(authenticate)` and `data_manager.hasPermission(user_info["email"], "<permission>")` for protected mutations.
5. Raise `HTTPException` with existing status-code conventions and user-facing detail strings.
6. Validate touched Python files with `python -m py_compile ...`; run backend tests if present.

## Route Patterns

- Define routes on the existing `router` in `router.py`.
- Use snake_case endpoint paths matching existing names, usually `POST` for mutations.
- Read JSON bodies with `await request.json()` and default defensively when a body is optional.
- Validate incoming body types before passing them to data-manager methods.
- Keep permission names consistent with the frontend and roles data, such as `delete`, `manage_project`, `review_record`, or `clean_record`.

## Data Manager Patterns

- Prefer adding a clearly named method near related operations in `data_manager.py`.
- Use server-side constraints for scope-sensitive operations. Apply trusted constraints, such as `record_group_id`, after reading client-provided filters so the client cannot widen the query.
- Preserve delete behavior by moving documents into the relevant `deleted_*` collection before deleting from the active collection.
- Record mutations with `recordHistory(...)` using the established action names and metadata fields.
- Avoid broad refactors while adding a route. Keep behavior scoped to the requested endpoint.

## Record Filters

- Existing frontend filters arrive as Mongo-like filter objects under `filter`.
- Combine filter objects with required server-owned constraints by adding the constraint at the top level, which Mongo treats as an AND with `$or` or `$nor` filters.
- Reject non-object filters with `400` rather than allowing accidental broad operations.
