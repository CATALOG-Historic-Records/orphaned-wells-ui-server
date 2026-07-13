# OGRRE Backend Agent Guide

This repo is the FastAPI backend for OGRRE. The paired frontend repo is `../orphaned-wells-ui`.

## Practices

- Read nearby route and data-manager code before editing.
- Keep route handlers thin and business logic in `ogrre/internal/data_manager.py`.
- Preserve existing worktree changes; do not reset or revert unrelated files.
- Prefer `rg` for search.
- Avoid broad refactors while adding endpoint behavior.
- Remove unused, obsolete, or orphaned code when changes make it unnecessary.
- Do not leave commented-out code or dead code paths.
- Keep abstractions proportional to current needs and clear future extension points.
- Use concise comments or documentation for important files, complex functions, non-obvious logic, operational steps, and critical integration points.
- Keep generated files, dependency directories, and local artifacts out of source control unless they are intentionally part of the project.

## API Routes

- Add FastAPI routes in `ogrre/routers/router.py` on the existing `router`.
- Use `Depends(authenticate)` and enforce permissions with `data_manager.hasPermission(user_info["email"], "<permission>")`.
- Raise `HTTPException` with existing status-code conventions and clear detail strings.
- Parse JSON bodies defensively when optional, and validate incoming body types before calling data-manager methods.
- Use snake_case endpoint paths and match existing POST-for-mutation conventions.

## Data Manager

- Add service methods near related methods in `ogrre/internal/data_manager.py`.
- Apply trusted server-side scope constraints after parsing client-provided filters.
- Preserve deletion audit behavior by copying deleted documents to the relevant `deleted_*` collection before removal.
- Record meaningful mutations with `recordHistory(...)`.

## Verification

- Run `python -m py_compile` on touched backend files.
- Run backend tests if a test suite exists for the touched area.

## Repo-Local Skills

- Use `$ogrre-backend-api-route` from `.codex/skills/ogrre-backend-api-route` for backend route and data-manager work.
