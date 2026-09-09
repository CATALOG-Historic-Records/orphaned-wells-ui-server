# orphaned-wells-ui-server
Backend server-side code for the orphaned wells UI

## Getting started (developer)

### Prerequisites

The following steps assume that:

1. `conda` is already installed and configured

### 1. Creating the Conda environment

Run the following command to create and activate a new Conda environment named `uow-server-env`:

```sh
conda env create --file environment.yml && conda activate uow-server-env
```

This will install the correct runtime versions of the backend (Python) and the backend dependencies.\
\
Alternatively, if you already have an environment that you would like to install the dependencies in, 
activate your environment and run the command:
```sh
pip install .
```

#### For Developers:

This section is for developers who plan to modify or contribute to the server's codebase. In the same environment
where you installed the package, run the following command:
```sh
pip install -r requirements-dev.txt
```

### 2. Add credential/environment files

Credentials are necessary for MongoDB, Google Cloud Storage uploads, and Google Document AI processing.

Create the backend environment file from the template:

```sh
cp ogrre/.env.example ogrre/.env
```

Fill in the required values described in `ogrre/.env.example`.

For Google-backed non-Docker local development, place these local service-account JSON keys in `ogrre/`, or use absolute paths in `.env`:

1. `storage-service-key.json`
    - Storage runtime service account.
    - Used only for Google Cloud Storage upload bucket reads, writes, deletes, and signed/download URL interactions.
    - `STORAGE_SERVICE_KEY` in `.env` must point to this file.
2. `document-ai-service-key.json`
    - Document AI runtime service account.
    - Used for online/batch Document AI processing and processor deployment/undeployment from the app.
    - `DOCUMENT_AI_SERVICE_KEY` in `.env` must point to this file.

Do not use the deployment/Terraform service-account key for local app runtime. That identity is for infrastructure changes and Kubernetes/App Engine deployment automation.

# Running the server

### Ensure that the `uow-server-env` Conda environment is active

```console
conda activate uow-server-env
```

### Start server on port 8001

```console
cd <orphaned-wells-ui-server-path>
uvicorn ogrre.main:app --reload --host 127.0.0.1 --port 8001
```

For Docker-based local development, place the same two runtime key files at `deployment/secrets/storage-service-key.json` and `deployment/secrets/document-ai-service-key.json`, then run the backend container:

```console
cd <orphaned-wells-ui-server-path>/deployment
docker compose --env-file ../ogrre/.env up web
```

The full Compose stack also includes nginx/certbot for deployed-hostname setups. If you run the full stack, `NGINX_ENV` in `ogrre/.env` must select an existing config directory under `deployment/nginx/`.
