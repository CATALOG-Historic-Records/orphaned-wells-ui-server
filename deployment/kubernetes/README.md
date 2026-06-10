# GKE deployment for orphaned-wells-ui-server

This directory contains the Kubernetes deployment template used by the GKE GitHub Actions workflows. The existing VM deployment remains intact; this path is opt-in so staging can be tested first.

## What Terraform creates

When `enable_gke=true`, `deployment/terraform` creates:

- one shared GKE Autopilot cluster
- one global static IP per backend environment
- one test DNS record per backend: `<env>-k8s-server.uow-carbon.org`
- Terraform outputs for GitHub Actions in `kubernetes_deploy_targets`

The existing VM module still owns the current primary DNS records, such as `staging-server.uow-carbon.org`. During cutover, `primary_dns_to_gke_backends` can repoint those existing records to the GKE load balancer IP.

## What GitHub Actions does

The staging workflow `.github/workflows/deploy-k8s-staging.yml`:

- builds the Docker image from `main`
- pushes `michaelpescelbl/orphaned-wells-ui-server:latest`

The reusable workflow `.github/workflows/deploy-k8s-dispatch.yml`:

- deploys an existing Docker image tag, defaulting to `latest`
- creates or updates the Kubernetes namespace
- creates a Docker Hub pull secret
- creates `backend-runtime-env` from the environment secret and `backend-runtime-files` from `creds.json` plus `michael2-service-key.json`
- renders `deployment/kubernetes/backend.yaml`
- applies the manifest and waits for the Deployment rollout

The environment-specific GKE workflows are:

- `.github/workflows/deploy-k8s-staging.yml`
- `.github/workflows/deploy-k8s-ca.yml`
- `.github/workflows/deploy-k8s-isgs.yml`
- `.github/workflows/deploy-k8s-newts.yml`
- `.github/workflows/deploy-k8s-osage.yml`

Manual dispatch is always allowed. Push-triggered GKE deploys are gated by repository variables so they do not run until you opt in.

## GCP prerequisites

Use the same GCP project as the VM deployment.

Enable these APIs, either through Terraform with `manage_project_services=true` or in the GCP console:

- Kubernetes Engine API: `container.googleapis.com`
- Compute Engine API: `compute.googleapis.com`
- Cloud DNS API: `dns.googleapis.com`

The identity running Terraform needs permissions to manage GKE, Compute addresses, and Cloud DNS. A practical setup is:

- `roles/container.admin`
- `roles/compute.networkAdmin`
- `roles/dns.admin`
- `roles/serviceusage.serviceUsageAdmin` if Terraform will enable APIs

The GitHub Actions service account in `SERVICE_KEY_JSON` needs enough access to fetch GKE credentials and apply Kubernetes resources. For the first staging test, use `roles/container.admin`. You can tighten this later with Kubernetes RBAC once the deployment path is proven.

## Create the GKE infrastructure

From the Terraform directory:

```bash
cd orphaned-wells-ui-server/deployment/terraform
terraform init
terraform plan -var-file=terraform.tfvars -var='enable_gke=true'
terraform apply -var-file=terraform.tfvars -var='enable_gke=true'
```

Then export the GitHub deploy target JSON:

```bash
terraform output -json kubernetes_deploy_targets | jq -c .
```

Store that JSON as the repository secret `K8S_DEPLOY_TARGETS`.

## Required GitHub secrets

Keep the existing secrets:

- `PROJECT_ID`
- `DOCKERHUB_USERNAME`
- `DOCKERHUB_ACCESS_TOKEN`
- `CREDS_JSON`
- `SERVICE_KEY_JSON`

Add or confirm the environment-file secrets:

- `STAGING_ENV`
- `CA_ENV`
- `ISGS_ENV`
- `NEWTS_ENV`
- `OSAGE_ENV`

Each environment secret should contain the same key/value pairs currently used by the VM `.env` file. The workflow overrides these values for Kubernetes:

- `ENVIRONMENT`
- `BACKEND_URL`
- `LOG_DIR`
- `LOCAL_STORAGE_ROOT`
- `LOCAL_STORAGE_URL_BASE`

Keep `COLLABORATOR` in the environment secret when that backend needs it. It is intentionally not derived from `DEPLOY_ENV`, because some deployment names and collaborator identifiers can differ.

## Deploy staging manually

Run the workflow in GitHub Actions:

```bash
gh workflow run deploy-k8s-staging.yml --ref main
```

Or use the GitHub UI: Actions -> Deploy Staging Server to GKE -> Run workflow.

After the workflow completes, check the rollout:

```bash
gcloud container clusters get-credentials uow-backend-gke --region us-central1 --project <PROJECT_ID>
kubectl -n uow-staging get pods
kubectl -n uow-staging get ingress backend
kubectl -n uow-staging get managedcertificate backend-cert
```

Google-managed certificates can take time to become `Active`. When the certificate is active:

```bash
curl -f https://staging-k8s-server.uow-carbon.org/health
curl -I http://staging-k8s-server.uow-carbon.org/health
```

The HTTP request should return a redirect to HTTPS.

## Enable automatic staging deploys

After the manual staging deploy works, add this repository variable:

```text
ENABLE_GKE_STAGING_DEPLOY=true
```

Pushes to `main` will then deploy staging to GKE. The existing VM workflow will still run until you remove or disable it.

## Cut over the primary staging hostname

The non-disruptive test hostname is `staging-k8s-server.uow-carbon.org`. To move the current hostname `staging-server.uow-carbon.org` to GKE, update Terraform variables:

```hcl
enable_gke = true

gke_backend_hostnames = {
  staging = "staging-server.uow-carbon.org"
}

primary_dns_to_gke_backends = ["staging"]
```

Apply Terraform:

```bash
terraform plan -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars
terraform output -json kubernetes_deploy_targets | jq -c .
```

Update the `K8S_DEPLOY_TARGETS` GitHub secret with the new output, then rerun:

```bash
gh workflow run deploy-k8s-staging.yml --ref main
```

Watch the certificate:

```bash
kubectl -n uow-staging get managedcertificate backend-cert -w
```

Do the same environment-by-environment after staging is validated. Use the environment-specific variables, for example:

```hcl
gke_backend_hostnames = {
  staging = "staging-server.uow-carbon.org"
  osage   = "osage-server.uow-carbon.org"
}

primary_dns_to_gke_backends = ["staging", "osage"]
```

## Enable automatic deploys for other environments

Use either one global repository variable:

```text
ENABLE_GKE_DEPLOYMENTS=true
```

Or opt in one environment at a time:

```text
ENABLE_GKE_CA_DEPLOY=true
ENABLE_GKE_ISGS_DEPLOY=true
ENABLE_GKE_NEWTS_DEPLOY=true
ENABLE_GKE_OSAGE_DEPLOY=true
```

## Notes

- The Kubernetes Deployment uses pod-local `emptyDir` volumes for `/logs` and `/data`. The deployed backend should continue using Google Cloud Storage for real document storage; GKE captures stdout logs centrally.
- The default GKE backend resources request 2 CPU and 6 GiB memory. The original 1 GiB memory request was too low for the current 8-worker container startup and caused GKE node-pressure evictions.
- The app still receives `creds.json` and `michael2-service-key.json` at `/code/ogrre/...`, matching the current Docker Compose paths.
- GKE replaces VM nginx/certbot with GKE Ingress, `ManagedCertificate`, `FrontendConfig`, and `BackendConfig`.
- The backend timeout is configured to 180 seconds through `BackendConfig`, matching the current nginx timeout.
- Google-managed certificates require the DNS name to point at the GKE load balancer before they become active. Test this timing in staging before production cutover.
