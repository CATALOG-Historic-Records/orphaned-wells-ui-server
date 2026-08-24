---
name: ogrre-backend-development
description: Build, edit, review, or refactor the OGRRE FastAPI backend. Use for any implementation touching orphaned-wells-ui-server backend code, including FastAPI routes, auth/session/CSRF behavior, Pydantic settings, MongoDB/PyMongo data-manager logic, record/project/processor workflows, Google Document AI integration, Google Cloud or local storage behavior, tests, backend docs, deployment configuration, generated artifact cleanup, or backend dependency changes.
---

# OGRRE Backend Development

Use this skill for backend implementation in `orphaned-wells-ui-server`. Keep
changes explicit, scoped, testable, and consistent with the current
FastAPI/PyMongo package.

## Required Context

Before editing, inspect the relevant local patterns:

- `README.md`, `requirements.txt`, `requirements-dev.txt`, `pyproject.toml`,
  and `setup.py` before changing setup, dependencies, packaging, validation, or
  developer workflow.
- `ogrre/main.py` before changing app construction, middleware, CORS, static
  mounts, startup behavior, or server entrypoints.
- `ogrre/routers/router.py` before changing HTTP endpoints, auth/session/CSRF
  flows, uploads, streaming responses, background tasks, or frontend API
  contracts.
- `ogrre/internal/data_manager.py` before changing projects, record groups,
  records, processors, users, teams, roles, history, filtering, locks, review,
  clean, export, download, or deletion behavior.
- `ogrre/internal/mongodb_connection.py` before changing MongoDB connection
  behavior.
- `ogrre/internal/settings.py`, `ogrre/.env.example`, and deployment manifests
  or templates before changing configuration names or defaults. Do not read or
  expose real local secrets from `.env`, credential JSON, or service-key files.
- `ogrre/internal/storage_api.py` before changing document/image storage,
  storage URL generation, local storage, Google Cloud Storage, upload paths,
  deleted-file movement, or image rotation.
- `ogrre/internal/document_ai_api.py`, `google_processor_manager.py`,
  `image_handling.py`, `batch_document_processing.py`, and `bulk_upload.py`
  before changing document processing, processor deployment, imports, batch
  jobs, or custom Document AI behavior.
- `../orphaned-wells-ui` service callers when backend response shapes, endpoint
  names, permission names, auth behavior, upload behavior, or user-visible
  workflows change.
- For route and data-manager endpoint work, also read
  `.agents/skills/ogrre-backend-api-route/SKILL.md`.

Prefer the backend's existing module boundaries and helper functions over new
abstractions. Keep new logic close to the owning route, data-manager method,
storage helper, processor integration, or auth boundary unless shared behavior
is already clear.

## Backend Shape

Respect the current boundaries:

- `ogrre/main.py` wires the FastAPI app, CORS middleware, `/health`, routers,
  local-storage mounting, dotenv loading, and uvicorn entrypoint behavior.
- `ogrre/routers/router.py` owns HTTP shape: route declarations, request
  parsing, dependency injection, permission gates, cookie/session/CSRF handling,
  response construction, and HTTP exception translation.
- `ogrre/internal/data_manager.py` owns business rules and MongoDB-backed data
  behavior for projects, record groups, records, processors, users, teams,
  roles, history, notes, locks, review, clean, downloads, exports, and
  deletions.
- `ogrre/internal/mongodb_connection.py` owns MongoDB client construction from
  environment values.
- `ogrre/internal/storage_api.py` owns local and Google Cloud Storage behavior,
  canonical storage key layout, upload/download helpers, URL conversion,
  deleted-file movement, and image rotation.
- Document processing modules own Google Document AI and custom processor
  integration behavior.

Do not introduce repository, migration, or SQLAlchemy patterns; this backend
uses direct PyMongo collection access through `DataManager`.

## Configuration And Secrets

Treat runtime configuration as an explicit contract:

- Use existing dotenv/environment patterns unless a change requires a clearer
  contract through `AppSettings`.
- Keep `ogrre/.env.example`, deployment manifests, and README instructions
  coherent when adding or renaming environment variables.
- Keep local defaults safe for development only. Do not add production defaults
  for secrets, tokens, service-account paths, OAuth credentials, database
  credentials, privileged users, or broad CORS origins.
- Validate dangerous configuration combinations when a misconfiguration could
  weaken auth, storage, document processing, or deployment behavior.
- Never commit real `.env` files, credential JSON, service keys, API tokens, or
  copied secrets.

## Auth, Sessions, And Permissions

Do not weaken security flows:

- Preserve `REQUIRE_AUTH`, bearer-token and cookie session behavior, refresh
  cookies, CSRF checks for unsafe methods, anonymous user/team handling, and
  authenticated-admin route gates in `ogrre/routers/router.py`.
- Use `Depends(authenticate)` on protected routes and enforce server-side
  permissions with `data_manager.hasPermission(user_info["email"], "<permission>")`.
- Keep permission names consistent with frontend role data and existing backend
  names such as `create_project`, `create_record_group`, `upload_document`,
  `manage_project`, `review_record`, `clean_record`, `manage_schema`,
  `manage_team`, `manage_system`, and `delete`.
- Do not add development auth bypasses, fake users, unconditional admin access,
  permissive production CORS, or disabled CSRF behavior to runtime code.
- Keep auth errors clear enough for clients without logging or returning
  sensitive token, cookie, or credential material.

## API Contracts

Backend changes are consumed by the React frontend:

- Prefer additive response changes. When changing endpoint paths, request
  payloads, response shapes, status codes, cookie behavior, permissions, or file
  upload/download contracts, update the frontend service/caller or document the
  required paired change.
- Keep route handlers thin: authenticate, authorize, parse and validate request
  data, call `data_manager` or the owning integration helper, and return a small
  response.
- Raise `HTTPException` with existing status-code conventions and direct detail
  strings or structured details that callers can handle.
- Reject invalid body types or filters with `400`; do not allow malformed input
  to widen queries or trigger broad mutations.
- Use bounded pagination, explicit sorting, and safe limits for list, export,
  download, and batch-style workflows when touching them.

## MongoDB And Data Integrity

Keep persistence behavior reviewable:

- Use `DataManager` methods for Mongo-backed business logic. Add methods near
  related methods and preserve existing collection naming and document shapes.
- Treat client-provided filters as untrusted. Apply server-owned scope
  constraints after parsing client filters so callers cannot widen project,
  record group, team, or permission boundaries.
- Preserve deletion audit behavior: copy deleted documents to the relevant
  `deleted_*` collection and include deletion metadata before removing active
  documents.
- Preserve storage cleanup/move behavior when deleting projects, record groups,
  records, or images.
- Record meaningful mutations with `recordHistory(...)` or
  `recordHistoryBulk(...)` using established action names and metadata fields.
- Use `bson.ObjectId` conversion consistently for Mongo `_id` lookups and avoid
  mixing string IDs and ObjectIds without checking nearby patterns.
- Preserve record lock, review, clean, notes, attribute tree, and processor
  schema invariants when changing record workflows.

## Integrations And Files

Make external dependency behavior honest and observable:

- Keep Google Cloud Storage and local storage behavior separated behind
  `storage_api`; do not make callers assemble storage paths or public URLs when
  helper functions exist.
- Preserve canonical storage prefixes such as `uploads` and `deleted` and the
  current record image directory layout unless the migration/compatibility path
  is explicit.
- Keep Google Document AI as the default backend and preserve explicit custom
  `DOCUMENT_AI_BACKEND`/`DOCUMENT_AI_URL` behavior when changing document
  processing.
- Do not fake persistence, storage, processor deployment, Document AI results,
  batch processing, identity provider behavior, or signed/download URLs in
  production routes. Keep mocks inside tests or local-only harnesses.
- Add timeouts for new HTTP or network calls and avoid blocking IO inside async
  route handlers unless the nearby code already establishes the pattern.
- Log unexpected failures at an appropriate level, but never log secrets,
  bearer tokens, OAuth tokens, cookies, CSRF values, service-account contents,
  signed URLs, or raw credential material.

## Generated Artifacts And Cleanup

Keep source changes clean:

- Do not edit or commit `.env`, `.env_*` files containing real deployment
  values, `creds.json`, service-account JSON, `.pytest_cache/`, `__pycache__/`,
  `.DS_Store`, build outputs, local virtual environments, downloaded data, or
  generated exports unless explicitly required and safe.
- Remove unused imports, stale helpers, obsolete tests, redundant branches, and
  duplicated nearby code introduced or exposed by the change.
- Keep cleanup scoped to the backend files and ownership area being changed.
  Do not broaden into unrelated refactors or overwrite user changes.

## Documentation And Tests

Backend changes must leave docs and tests coherent:

- Update `README.md` when setup, dependency installation, runtime behavior,
  environment variables, credential requirements, storage mode, validation, or
  deployment expectations change.
- Update deployment docs/manifests when Kubernetes, Docker, nginx, Terraform,
  ports, health checks, storage, CORS, auth, or environment requirements change.
- Add or update focused tests when practical for request parsing, permissions,
  auth/session/CSRF behavior, data-manager rules, Mongo query construction,
  storage behavior, Document AI fallbacks, error handling, and regression-prone
  record workflows.
- Prefer deterministic unit tests around pure helpers and data-manager behavior.
  Use integration or smoke checks when real dependency behavior is the risk.

## Validation

Choose the narrowest validation that proves the change:

- For touched Python files, run `python -m py_compile <files>`.
- Run `python -m pytest` or focused pytest tests if a test suite exists for the
  touched area.
- Run `python -m black --check <files>` when formatting-sensitive Python edits
  are made and the environment has the dev dependency installed.
- For dependency or package changes, verify installation/build behavior with the
  narrowest practical package command.
- For docs- or skill-only changes, run the skill validator when applicable and
  `git diff --check`.
