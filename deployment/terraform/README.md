# Terraform deployment for orphaned-wells-ui-server

This directory contains the Terraform configuration used to manage backend VM infrastructure for OGRRE collaborators.

## What is included

- `main.tf` defines a `local.collaborators` map and creates a backend VM module for each entry.
- `gke.tf` creates the shared GKE deployment infrastructure unless `enable_gke=false`.
- `modules/backend_vm` contains the reusable VM module, including a compute instance, static IP, and DNS record.
- `variables.tf` declares the Terraform input variables.
- `terraform.tfvars` provides the default Google Cloud project and region values.
- `scripts/import_backend_vms.sh` imports existing GCP instances, static IPs, and DNS records into Terraform state.

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

Initialize the working directory and download providers:

```bash
terraform init
```

Create an execution plan with the configured variables:

```bash
terraform plan -var-file=terraform.tfvars
```

Apply the planned changes:

```bash
terraform apply -var-file=terraform.tfvars
```

## GKE deployment infrastructure

The GKE path is enabled by default. The existing VM resources are still managed by Terraform and can remain stopped in GCP while GKE serves traffic.

Create or update the GKE cluster, global load balancer IPs, and `<env>-k8s-server.uow-carbon.org` test DNS records:

```bash
terraform plan -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars
```

Export the GitHub Actions target map:

```bash
terraform output -json kubernetes_deploy_targets | jq -c .
```

Store that JSON as the GitHub secret `K8S_DEPLOY_TARGETS`. See `../kubernetes/README.md` for Kubernetes deployment and operations commands.

To explicitly exclude GKE resources from a plan:

```bash
terraform plan -var-file=terraform.tfvars -var='enable_gke=false'
```

If you need to destroy infrastructure, run:

```bash
terraform destroy -var-file=terraform.tfvars
```

> Note: this repository currently uses local state by default (`terraform.tfstate`). Do not delete or lose this file unless you intend to recreate the infrastructure.

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
terraform plan -target="module.backend_vms[\"staging\"]" -var-file=terraform.tfvars
```

Apply changes for a specific collaborator module:

```bash
terraform apply -target="module.backend_vms[\"staging\"]" -var-file=terraform.tfvars
```

You can also target individual resources within a module:

```bash
terraform plan -target="module.backend_vms[\"staging\"].google_compute_instance.vm" -var-file=terraform.tfvars
```

> Caution: using `-target` modifies state tracking and should only be used for specific, isolated changes. Always review the plan output carefully before applying.

## Import existing infrastructure into Terraform state

The import script is designed to import existing backend VM resources for collaborators that already exist in Google Cloud.

```bash
bash scripts/import_backend_vms.sh
```

This script imports:

- the Compute Engine instance
- the reserved static IP address
- the DNS record in the `uow-carbon-org` managed zone

### Updating the import script for a new collaborator

If you add a collaborator, also update `scripts/import_backend_vms.sh`:

- add the collaborator key to the `COLLABORATORS` array
- add the collaborator zone mapping to the `ZONES` associative array

For example:

```bash
declare -A ZONES=(
  [isgs]="us-central1-a"
  [osage]="us-central1-f"
  [ca]="us-central1-f"
  [newts]="us-central1-b"
  [staging]="us-central1-a"
  [boots]="us-central1-a"
)

COLLABORATORS=("isgs" "osage" "ca" "newts" "staging" "boots")
```

Then rerun the import script.

## Adding a new collaborator

To add a new collaborator VM, update the `local.collaborators` map in `main.tf`.

Example collaborator block:

```hcl
locals {
  collaborators = {
    boots = {
      enable_startup_script  = false
      zone                   = "us-central1-a"
      machine_type           = "e2-standard-2"
      boot_image             = "https://www.googleapis.com/compute/v1/projects/debian-cloud/global/images/debian-12-bookworm-v20260513"
      boot_disk_size         = 20
      boot_resource_policies = []
      boot_disk_device_name  = "boots-uow-server"
    }

    # existing collaborators...
  }
}
```

After updating `main.tf`, run:

```bash
terraform plan -var-file=terraform.tfvars
```

If the new VM already exists in the GCP project, also add it to `scripts/import_backend_vms.sh` and run the script to import the resources into state.

## Notes

- `main.tf` uses a `for_each` loop to instantiate the `backend_vm` module for each collaborator.
- The module creates a compute instance, reserved static IP address, and DNS record in `uow-carbon-org`.
- Each managed resource uses `prevent_destroy = true` to protect production infrastructure from accidental deletion.
- Keep `terraform.tfvars` updated if the project or region changes.

If you need further detail on a specific collaborator or import workflow, I can expand this README with step-by-step examples.
