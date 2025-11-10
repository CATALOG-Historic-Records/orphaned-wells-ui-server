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

Credentials are necessary for backend functionality. This includes Google Cloud's document AI features, MongoDB database access, and Google Cloud Storage for storing documents/images. To access these functionalities, you must create the following credential files and place them in the **< orphaned-wells-ui-server-path >/ogrre/internal/** directory:
1. **.env** 
    - Must contain **PROJECT_ID**, **LOCATION**, **PROCESSOR_ID**, **DB_USERNAME**, **DB_PASSWORD**, **STORAGE_SERVICE_KEY** (3rd item on this list)
2. **creds.json**
    - Must contain GCP Storage Bucket credentials. To generate this file using gcloud api, see https://cloud.google.com/document-ai/docs/libraries#authentication.
3. **STORAGE_SERVICE_KEY** file
    - Name doesn't matter as long as it matches what you store in .env
    - Must contain google cloud client credentials. See https://docs.gspread.org/en/latest/oauth2.html#for-bots-using-service-account.

# Running the server

### Ensure that the `uow-server-env` Conda environment is active

```console
conda activate uow-server-env
```

### Start server on port 8001

```console
cd <orphaned-wells-ui-server-path>/app && uvicorn main:app --reload --host 127.0.0.1 --port 8001
```