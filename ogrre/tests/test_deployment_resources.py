"""Execute the deploy-target parser with synthetic inputs; no cloud access."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="Deployment parser checks require bash and jq",
)


@pytest.fixture
def parse_target(tmp_path):
    workflow_path = (
        Path(__file__).resolve().parents[2]
        / ".github/workflows/deploy-k8s-dispatch.yml"
    )
    workflow = yaml.safe_load(workflow_path.read_text())
    script = next(
        step["run"]
        for step in workflow["jobs"]["deploy"]["steps"]
        if step.get("id") == "target"
    )
    (tmp_path / "deployment/secrets").mkdir(parents=True)
    output = tmp_path / "github-output"

    def parse(environment, resources=None, raw=None):
        target = {
            "cluster_name": "test-cluster",
            "cluster_location": "us-central1",
            "namespace": f"uow-{environment}",
            "host": "backend.example.com",
            "static_ip_name": "test-ip",
            "storage_bucket_name": "test-uploads",
            **(resources or {}),
        }
        (tmp_path / "deployment/secrets/k8s-deploy-targets.json").write_text(
            json.dumps({environment: target}) if raw is None else raw
        )
        subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", script],
            cwd=tmp_path,
            env={
                "PATH": os.environ["PATH"],
                "DEPLOY_ENV": environment,
                "GITHUB_OUTPUT": str(output),
            },
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return dict(line.split("=", 1) for line in output.read_text().splitlines())

    return parse


@pytest.mark.parametrize(
    (
        "environment,replicas,api_cpu_request,api_cpu_limit,"
        "api_memory_request,api_memory_limit,worker_cpu,worker_memory"
    ),
    [
        ("staging", "1", "500m", "1", "1Gi", "2Gi", "1", "6Gi"),
        ("isgs", "2", "1", "1", "4Gi", "4Gi", "1850m", "12Gi"),
    ],
)
def test_defaults_size_api_and_worker_independently(
    parse_target,
    environment,
    replicas,
    api_cpu_request,
    api_cpu_limit,
    api_memory_request,
    api_memory_limit,
    worker_cpu,
    worker_memory,
):
    target = parse_target(environment)
    assert target["replicas"] == replicas
    assert target["api_uvicorn_workers"] == "2"
    assert target["cpu_request"] == api_cpu_request
    assert target["cpu_limit"] == api_cpu_limit
    assert target["memory_request"] == api_memory_request
    assert target["memory_limit"] == api_memory_limit
    for field in ("request", "limit"):
        assert target[f"processing_job_cpu_{field}"] == worker_cpu
        assert target[f"processing_job_memory_{field}"] == worker_memory


@pytest.mark.parametrize("environment", ["staging", "isgs"])
@pytest.mark.parametrize(
    "cpu,memory_request,memory_limit",
    [("1", "4Gi", "4Gi"), ("500m", "1Gi", "4Gi"), ("1", "6Gi", "6Gi")],
)
def test_explicit_resources_are_preserved(
    parse_target, environment, cpu, memory_request, memory_limit
):
    resources = {
        "cpu_request": cpu,
        "cpu_limit": cpu,
        "memory_request": memory_request,
        "memory_limit": memory_limit,
        "processing_job_cpu_request": "2",
        "processing_job_cpu_limit": "2",
        "processing_job_memory_request": "14Gi",
        "processing_job_memory_limit": "14Gi",
    }
    target = parse_target(environment, resources)
    assert {key: target[key] for key in resources} == resources


@pytest.mark.parametrize(
    "raw", ["", "not json", "[]", "{}", '{"ca": null}', '{"ca": []}']
)
def test_missing_disabled_or_malformed_target_stops_deployment(parse_target, raw):
    with pytest.raises(subprocess.CalledProcessError):
        parse_target("ca", raw=raw)


@pytest.mark.parametrize("value", ["", None, [], {}, "host\ninjected_output=value"])
def test_invalid_required_field_stops_deployment(parse_target, value):
    with pytest.raises(subprocess.CalledProcessError):
        parse_target("staging", {"host": value})


def test_runtime_env_defaults_and_overrides(tmp_path):
    workflow_path = (
        Path(__file__).resolve().parents[2]
        / ".github/workflows/deploy-k8s-dispatch.yml"
    )
    workflow = yaml.safe_load(workflow_path.read_text())
    script = next(
        step["run"]
        for step in workflow["jobs"]["deploy"]["steps"]
        if step.get("id") == "runtime_env"
    )
    (tmp_path / "deployment/secrets").mkdir(parents=True)

    base_env = {
        "PATH": os.environ["PATH"],
        "DEPLOY_ENV": "boots",
        "HOSTNAME": "boots-server.uow-carbon.org",
        "NAMESPACE": "uow-boots",
        "IMAGE": "example/image:sha",
        "STORAGE_BUCKET_NAME": "boots_uploads",
        "API_UVICORN_WORKERS": "2",
        "PROCESSING_JOB_CPU_REQUEST": "1850m",
        "PROCESSING_JOB_MEMORY_REQUEST": "12Gi",
        "PROCESSING_JOB_CPU_LIMIT": "1850m",
        "PROCESSING_JOB_MEMORY_LIMIT": "12Gi",
        "PROCESSING_JOB_EPHEMERAL_STORAGE": "10Gi",
        "PROCESSING_JOB_ACTIVE_DEADLINE_SECONDS": "86400",
        "PROCESSING_JOB_TTL_SECONDS_AFTER_FINISHED": "604800",
        "PROCESSING_JOB_MAX_ACTIVE": "1",
        "PROJECT_ID": "tidy-outlet-412020",
        "GOOGLE_CLIENT_ID": "repo-client-id",
        "GOOGLE_CLIENT_SECRET": "repo-client-secret",
        "RUNTIME_ENV_CONTENT": (
            "DB_NAME=ogrre_boots\n"
            "DB_CONNECTION=cluster0.example.mongodb.net\n"
            "DB_USERNAME=boots_user\n"
            "DB_PASSWORD=boots_pass\n"
        ),
    }

    subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        cwd=tmp_path,
        env=base_env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    rendered = dict(
        line.split("=", 1)
        for line in (tmp_path / "deployment/secrets/runtime.k8s.env")
        .read_text()
        .splitlines()
    )
    assert rendered["DB_NAME"] == "ogrre_boots"
    assert rendered["COLLABORATOR"] == "boots"
    assert rendered["ALLOWED_ORIGINS"] == "https://boots.uow-carbon.org"
    assert rendered["REQUIRE_AUTH"] == "true"
    assert rendered["AUTH_PROVIDER"] == "google"
    assert rendered["token_uri"] == "https://oauth2.googleapis.com/token"
    assert rendered["client_id"] == "repo-client-id"
    assert rendered["client_secret"] == "repo-client-secret"
    assert rendered["SESSION_COOKIE_DOMAIN"] == ".uow-carbon.org"
    assert rendered["SESSION_COOKIE_SECURE"] == "true"
    assert rendered["SESSION_COOKIE_SAMESITE"] == "lax"
    assert rendered["PROJECT_ID"] == "tidy-outlet-412020"
    assert rendered["LOCATION"] == "us"
    assert rendered["STORAGE_BACKEND"] == "google"
    assert rendered["DOCUMENT_AI_BACKEND"] == "google"
    assert rendered["USE_DB_SCHEMAS"] == "false"

    # Explicit RUNTIME_ENV values override the defaults
    override_env = {
        **base_env,
        "RUNTIME_ENV_CONTENT": (
            "DB_NAME=ogrre_boots\n"
            "DB_CONNECTION=cluster0.example.mongodb.net\n"
            "DB_USERNAME=boots_user\n"
            "DB_PASSWORD=boots_pass\n"
            "COLLABORATOR=custom_collab\n"
            "USE_DB_SCHEMAS=true\n"
            "client_id=custom-client-id\n"
        ),
    }
    subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        cwd=tmp_path,
        env=override_env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    rendered_override = dict(
        line.split("=", 1)
        for line in (tmp_path / "deployment/secrets/runtime.k8s.env")
        .read_text()
        .splitlines()
    )
    assert rendered_override["COLLABORATOR"] == "custom_collab"
    assert rendered_override["USE_DB_SCHEMAS"] == "true"
    assert rendered_override["client_id"] == "custom-client-id"
    assert rendered_override["client_secret"] == "repo-client-secret"

    # Missing client_id/client_secret or DB credentials fails fast (and emits Atlas warning when DB is missing)
    for missing_env in (
        {**base_env, "GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": ""},
        {**base_env, "RUNTIME_ENV_CONTENT": ""},
    ):
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", script],
                cwd=tmp_path,
                env=missing_env,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        if not missing_env.get("RUNTIME_ENV_CONTENT"):
            assert "Automatic MongoDB provisioning skipped" in exc_info.value.stdout


def test_provision_mongodb_guards_against_existing_user_and_non_empty_db():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "provision_mongodb",
        Path(__file__).resolve().parents[2] / "deployment/ci/provision_mongodb.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class FakeCollection:
        def __init__(self, count=0):
            self.count = count
            self.inserted = []

        def count_documents(self, query, limit=None):
            return self.count

        def insert_many(self, docs):
            self.inserted.extend(docs)

        def insert_one(self, doc):
            self.inserted.append(doc)

    class FakeDB:
        def __init__(self, name, collections=None):
            self.name = name
            self._collections = collections or []
            self.roles = FakeCollection()
            self.teams = FakeCollection()
            self.users = FakeCollection()
            self._by_name = {
                "roles": self.roles,
                "teams": self.teams,
                "users": self.users,
                "projects": FakeCollection(),
                "record_groups": FakeCollection(),
                "records": FakeCollection(),
            }

        def list_collection_names(self):
            return list(self._collections)

        def __getitem__(self, key):
            return self._by_name[key]

    # Non-empty DB is never modified
    non_empty_db = FakeDB("ogrre-isgs", collections=["records", "users"])
    assert (
        mod.initialize_empty_database(non_empty_db, "ISGS", ["admin@example.com"])
        is False
    )
    assert non_empty_db.roles.inserted == []
    assert non_empty_db.teams.inserted == []
    assert non_empty_db.users.inserted == []

    # Empty DB is initialized cleanly
    empty_db = FakeDB("ogrre-boots", collections=[])
    assert (
        mod.initialize_empty_database(
            empty_db, "BOOTS", ["Admin1@Example.com, admin2@example.com"]
        )
        is True
    )
    assert len(empty_db.roles.inserted) == len(mod.ROLES)
    assert empty_db.teams.inserted == [
        {
            "name": "BOOTS",
            "display_name": "BOOTS",
            "users": ["admin1@example.com", "admin2@example.com"],
            "project_list": [],
        }
    ]
    assert [u["email"] for u in empty_db.users.inserted] == [
        "admin1@example.com",
        "admin2@example.com",
    ]

    # Existing Atlas user is never overwritten
    class FakeResponse:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload or {}

        def json(self):
            return self._payload

    class FakeSession:
        def __init__(self, get_status, post_status=201):
            self.get_status = get_status
            self.post_status = post_status
            self.posts = []

        def get(self, url, **kwargs):
            return FakeResponse(self.get_status)

        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return FakeResponse(self.post_status, {"access_token": "tok"})

    with pytest.raises(RuntimeError, match="already exists"):
        mod.create_atlas_database_user(
            project_id="proj1",
            public_key="pub",
            private_key="priv",
            db_name="ogrre-isgs",
            username="ogrre-isgs",
            password="pw",
            session=FakeSession(get_status=200),
        )

    session = FakeSession(get_status=404, post_status=201)
    mod.create_atlas_database_user(
        project_id="proj1",
        public_key="pub",
        private_key="priv",
        db_name="ogrre-boots",
        username="ogrre-boots",
        password="pw",
        session=session,
    )
    assert len(session.posts) == 1
    assert session.posts[0][1]["json"]["roles"] == [
        {"roleName": "readWrite", "databaseName": "ogrre-boots"}
    ]


def test_runtime_env_priority_2_gcs_and_k8s_and_priority_3_provisioning(tmp_path):
    """Simulate Priority 2 (GCS and GKE secret reuse) and Priority 3 (first-time Atlas provisioning)."""
    workflow_path = (
        Path(__file__).resolve().parents[2]
        / ".github/workflows/deploy-k8s-dispatch.yml"
    )
    workflow = yaml.safe_load(workflow_path.read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    runtime_script = next(
        step["run"] for step in steps if step.get("id") == "runtime_env"
    )
    create_secrets_script = next(
        step["run"] for step in steps if step.get("name") == "Create deployment secrets"
    )

    (tmp_path / "deployment/secrets").mkdir(parents=True)
    (tmp_path / "deployment/terraform").mkdir(parents=True)
    (tmp_path / "deployment/terraform/backend.tf").write_text(
        'terraform {\n  backend "gcs" {\n    bucket = "test-state-bucket"\n  }\n}\n'
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    github_output = tmp_path / "github-output"

    base_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DEPLOY_ENV": "boots",
        "HOSTNAME": "boots-server.uow-carbon.org",
        "NAMESPACE": "uow-boots",
        "IMAGE": "example/image:sha",
        "STORAGE_BUCKET_NAME": "boots_uploads",
        "API_UVICORN_WORKERS": "2",
        "PROCESSING_JOB_CPU_REQUEST": "1850m",
        "PROCESSING_JOB_MEMORY_REQUEST": "12Gi",
        "PROCESSING_JOB_CPU_LIMIT": "1850m",
        "PROCESSING_JOB_MEMORY_LIMIT": "12Gi",
        "PROCESSING_JOB_EPHEMERAL_STORAGE": "10Gi",
        "PROCESSING_JOB_ACTIVE_DEADLINE_SECONDS": "86400",
        "PROCESSING_JOB_TTL_SECONDS_AFTER_FINISHED": "604800",
        "PROCESSING_JOB_MAX_ACTIVE": "1",
        "PROJECT_ID": "tidy-outlet-412020",
        "GOOGLE_CLIENT_ID": "repo-client-id",
        "GOOGLE_CLIENT_SECRET": "repo-client-secret",
        "REPO_DB_CONNECTION": "cluster0.example",
        "RUNTIME_ENV_CONTENT": "",
        "GITHUB_OUTPUT": str(github_output),
    }

    # --- Case 2a: Priority 2 loads existing snapshot from GCS when RUNTIME_ENV is empty ---
    gcloud_stub = bin_dir / "gcloud"
    gcloud_stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "storage" ] && [ "$2" = "cat" ] && [ "$3" = "gs://test-state-bucket/secrets/boots.env" ]; then\n'
        '  printf "DB_NAME=ogrre-boots\\nDB_CONNECTION=cluster0.example\\nDB_USERNAME=ogrre-boots\\nDB_PASSWORD=gcs-saved-pass\\n"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    gcloud_stub.chmod(0o755)

    res_gcs = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", runtime_script],
        cwd=tmp_path,
        env=base_env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "Loaded existing runtime environment snapshot" in res_gcs.stdout
    lines = (tmp_path / "deployment/secrets/runtime.k8s.env").read_text().splitlines()
    rendered_gcs = dict(line.split("=", 1) for line in lines)
    assert rendered_gcs["DB_PASSWORD"] == "gcs-saved-pass"
    assert sum(1 for line in lines if line.startswith("DB_CONNECTION=")) == 1
    assert "gcs_env_uri=gs://test-state-bucket/secrets/boots.env" in github_output.read_text()

    # --- Case 2b: Priority 2 falls back to GKE secret when GCS object does not exist ---
    import base64

    gcloud_stub.write_text("#!/usr/bin/env bash\nexit 1\n")
    b64 = lambda s: base64.b64encode(s.encode()).decode()
    k8s_secret_json = json.dumps(
        {
            "data": {
                "DB_NAME": b64("ogrre-boots"),
                "DB_CONNECTION": b64("cluster0.example"),
                "DB_USERNAME": b64("ogrre-boots"),
                "DB_PASSWORD": b64("k8s-saved-pass"),
            }
        }
    )
    kubectl_stub = bin_dir / "kubectl"
    kubectl_stub.write_text(
        f"#!/usr/bin/env bash\n"
        f"cat <<'EOF'\n{k8s_secret_json}\nEOF\n"
    )
    kubectl_stub.chmod(0o755)

    res_k8s = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", runtime_script],
        cwd=tmp_path,
        env=base_env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "Loaded existing runtime environment from Kubernetes secret" in res_k8s.stdout
    rendered_k8s = dict(
        line.split("=", 1)
        for line in (tmp_path / "deployment/secrets/runtime.k8s.env")
        .read_text()
        .splitlines()
    )
    assert rendered_k8s["DB_PASSWORD"] == "k8s-saved-pass"

    # --- Case 3: Priority 3 runs provision_mongodb.py on first-time deploy ---
    kubectl_stub.write_text("#!/usr/bin/env bash\nexit 1\n")
    (tmp_path / "deployment/ci").mkdir(parents=True)
    (tmp_path / "deployment/ci/provision_mongodb.py").write_text(
        "import argparse, os\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--collaborator')\n"
        "p.add_argument('--env-file')\n"
        "args = p.parse_args()\n"
        "with open(args.env_file, 'a') as f:\n"
        "    f.write(f'DB_NAME=ogrre-{args.collaborator}\\n')\n"
        "    f.write(f'DB_CONNECTION={os.environ[\"DB_CONNECTION\"]}\\n')\n"
        "    f.write(f'DB_USERNAME=ogrre-{args.collaborator}\\n')\n"
        "    f.write('DB_PASSWORD=provisioned-pass\\n')\n"
    )
    python_stub = bin_dir / "python3"
    python_stub.write_text(
        f"#!/usr/bin/env bash\n"
        f'if [ "$1" = "-c" ]; then exit 0; fi\n'
        f'exec "{shutil.which("python3")}" "$@"\n'
    )
    python_stub.chmod(0o755)

    first_deploy_env = {
        **base_env,
        "ATLAS_PROJECT_ID": "atlas-proj-123",
        "ATLAS_PUBLIC_KEY": "atlas-pub",
        "ATLAS_PRIVATE_KEY": "atlas-priv",
    }
    res_prov = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", runtime_script],
        cwd=tmp_path,
        env=first_deploy_env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "First-time deployment detected for 'boots'" in res_prov.stdout
    prov_lines = (tmp_path / "deployment/secrets/runtime.k8s.env").read_text().splitlines()
    rendered_prov = dict(line.split("=", 1) for line in prov_lines)
    assert rendered_prov["DB_NAME"] == "ogrre-boots"
    assert rendered_prov["DB_USERNAME"] == "ogrre-boots"
    assert rendered_prov["DB_PASSWORD"] == "provisioned-pass"
    assert sum(1 for line in prov_lines if line.startswith("DB_CONNECTION=")) == 1

    # --- Test Create deployment secrets step (both GCS upload success and graceful warning on failure) ---
    (tmp_path / "deployment/secrets/storage-service-key.json").write_text("{}")
    (tmp_path / "deployment/secrets/document-ai-service-key.json").write_text("{}")
    kubectl_stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    gcloud_stub.write_text("#!/usr/bin/env bash\nexit 1\n")  # Simulate GCS upload 403 failure

    res_create = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", create_secrets_script],
        cwd=tmp_path,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "NAMESPACE": "uow-boots",
            "GCS_ENV_URI": "gs://test-state-bucket/secrets/boots.env",
            "DOCKERHUB_USERNAME": "user",
            "DOCKERHUB_ACCESS_TOKEN": "tok",
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "::warning::Could not upload runtime environment snapshot" in res_create.stdout


def test_all_workflow_bash_scripts_and_contracts_are_valid():
    """Validate bash syntax (-n) and workflow_call contracts across all backend and frontend workflows."""
    backend_root = Path(__file__).resolve().parents[2]
    frontend_root = backend_root.parent / "orphaned-wells-ui"
    workflow_dirs = [backend_root / ".github/workflows"]
    if (frontend_root / ".github/workflows").is_dir():
        workflow_dirs.append(frontend_root / ".github/workflows")

    for wf_dir in workflow_dirs:
        for wf_file in sorted(wf_dir.glob("*.yml")):
            wf = yaml.safe_load(wf_file.read_text())
            for job_name, job in (wf.get("jobs") or {}).items():
                # If job calls a local reusable workflow, verify inputs and secrets exist in callee
                uses = job.get("uses")
                if uses and uses.startswith("./.github/workflows/"):
                    callee_path = wf_dir.parent.parent / uses[2:]
                    assert callee_path.is_file(), f"Missing reusable workflow {callee_path}"
                    callee = yaml.safe_load(callee_path.read_text())
                    on_block = callee.get("on") or callee.get(True) or {}
                    wf_call = on_block.get("workflow_call") or {}
                    allowed_inputs = set((wf_call.get("inputs") or {}).keys())
                    allowed_secrets = set((wf_call.get("secrets") or {}).keys())
                    for passed_input in (job.get("with") or {}).keys():
                        assert passed_input in allowed_inputs, (
                            f"{wf_file.name}:{job_name} passes unknown input '{passed_input}' to {callee_path.name}"
                        )
                    if isinstance(job.get("secrets"), dict):
                        for passed_secret in job["secrets"].keys():
                            assert passed_secret in allowed_secrets, (
                                f"{wf_file.name}:{job_name} passes unknown secret '{passed_secret}' to {callee_path.name}"
                            )

                # Check bash -n syntax on every run: block
                for idx, step in enumerate(job.get("steps") or []):
                    if "run" in step:
                        subprocess.run(
                            ["bash", "-n"],
                            input=step["run"],
                            text=True,
                            check=True,
                            capture_output=True,
                        )
