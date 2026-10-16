# Terraform deployment for orphaned-wells-ui-server

This directory contains the Terraform configuration used to manage OGRRE backend infrastructure.

For credential-free PR checks and approval-gated applies from `main`, follow
[Terraform CI setup and rollout](../ci/README.md). When `ENABLE_TERRAFORM_CI=true`,
merge infrastructure changes to `main` and approve saved plans containing changes.
Verified no-change plans finish automatically without applying.
Updated backend workflows read deploy targets live from the shared workspace;
the secret-update commands below apply only to the disabled rollout fallback.

## What is included

- `variables.tf` defines core infrastructure variables and defaults (GKE cluster, region, network, storage location, etc.) without hardcoding specific environments or project IDs.
- `main.tf` orchestrates core providers and project services.
- `gke.tf` creates the shared GKE deployment infrastructure unless `enable_gke=false`, including per-environment API and batch-worker resource defaults exported to deployment automation.
- `kubernetes_rbac.tf` creates each backend namespace, its two workload ServiceAccounts, and the API Job-dispatch Role/RoleBinding.
- `storage.tf` creates one Cloud Storage upload bucket per unique GKE backend bucket name.
- `terraform.tfvars.example` documents the required project-level variables (`project_id`, `dns_managed_zone`, `backend_dns_domain`) and collaborator backend definitions (`gke_backends`).
- `backend.tf` configures the shared GCS backend for Terraform state.
- `scripts/bootstrap_terraform_state_bucket.sh` creates or updates the GCS state bucket.
- `scripts/sync_tfvars.sh` syncs the shared `terraform.tfvars` file between local developer environments, Google Cloud Storage, and GitHub Secrets.
- `scripts/import_existing_infrastructure.sh` imports existing managed resources into a workspace from the current Terraform configuration.

## Prerequisites

- Terraform installed at the version in `.terraform-version` (1.13.5)
- Google Cloud SDK installed
- `gke-gcloud-auth-plugin` installed (`gcloud components install gke-gcloud-auth-plugin`)
- `jq` installed
- GitHub CLI `gh` installed and authenticated when updating GitHub Actions secrets from the command line
- Access to the target GCP project for this deployment
- A Terraform identity authorized to create Kubernetes namespaces, Roles, RoleBindings, and ServiceAccounts in the shared GKE cluster
- A supported shell to run `bash` scripts

## Service Accounts

Manual Terraform operations use a human or dedicated platform identity. CI uses
separate plan and apply accounts through WIF. Do not run Terraform with the
backend storage or Document AI runtime keys.

Recommended identities:

| Service account | Used for | Credentials location |
| --- | --- | --- |
| Storage runtime, for example `ogrre-storage-runtime` | Backend Cloud Storage upload bucket reads, writes, deletes, and signed/download URL interactions | Local `ogrre/.env` as `STORAGE_SERVICE_KEY`; GitHub secret `STORAGE_SERVICE_KEY_JSON` |
| Document AI runtime, for example `ogrre-document-ai` | Backend online/batch Document AI processing and processor deployment/undeployment | Local `ogrre/.env` as `DOCUMENT_AI_SERVICE_KEY`; GitHub secret `DOCUMENT_AI_SERVICE_KEY_JSON` |
| Terraform platform, for example a privileged human operator or dedicated infrastructure account | Terraform cloud infrastructure plus Kubernetes namespaces and runtime RBAC | Local `GOOGLE_APPLICATION_CREDENTIALS` or user ADC |
| GitHub deployment, `ogrre-deployment-ci` | Workload-only Kubernetes deployments | GitHub secret `DEPLOYMENT_SERVICE_KEY_JSON` |
| Terraform CI plan, `github-terraform-plan` | Live planning and automatic no-change completion | WIF; repository variable `TF_PLAN_SERVICE_ACCOUNT` |
| Terraform CI apply, `github-terraform-ci` | Approved saved-plan apply | WIF and protected `terraform-apply` Environment; variable `TF_APPLY_SERVICE_ACCOUNT` |

See [CI identity permissions](../ci/README.md#identities-and-artifact-permissions)
for the narrower plan account and bootstrap configuration.

The Terraform platform identity needs enough project and Kubernetes access to manage the infrastructure in this directory:

- `roles/container.admin`
- `roles/compute.networkAdmin`
- `roles/dns.admin`
- `roles/storage.admin`
- `roles/serviceusage.serviceUsageAdmin` if Terraform manages project services

It also needs read/write access to the Terraform state bucket. `roles/storage.admin` at the project level covers this; a narrower setup can grant state-bucket access directly.

Keep Cloud Storage upload-bucket runtime access on the storage runtime service account and Document AI API/runtime access on the Document AI runtime service account. Those runtime identities are configured in the backend app environment, not in Terraform.

Terraform uses the Kubernetes provider as well as the Google provider. The
Terraform platform identity must be able to create Kubernetes Roles and
RoleBindings; `roles/container.admin` is sufficient. Do not give that broad
role to `ogrre-deployment-ci` just to deploy application updates. Its existing
`roles/container.developer` access is used by GitHub Actions for the
workload-only manifest after Terraform has provisioned the namespace
foundation.

## Google Cloud Login

From this directory, authenticate to Google Cloud and set the project that matches your Terraform configuration:

```bash
cd orphaned-wells-ui-server/deployment/terraform

gcloud auth login

gcloud config set project <YOUR_PROJECT_ID>

gcloud auth application-default login
```

If you use a Terraform platform service-account JSON key locally instead of user ADC, set it in your shell before running Terraform:

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/secure/path/ogrre-terraform-platform-service-key.json
```

The import script also unsets `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_AUTHORIZED_USER_CREDENTIALS`, and `CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE` to avoid conflicts with existing credentials.

## Terraform commands

Manual planning and applying remain supported alongside GitHub Actions. Both
paths operate on the same remote workspace and the same shared infrastructure;
using the staging workflow does not isolate Terraform changes to staging.

### Provider lockfile maintenance

CI and the manual commands below use `-lockfile=readonly`. Commit provider
checksums for every supported platform before using that mode. The lockfile
includes Linux x86-64 for GitHub Actions, Linux ARM64 for local containers, and
both Intel and Apple Silicon macOS.

After changing provider selections, regenerate the platform checksums using
the pinned Terraform CLI. From the backend repository root:

```bash
terraform -chdir=deployment/terraform get
terraform -chdir=deployment/terraform providers lock \
  -platform=linux_amd64 \
  -platform=linux_arm64 \
  -platform=darwin_amd64 \
  -platform=darwin_arm64
git diff -- deployment/terraform/.terraform.lock.hcl
```

These commands prepare modules and fetch provider checksums without accessing
remote state or applying infrastructure. Review the reported HashiCorp signatures
and lockfile diff, then commit the updated lockfile. The lock command retains
existing provider versions that satisfy the configuration constraints.

If initialization warns **Provider lock file not updated**, followed by
validation reporting **missing or corrupted provider plugins**, check for
missing platform checksums. Signed archive (`zh:`) checksums can verify a
download, but validation needs a matching extracted-package (`h1:`) checksum.
Read-only initialization cannot save that additional checksum. Regenerate the
lockfile as above instead of disabling checksum verification in CI. See
[HashiCorp's platform-locking documentation](https://developer.hashicorp.com/terraform/cli/commands/providers/lock#specifying-target-platforms).

### Manual plan

Use Terraform at the version in `.terraform-version` and install the Kubernetes
authentication plugin. For human ADC authentication, start with:

```bash
cd orphaned-wells-ui-server/deployment/terraform
unset GOOGLE_APPLICATION_CREDENTIALS GOOGLE_AUTHORIZED_USER_CREDENTIALS CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE
gcloud auth login
gcloud config set project tidy-outlet-412020
gcloud auth application-default login
gcloud components install gke-gcloud-auth-plugin

terraform version
unset TF_WORKSPACE
terraform init -input=false -lockfile=readonly
terraform workspace select ogrre
terraform workspace show
terraform fmt -check -recursive
terraform validate
terraform plan -input=false -lock-timeout=5m
```

Confirm the CLI matches `.terraform-version` and the workspace is `ogrre`
before proceeding. A speculative plan does not apply changes. It can run while
CI is enabled, although it may wait for the Terraform state lock. Never disable
state locking to work around a concurrent operation.

Local `terraform.tfvars` files are loaded automatically. In CI,
`terraform-apply.yml` hydrates `deployment/terraform/terraform.tfvars` directly
from the `PROD_TERRAFORM_TFVARS` GitHub Secret, ensuring production infrastructure
inventory is managed securely without being checked into Git.

### Sharing and Syncing `terraform.tfvars` via GCS

Because `terraform.tfvars` contains production inventory and is excluded from Git (`.gitignore`), the shared source of truth is stored in Google Cloud Storage:

```text
gs://<TF_STATE_BUCKET>/config/terraform.tfvars
```

Use `scripts/sync_tfvars.sh` to manage your local copy:

- **Download the latest shared configuration:**
  ```bash
  ./deployment/terraform/scripts/sync_tfvars.sh pull
  ```
  *(Creates a `.bak` backup of your local `terraform.tfvars` if one already exists)*

- **Inspect local modifications vs. GCS:**
  ```bash
  ./deployment/terraform/scripts/sync_tfvars.sh diff
  ```

- **Upload local changes to GCS:**
  ```bash
  ./deployment/terraform/scripts/sync_tfvars.sh push
  ```

- **Upload to GCS and update the CI secret (`PROD_TERRAFORM_TFVARS`) at once:**
  ```bash
  ./deployment/terraform/scripts/sync_tfvars.sh push --secret
  ```

- **Sync the GCS configuration directly into the GitHub Secret (`PROD_TERRAFORM_TFVARS`):**
  ```bash
  ./deployment/terraform/scripts/sync_tfvars.sh sync-secret
  ```
  *(Or stream directly from GCS into `gh` via pipe:)*
  ```bash
  gcloud storage cat gs://tidy-outlet-412020-ogrre-terraform-state/config/terraform.tfvars | \
    gh secret set PROD_TERRAFORM_TFVARS --repo CATALOG-Historic-Records/orphaned-wells-ui-server
  ```

*(Manual fallback: `gcloud storage cp gs://tidy-outlet-412020-ogrre-terraform-state/config/terraform.tfvars deployment/terraform/terraform.tfvars`)*

### Manual apply

Use the reviewed infrastructure configuration from current `main`. Coordinate
the operation with other operators. If Terraform CI is enabled, follow
[pause and resume for manual operations](../ci/README.md#manual-plan-and-apply)
before changing the shared infrastructure. Keep the CI flag enabled while
pausing workflows so deploys do not switch back to stale secret-based targets.

Create a saved plan outside the repository, inspect it, and apply that exact
plan as a separate deliberate step:

```bash
OGRRE_PLAN_DIR="$(mktemp -d)"
chmod 700 "$OGRRE_PLAN_DIR"
terraform plan -input=false -lock-timeout=5m -out="$OGRRE_PLAN_DIR/manual.tfplan"
terraform show -no-color "$OGRRE_PLAN_DIR/manual.tfplan"
```

After reviewing all proposed changes:

```bash
terraform apply -input=false -lock-timeout=5m "$OGRRE_PLAN_DIR/manual.tfplan"
terraform output -json kubernetes_deploy_targets | jq .
rm -f "$OGRRE_PLAN_DIR/manual.tfplan"
rmdir "$OGRRE_PLAN_DIR"
```

Passing a saved plan to `terraform apply` executes it without another approval
prompt. Keep the plan private; it can contain sensitive values. If the state or
desired configuration changes before apply, generate and review a new plan.

When CI is enabled, a manual state write invalidates the recorded successful
apply. Resume the staging workflow first and dispatch a fresh run with
`force_terraform_plan=true`. Review the reconciliation plan. If local inputs match
`main`, no-change verification restores readiness automatically; otherwise review
and approve the proposed changes. After reconciliation succeeds, resume the remaining
deployment workflows. Never fabricate or manually edit the CI readiness record.

When CI is disabled, refresh the fallback `K8S_DEPLOY_TARGETS` secret using the
commands below before deploying. Keep `DEPLOYMENT_SERVICE_KEY_JSON` in either
mode for phase one.

## Existing GKE namespaces

Terraform now owns OGRRE namespaces and runtime Kubernetes RBAC. Before the
first apply of this change, import every namespace that already exists in the
cluster; otherwise Terraform will correctly attempt to create it and the
Kubernetes API will reject the duplicate. Run an import only for namespaces
that `kubectl get namespace` confirms already exist:

```bash
terraform import 'kubernetes_namespace_v1.backend["staging"]' uow-staging
terraform import 'kubernetes_namespace_v1.backend["isgs"]' uow-isgs
terraform import 'kubernetes_namespace_v1.backend["newts"]' uow-newts
terraform import 'kubernetes_namespace_v1.backend["osage"]' uow-osage
terraform import 'kubernetes_namespace_v1.backend["rrc"]' uow-rrc
```

After import, review the plan. It should adopt the namespaces and create the
two ServiceAccounts plus `processing-job-dispatcher` Role and RoleBinding for
each backend. Do not run the former GitHub Actions RBAC bootstrap; Terraform
replaces it. Namespace resources use `prevent_destroy=true`, so removing a
backend definition cannot silently remove a namespace and its workloads.

For a new collaborator, add the backend to `gke_backends` in `terraform.tfvars`
(and update the `PROD_TERRAFORM_TFVARS` GitHub Secret for CI) and run the normal
Terraform apply. Terraform creates the namespace and worker authorization as part
of that same change, so no per-collaborator RBAC bootstrap is required.

Set `enable_kubernetes_workloads = false` when cloud resources must remain
managed but the collaborator is not yet ready for a GKE backend. Such an entry
does not receive a Kubernetes namespace, runtime ServiceAccounts/RBAC, or a
`kubernetes_deploy_targets` entry.

## GKE deployment infrastructure

The GKE path is enabled by default (`enable_gke=true`). All OGRRE backend
environments run on GKE; legacy Compute Engine VMs have been completely retired.

Create or update the GKE cluster, global load balancer IPs, upload buckets, primary DNS records, and optional `<env>-k8s-server.uow-carbon.org` test DNS records:

```bash
terraform plan
terraform apply
```

Export the GitHub Actions target map:

```bash
terraform output -json kubernetes_deploy_targets | jq -c .
```

Only for the disabled CI rollout fallback, store that JSON as `K8S_DEPLOY_TARGETS`:

```bash
gh auth login
gh secret set K8S_DEPLOY_TARGETS \
  --repo CATALOG-Historic-Records/orphaned-wells-ui-server \
  --body "$(terraform output -json kubernetes_deploy_targets | jq -c .)"
```

When Terraform deployment behavior changes, update the operator-facing frontend docs in `../orphaned-wells-ui/docs/docs/deploy-gcp` as part of the same work so the two repos stay aligned.

See `../kubernetes/README.md` for Kubernetes deployment and operations commands.

### Batch-worker resource configuration

Terraform does not create a permanent worker deployment or additional Google
Cloud resources for document processing. It exports per-environment values in
`kubernetes_deploy_targets`; GitHub Actions writes those values into the
runtime secret used by the API to create short-lived Kubernetes Jobs.

Each backend may set these optional `gke_backends` or
`gke_backend_overrides` fields:

- `api_uvicorn_workers` controls the API container's Uvicorn worker count.
- `processing_job_cpu_request`, `processing_job_memory_request`,
  `processing_job_cpu_limit`, and `processing_job_memory_limit` size each
  batch-processing Job independently from the always-running API pods.
- `processing_job_ephemeral_storage` reserves worker scratch space for source
  downloads and image conversion.
- `processing_job_active_deadline_seconds` is the maximum Job run time;
  `processing_job_ttl_seconds_after_finished` retains completed Job metadata
  and Pod logs for troubleshooting.
- `processing_job_max_active` limits simultaneous batch Jobs per environment.
- `enable_kubernetes_workloads` controls whether Terraform also creates the
  namespace, runtime RBAC, and GitHub Actions deploy target. It defaults to
  `true`; CA intentionally sets it to `false` until it is ready for GKE.

The default collaborator worker is 1850m CPU and 12Gi memory. Staging workers
retain 1 CPU and 6Gi while the staging API requests 500m CPU and 1Gi memory,
with limits of 1 CPU and 2Gi, across one replica with two Uvicorn workers.
Production API defaults target two replicas, each with 1 CPU and 4Gi.
Production workers retain their larger allocation independently.
After approving Terraform changes to these outputs, deploy the backend; no
resource setting takes effect from Terraform alone. Refresh `K8S_DEPLOY_TARGETS`
only when the CI rollout flag is disabled.

### API resource reduction

API requests and limits use these targets; production replica counts and all
worker settings remain unchanged:

| Environment | API replicas | API request per pod | API limit per pod | Worker CPU / memory |
| --- | --- | --- | --- | --- |
| Staging | 1 | 500m CPU / 1Gi | 1 CPU / 2Gi | 1 CPU / 6Gi |
| Production collaborators | 2 | 1 CPU / 4Gi | 1 CPU / 4Gi | 1850m CPU / 12Gi |

For an otherwise up-to-date workspace, `terraform plan` should show only
changes under `kubernetes_deploy_targets`: staging `cpu_request` changes from
`1` to `500m`, staging `memory_request` changes from `4Gi` to `1Gi`, and
staging `memory_limit` changes from `4Gi` to `2Gi`. Staging `cpu_limit` stays
at `1`, production targets stay unchanged, and worker settings stay unchanged.

There should be no infrastructure resources to add, change, or destroy from
this sizing change. Review any other changes separately. Apply the output
update, refresh the GitHub secret using the export instructions above, and
deploy the affected environments with the updated workflow. Explicit values
in the secret take precedence over workflow defaults, so older secrets retain
the previous pod sizes until refreshed. Applying and exporting these values
does not resize live pods; each backend deployment activates its new target.

Follow the [staging checks and rollback instructions](../kubernetes/README.md#staging-checks-before-reducing-api-resources)
before considering production changes. Validate production with real traffic
before considering any further API reduction. Use
`gke_backend_overrides` for environment-specific adjustments or rollback.

### Primary DNS state migration

Primary backend DNS records now live at root-level GKE resource addresses:

```text
google_dns_record_set.gke_backend_primary["<collaborator>"]
```

The existing records were previously managed inside the legacy VM module:

```text
module.backend_vms["<collaborator>"].google_dns_record_set.dns
```

`moved.tf` contains Terraform `moved` blocks for the existing collaborators. On the first plan after this change, Terraform should report those DNS record resources as moved, not created or destroyed. Review the plan carefully and do not apply a plan that proposes a second record set for an existing `<collaborator>-server.uow-carbon.org` A record.

To explicitly exclude GKE resources from a plan:

```bash
terraform plan -var='enable_gke=false'
```

If you need to destroy infrastructure, run:

```bash
terraform destroy
```

> Note: this repository uses a shared GCS backend for state. Do not manually edit, delete, or overwrite remote state unless you are intentionally performing a state migration or recovery.

## Upload Buckets

Each GKE backend gets a Cloud Storage upload bucket. The default bucket name is `<collaborator>_uploads`; use `upload_bucket_name` for existing exceptions or explicit custom names.

The shared defaults preserve the current bucket names:

```text
staging -> uploaded_documents_v0
isgs    -> isgs_uploads
newts   -> newts_uploads
osage   -> osage_uploads
rrc     -> rrc_uploads
```

Bucket resources are keyed by bucket name, so collaborators can intentionally share a bucket without Terraform creating duplicate resources. Buckets use `force_destroy=false` and `prevent_destroy=true` to protect uploaded files.

To customize a new collaborator:

```hcl
gke_backend_overrides = {
  boots = {
    upload_bucket_name = "boots_uploads"
  }
}
```

Cloud Storage bucket location is immutable. New buckets default to `upload_bucket_location`, currently `US`; imported buckets keep their existing location, storage class, and encryption settings.

## Remote state setup

Terraform state is stored in this GCS bucket:

```text
gs://tidy-outlet-412020-ogrre-terraform-state/orphaned-wells-ui-server
```

The bucket must exist before `terraform init` can use the backend. Create or update it with:

```bash
cd orphaned-wells-ui-server/deployment/terraform
bash scripts/bootstrap_terraform_state_bucket.sh
```

The bootstrap script creates the bucket in `us-central1`, enables uniform bucket-level access, enforces public access prevention, and enables object versioning.

### Migrating the known-good local state

Use this once to promote the known-good local `staging-test` state into the shared GCS backend. The example below pushes that state into the remote `ogrre` workspace. Set `REMOTE_WORKSPACE=staging-test` instead if you want to preserve the old workspace name.

```bash
cd orphaned-wells-ui-server/deployment/terraform

BACKUP_DIR="/private/tmp/ogrre-tfstate-migration-$(date +%Y%m%d%H%M%S)"
mkdir -p "$BACKUP_DIR"

cp terraform.tfstate.d/staging-test/terraform.tfstate "$BACKUP_DIR/staging-test.tfstate"
cp -R terraform.tfstate terraform.tfstate.backup terraform.tfstate.d "$BACKUP_DIR"/

bash scripts/bootstrap_terraform_state_bucket.sh

terraform init -reconfigure

REMOTE_WORKSPACE=ogrre
terraform workspace new "$REMOTE_WORKSPACE" || terraform workspace select "$REMOTE_WORKSPACE"
terraform state push "$BACKUP_DIR/staging-test.tfstate"

terraform plan
```

After the migration succeeds, other developers should run:

```bash
cd orphaned-wells-ui-server/deployment/terraform
terraform init
terraform workspace select ogrre
terraform plan
```

If `terraform state push` reports that remote state already exists, stop and inspect the remote workspace before using `-force`.

## Workspaces

Terraform workspaces allow you to manage multiple state files for the same configuration. This is useful for separating environments (e.g., staging, production) or testing changes.

List all workspaces:

```bash
terraform workspace list
```

Create a new workspace:

```bash
terraform workspace new <workspace_name>
```

Switch to a workspace:

```bash
terraform workspace select <workspace_name>
```

Show the currently active workspace:

```bash
terraform workspace show
```

Each workspace maintains its own state file and can have different variable values. When you switch workspaces, Terraform loads the associated state.

## Targeting specific modules or resources

To plan or apply changes to only a specific resource, use the `-target` flag. This is useful when testing changes to one collaborator's infrastructure without affecting others.

Plan changes for a specific collaborator resource:

```bash
terraform plan -target='google_storage_bucket.backend_uploads["staging"]'
```

Apply changes for a specific collaborator resource:

```bash
terraform apply -target='google_storage_bucket.backend_uploads["staging"]'
```

You can also target other collaborator resources like DNS record sets:

```bash
terraform plan -target='google_dns_record_set.gke_backend_primary["staging"]'
```

> Caution: using `-target` modifies state tracking and should only be used for specific, isolated changes. Always review the plan output carefully before applying.

## Import existing infrastructure into Terraform state

Use the comprehensive importer when creating a new workspace that should track existing Google Cloud infrastructure represented by this Terraform configuration. The script parses `variables.tf` plus optional `terraform.tfvars` overrides, generates the expected resource addresses and Google import IDs from this repo's naming conventions, and imports them into the target workspace. It does not use `terraform console`, so manifest generation does not depend on decoding existing Terraform state.

Preview the import into `ogrre`:

```bash
bash scripts/import_existing_infrastructure.sh --target-workspace ogrre --dry-run
```

Run the import into `ogrre`:

```bash
bash scripts/import_existing_infrastructure.sh --target-workspace ogrre
```

If the target workspace has disposable, stale, or provider-incompatible local state, move its state file aside before importing:

```bash
bash scripts/import_existing_infrastructure.sh --target-workspace ogrre --reset-state
```

`--reset-state` only moves the selected local Terraform workspace state file to a timestamped backup. It does not change Google Cloud resources.

The importer covers resources currently represented by the Terraform files, including:

- enabled legacy backend VM module resources: Compute Engine instances, regional static IPs, and VM-owned primary DNS records
- shared firewall rules
- GKE-required project services
- the shared GKE cluster
- GKE backend upload buckets
- GKE global static IPs
- GKE primary DNS records
- GKE test DNS records

Resources already present in the target workspace are skipped. Missing, not-yet-created, or otherwise non-importable resources are reported in the summary and do not stop the script unless `--strict` is passed. This is expected when the configuration includes a new collaborator whose DNS record or GKE IP does not exist yet.

To import only enabled VM module resources, pass `--backend-vms-only`. You can also limit VM imports to specific collaborators:

```bash
bash scripts/import_existing_infrastructure.sh --target-workspace ogrre --backend-vms-only staging
```

## Adding a new GKE collaborator

## Adding a new collaborator

Adding a collaborator involves three coordinated steps across infrastructure, deployment configuration, and secrets:

### 1. Configure Infrastructure (`terraform.tfvars`)

First, ensure your local configuration is up to date:
```bash
./deployment/terraform/scripts/sync_tfvars.sh pull
```

Collaborator backend definitions live in `gke_backends` inside `terraform.tfvars` (see `terraform.tfvars.example` for reference).

Add the collaborator backend configuration:

```hcl
gke_backends = {
  # existing backends...
  boots = {
    # Optional overrides, e.g. custom upload bucket:
    # upload_bucket_name = "boots_uploads"
  }
}
```

This provisions:
- Cloud Storage upload bucket (`boots_uploads`)
- Kubernetes namespace (`uow-boots`) and service accounts/RBAC
- GKE static IP address (`boots-uow-gke-ip`)
- Primary DNS record (`boots-server.uow-carbon.org`)
- Test DNS record (`boots-k8s-server.uow-carbon.org`)
- `kubernetes_deploy_targets.boots` output

For local testing:
```bash
terraform plan
terraform apply
```

For production CI and sharing with teammates:
Upload the updated configuration to GCS and update the GitHub secret in one step:
```bash
./deployment/terraform/scripts/sync_tfvars.sh push --secret
```
When merged to `main`, CI will automatically hydrate the secret, plan, and prompt for apply approval.

### 2. Register Branch (`collaborators.json`)

Add the collaborator branch name to `collaborators.json` in the backend repository root:

```json
[
  "osage",
  "newts",
  "isgs",
  "rrc",
  "boots"
]
```

This single file drives:
- `.github/workflows/deploy-k8s-collaborator.yml`: Automatically deploys pushes to any collaborator branch listed in `collaborators.json`.
- `.github/workflows/sync-collaborators.yml`: Automatically opens synchronization PRs from `main` to each branch in `collaborators.json`.

### 3. Branch & Runtime Secrets

1. Create and push the collaborator branch (e.g. `boots`).
2. Add the runtime secret in GitHub:
   - For standard multi-environment setups: create the environment first (`gh api --method PUT repos/CATALOG-Historic-Records/orphaned-wells-ui-server/environments/boots`), then configure `RUNTIME_ENV` inside it (`gh secret set RUNTIME_ENV -R CATALOG-Historic-Records/orphaned-wells-ui-server --env boots < boots.env`).
   - For standalone or single-collaborator forks: `RUNTIME_ENV` can be set as a repository secret and will be picked up directly via fallback.
3. Pushes to the branch will trigger `deploy-k8s-collaborator.yml` and deploy the backend to GKE.

## Notes

- GKE primary DNS records are managed by `google_dns_record_set.gke_backend_primary`.
- Each managed resource uses `prevent_destroy = true` to protect production infrastructure from accidental deletion.
- Project-specific configuration lives in `terraform.tfvars`, while generic variable declarations and defaults live in `variables.tf`.


## Directory upload bucket settings

`backend_uploads` manages CORS for browser-to-GCS directory transfers and a
14-day lifecycle rule for `directory_uploads/` and `directory_upload_outputs/`.
Origins default to the frontend custom domains for each bucket's backends.
The staging bucket also allows `http://localhost:3000` (native and Docker
development) and `http://localhost:3001` (the isolated Docker E2E stack).
Set `upload_bucket_cors_origins` to replace a bucket's complete origin list for
other hosts or ports; retain the frontend domain and local origins still used.
Use the origin in the browser address bar, including the published host port.
`http://127.0.0.1:3000` is a different origin and requires an explicit entry.
The backend serving that frontend must also permit its origin in
`ALLOWED_ORIGINS`; bucket configuration does not update backend runtime settings.

GCS bucket CORS governs XML API requests. The JSON API resumable sessions used
for directory uploads supply the browser origin at session creation and have
their own CORS handling. See [Google's endpoint behavior documentation](https://docs.cloud.google.com/storage/docs/cross-origin#cloud-storage-cors-support).
Local development with `STORAGE_BACKEND=local` does not access GCS.

Review existing CORS and lifecycle settings in the Terraform plan before
applying these changes. The deletion rule covers only the two temporary
prefixes; it does not cover `uploads/`, `deleted/`, or caller-owned batch inputs.
Apply storage configuration before deploying the paired upload frontend/backend.
API sizing and validation are documented in
[the Kubernetes README](../kubernetes/README.md#staging-checks-before-reducing-api-resources).
