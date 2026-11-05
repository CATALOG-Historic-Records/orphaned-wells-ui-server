# Testing and Validation

Install `requirements-dev.txt`, then run:

```sh
python -m pytest ogrre/tests -q
python -m py_compile ogrre/main.py ogrre/routers/router.py ogrre/processing_worker.py ogrre/internal/directory_upload.py ogrre/internal/data_manager.py ogrre/internal/storage_api.py ogrre/internal/batch_document_processing.py ogrre/internal/processing_job_runner.py ogrre/internal/document_ai_api.py
```

Upload tests use an isolated MongoDB test double and mocked cloud calls. They do
not prove live GCS CORS, IAM, Kubernetes scheduling, Document AI compatibility,
or production memory usage; use the deployment smoke checks before rollout.

The GitHub Actions **Checks** workflow runs Black and **Backend tests (pytest)**
in separate, parallel jobs. Pytest uses Python 3.12 and the development
requirements, with pip downloads cached between runs; it needs no MongoDB
credentials or cloud credentials. The test job starts an isolated MongoDB 7
service and sets its blocking-sort limit to 32 MiB. Query regressions verify
index use, pagination/navigation, and retired-field filtering with disk spilling
disabled. Loading integration tests exercise projects, record groups, records,
columns, and schemas with `USE_DB_SCHEMAS` both disabled and enabled. Frontend
E2E testing starts only after both jobs pass, so backend failures are reported
before starting the Docker/browser suite.

To include these integration tests locally, set `OGRRE_TEST_MONGO_URI` to a
disposable local MongoDB (for example `mongodb://127.0.0.1:27029`) before running
pytest. The tests require localhost, create uniquely named test databases, and
remove those databases afterward. Without that variable, Mongo integration tests
are skipped. Do not point tests at staging or production.

For push and pull-request runs, E2E tests pair the triggering backend commit with
`main` in `CATALOG-Historic-Records/orphaned-wells-ui`. For coordinated changes,
select **Actions → Checks → Run workflow**, choose the backend branch in the
branch selector, and set `frontend_ref` to the frontend branch, tag, or commit.
Optionally set `frontend_repository` to a fork (`owner/repository`). Defaults
remain the upstream frontend's `main`; overrides affect only that manual run.

From this repository, matching branches can be tested with:

```sh
gh workflow run checks.yml --ref db-schemas -f frontend_ref=db-schemas
```

Add `-f frontend_repository=OWNER/orphaned-wells-ui` to select a fork. Private
repositories require the optional `CHECKOUT_TOKEN` secret with read access to
both repositories. The reusable E2E workflow definition remains on the upstream
frontend's `main`; the inputs select the application and test source checkouts.

GitHub requires the `workflow_dispatch` trigger to exist on the repository's
default branch before manual runs are available. Land these CI changes there
once; no temporary branch names need to be committed or removed for later runs.
The frontend's **App Tests** workflow has matching `backend_ref` and
`backend_repository` inputs and builds the selected backend from source.

