# Terraform deployment for orphaned-wells-ui-server

This directory contains the Terraform configuration used to manage OGRRE backend infrastructure.

## What is included

- `variables.tf` defines the shared defaults, including the default GKE backends and legacy VM inventory.
- `main.tf` creates legacy backend VM modules only for entries in `legacy_backend_vms`.
- `gke.tf` creates the shared GKE deployment infrastructure unless `enable_gke=false`.
- `modules/backend_vm` contains the reusable legacy VM module, including a compute instance, static IP, and optional VM-owned DNS record.
- `terraform.tfvars.example` shows optional local override patterns.
- `backend.tf` configures the shared GCS backend for Terraform state.
- `scripts/bootstrap_terraform_state_bucket.sh` creates or updates the GCS state bucket.
- `scripts/import_existing_infrastructure.sh` imports existing managed resources into a workspace from the current Terraform configuration.

## Prerequisites

- Terraform installed (compatible with Terraform 1.x)
- Google Cloud SDK installed
- Access to the target GCP project for this deployment
- A supported shell to run `bash` scripts

## Google Cloud login

From this directory, authenticate to Google Cloud and set the project that matches your Terraform configuration:

```bash
cd orphaned-wells-ui-server/deployment/terraform

gcloud auth login

gcloud config set project <YOUR_PROJECT_ID>

gcloud auth application-default login
```

The import script also unsets `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_AUTHORIZED_USER_CREDENTIALS`, and `CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE` to avoid conflicts with existing credentials.

## Terraform commands

Initialize the working directory, backend, and providers:

```bash
terraform init
```

Create an execution plan with the configured variables:

```bash
terraform plan
```

Apply the planned changes:

```bash
terraform apply
```

## GKE deployment infrastructure

The GKE path is enabled by default. Existing VM resources are still managed by Terraform through `legacy_backend_vms`, but GKE-owned backends no longer need a VM entry. Existing VMs can remain stopped in GCP while GKE serves traffic.

Create or update the GKE cluster, global load balancer IPs, primary DNS records, and optional `<env>-k8s-server.uow-carbon.org` test DNS records:

```bash
terraform plan
terraform apply
```

Export the GitHub Actions target map:

```bash
terraform output -json kubernetes_deploy_targets | jq -c .
```

Store that JSON as the GitHub secret `K8S_DEPLOY_TARGETS`. See `../kubernetes/README.md` for Kubernetes deployment and operations commands.

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

To plan or apply changes to only a specific module, use the `-target` flag. This is useful when testing changes to one collaborator's infrastructure without affecting others.

Plan changes for a specific collaborator module:

```bash
terraform plan -target="module.backend_vms[\"staging\"]"
```

Apply changes for a specific collaborator module:

```bash
terraform apply -target="module.backend_vms[\"staging\"]"
```

You can also target individual resources within a module:

```bash
terraform plan -target="module.backend_vms[\"staging\"].google_compute_instance.vm"
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

- legacy backend VM module resources: Compute Engine instances, regional static IPs, and VM-owned primary DNS records
- shared firewall rules
- GKE-required project services
- the shared GKE cluster
- GKE global static IPs
- GKE primary DNS records
- GKE test DNS records

Resources already present in the target workspace are skipped. Missing, not-yet-created, or otherwise non-importable resources are reported in the summary and do not stop the script unless `--strict` is passed. This is expected when the configuration includes a new collaborator whose DNS record or GKE IP does not exist yet.

To keep the old VM-only behavior, pass `--backend-vms-only`. You can also limit VM imports to specific collaborators:

```bash
bash scripts/import_existing_infrastructure.sh --target-workspace ogrre --backend-vms-only staging
```

## Adding a new GKE collaborator

Defaults for the shared project are in `variables.tf`, so a local `terraform.tfvars` file is optional. To add a GKE-only collaborator locally without editing the default map, add a small override:

```hcl
gke_backend_overrides = {
  boots = {}
}
```

That creates:

- `boots-uow-gke-ip`
- `boots-server.uow-carbon.org`
- `boots-k8s-server.uow-carbon.org`, unless disabled
- a `kubernetes_deploy_targets.boots` output entry

If the collaborator needs non-default GKE settings, set them in the same map:

```hcl
gke_backend_overrides = {
  boots = {
    replicas             = 1
    memory_request       = "8Gi"
    memory_limit         = "8Gi"
    persistent_disk_size = "20Gi"
  }
}
```

Then run:

```bash
terraform plan
terraform apply
terraform output -json kubernetes_deploy_targets | jq -c .
```

Store the updated output as the backend repository secret `K8S_DEPLOY_TARGETS`.

## Adding a legacy VM

Only add a collaborator to `legacy_backend_vms` when you intentionally need Terraform to manage a Compute Engine VM.

Example legacy VM block:

```hcl
legacy_backend_vms = {
  boots = {
    enable_startup_script  = false
    zone                   = "us-central1-a"
    machine_type           = "e2-standard-2"
    boot_image             = "https://www.googleapis.com/compute/v1/projects/debian-cloud/global/images/debian-12-bookworm-v20260513"
    boot_disk_size         = 20
    boot_resource_policies = []
    boot_disk_device_name  = "boots-uow-server"
  }
}
```

If the same collaborator is also a GKE backend with `create_primary_dns_record=true`, the VM module will not create a duplicate primary DNS record.

After updating Terraform, run:

```bash
terraform plan
```

If the new VM already exists in the GCP project, run `scripts/import_existing_infrastructure.sh` to import the resources into state. The import script reads the backend maps from Terraform, so it does not need a per-collaborator script update.

## Notes

- `main.tf` uses a `for_each` loop to instantiate the `backend_vm` module for each `legacy_backend_vms` entry.
- The VM module creates a compute instance, reserved static IP address, and an optional DNS record in `uow-carbon-org`.
- GKE primary DNS records are managed by `google_dns_record_set.gke_backend_primary`.
- Each managed resource uses `prevent_destroy = true` to protect production infrastructure from accidental deletion.
- Use `terraform.tfvars` only for local overrides; shared non-secret defaults live in `variables.tf`.

If you need further detail on a specific collaborator or import workflow, I can expand this README with step-by-step examples.
