# Kubernetes deployment for orphaned-wells-ui-server

This directory contains the Kubernetes deployment template used to run each backend environment on GKE. Terraform owns the cloud infrastructure, and the GitHub Actions workflows render and apply this Kubernetes template for each backend environment.

The existing Compute Engine VM resources are still managed by Terraform. They can remain stopped in GCP while the Kubernetes deployment serves traffic.

## Architecture

Terraform in `deployment/terraform` creates:

- one shared GKE Autopilot cluster
- one global static IP per backend environment
- optional `<env>-k8s-server.uow-carbon.org` test DNS records
- optional primary DNS records, such as `staging-server.uow-carbon.org`, pointing to the GKE static IP
- `kubernetes_deploy_targets`, the JSON map consumed by GitHub Actions

Kubernetes creates one namespace per backend environment:

| Environment | Namespace |
| --- | --- |
| staging | `uow-staging` |
| isgs | `uow-isgs` |
| newts | `uow-newts` |
| osage | `uow-osage` |
| ca | `uow-ca` |

Each namespace contains:

- `Deployment/backend`
- `Service/backend`
- `BackendConfig/backend-config`
- `ManagedCertificate/backend-cert`
- `FrontendConfig/backend-frontend-config`
- `Ingress/backend`
- `Secret/backend-runtime-env`
- `Secret/backend-runtime-files`
- `Secret/dockerhub-pull`

## Rendered manifests

`deployment/kubernetes/backend.yaml` is the Kubernetes manifest template. The workflow renders it with environment-specific values like:

- `NAMESPACE`
- `DEPLOY_ENV`
- `IMAGE`
- `HOSTNAME`
- `STATIC_IP_NAME`
- CPU and memory requests/limits

The rendered file is an ephemeral deployment artifact:

```bash
deployment/kubernetes/rendered/backend.yaml
```

Do not commit rendered manifests. Commit changes to the template instead.

## GCP prerequisites

Use the same GCP project as the VM deployment.

Required APIs:

- Kubernetes Engine API: `container.googleapis.com`
- Compute Engine API: `compute.googleapis.com`
- Cloud DNS API: `dns.googleapis.com`

The identity running Terraform needs permissions to manage GKE, Compute addresses, and Cloud DNS. A practical setup is:

- `roles/container.admin`
- `roles/compute.networkAdmin`
- `roles/dns.admin`
- `roles/serviceusage.serviceUsageAdmin` if Terraform manages project services

The GitHub Actions service account in `SERVICE_KEY_JSON` needs enough access to fetch GKE credentials and apply Kubernetes resources. `roles/container.admin` is sufficient for the current deployment path.

## Deploy or update GKE infrastructure

From the Terraform directory:

```bash
cd orphaned-wells-ui-server/deployment/terraform
terraform init
terraform plan -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars
```

`enable_gke` defaults to `true`, so the GKE cluster, static IPs, and related GKE resources are included unless explicitly disabled.

To explicitly disable GKE planning:

```bash
terraform plan -var-file=terraform.tfvars -var='enable_gke=false'
```

## Derive `K8S_DEPLOY_TARGETS`

After Terraform apply, export the deployment target map:

```bash
cd orphaned-wells-ui-server/deployment/terraform
terraform output -json kubernetes_deploy_targets | jq -c .
```

Store that exact JSON as the GitHub repository secret:

```bash
gh secret set K8S_DEPLOY_TARGETS \
  --repo CATALOG-Historic-Records/orphaned-wells-ui-server \
  --body "$(terraform output -json kubernetes_deploy_targets | jq -c .)"
```

If you are working from a fork, replace `--repo` with the fork repository.

The JSON must contain the environment you deploy. For example:

```bash
terraform output -json kubernetes_deploy_targets | jq '.staging'
terraform output -json kubernetes_deploy_targets | jq '.newts'
```

The `host` field controls the Kubernetes Ingress host and the Google-managed certificate domain. DNS alone is not enough; after changing hostnames in Terraform, update `K8S_DEPLOY_TARGETS` and redeploy the backend.

## Required GitHub secrets

Keep the existing deployment secrets:

- `PROJECT_ID`
- `DOCKERHUB_USERNAME`
- `DOCKERHUB_ACCESS_TOKEN`
- `CREDS_JSON`
- `SERVICE_KEY_JSON`

Each backend environment also needs an environment-file secret:

- `STAGING_ENV`
- `CA_ENV`
- `ISGS_ENV`
- `NEWTS_ENV`
- `OSAGE_ENV`

The environment-file secret should contain the same key/value pairs used by the VM `.env` file. The workflow overrides these Kubernetes-owned values:

- `ENVIRONMENT`
- `BACKEND_URL`
- `LOG_DIR`
- `LOCAL_STORAGE_ROOT`
- `LOCAL_STORAGE_URL_BASE`

Keep `COLLABORATOR` in the environment secret when the backend needs it.

## GitHub Actions deployment

The staging workflow builds and pushes both `michaelpescelbl/orphaned-wells-ui-server:latest` and an immutable commit-SHA tag, then deploys staging with the SHA tag. The `latest` tag remains available for environment-specific backend deployments that run later:

```bash
gh workflow run deploy-k8s-staging.yml \
  --repo CATALOG-Historic-Records/orphaned-wells-ui-server \
  --ref main
```

The environment-specific workflows deploy the existing Docker image tag, defaulting to `latest`:

```bash
gh workflow run deploy-k8s-isgs.yml --repo CATALOG-Historic-Records/orphaned-wells-ui-server --ref isgs
gh workflow run deploy-k8s-newts.yml --repo CATALOG-Historic-Records/orphaned-wells-ui-server --ref newts
gh workflow run deploy-k8s-osage.yml --repo CATALOG-Historic-Records/orphaned-wells-ui-server --ref osage
gh workflow run deploy-k8s-ca.yml --repo CATALOG-Historic-Records/orphaned-wells-ui-server --ref ca
```

Automatic deploys are controlled by repository variables:

```text
ENABLE_GKE_DEPLOYMENTS=true
```

Or one environment at a time:

```text
ENABLE_GKE_STAGING_DEPLOY=true
ENABLE_GKE_CA_DEPLOY=true
ENABLE_GKE_ISGS_DEPLOY=true
ENABLE_GKE_NEWTS_DEPLOY=true
ENABLE_GKE_OSAGE_DEPLOY=true
```

## Command-line deployment without GitHub Actions

Use this path when you need to apply the Kubernetes manifest manually from your machine.

Required local tools:

- `gcloud`
- `kubectl`
- `terraform`
- `jq`
- `envsubst`

Authenticate to GKE:

```bash
gcloud container clusters get-credentials uow-backend-gke \
  --region us-central1 \
  --project <PROJECT_ID>
```

Set deployment variables from Terraform output:

```bash
cd orphaned-wells-ui-server

DEPLOY_ENV=staging
TARGETS_JSON="$(cd deployment/terraform && terraform output -json kubernetes_deploy_targets)"
NAMESPACE="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].namespace' <<< "$TARGETS_JSON")"
HOSTNAME="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].host' <<< "$TARGETS_JSON")"
STATIC_IP_NAME="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].static_ip_name' <<< "$TARGETS_JSON")"
REPLICAS="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].replicas // 1' <<< "$TARGETS_JSON")"
CPU_REQUEST="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].cpu_request // "2"' <<< "$TARGETS_JSON")"
MEMORY_REQUEST="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].memory_request // "6Gi"' <<< "$TARGETS_JSON")"
CPU_LIMIT="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].cpu_limit // "2"' <<< "$TARGETS_JSON")"
MEMORY_LIMIT="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].memory_limit // "6Gi"' <<< "$TARGETS_JSON")"
PERSISTENT_DISK_SIZE="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].persistent_disk_size // "20Gi"' <<< "$TARGETS_JSON")"
IMAGE=michaelpescelbl/orphaned-wells-ui-server:latest
DEPLOY_RUN_ID="local-$(date +%s)"
```

Prepare local secrets:

```bash
mkdir -p deployment/secrets deployment/kubernetes/rendered

cp ogrre/.env_"$DEPLOY_ENV" deployment/secrets/runtime.env
cp ogrre/creds.json deployment/secrets/creds.json
cp ogrre/michael2-service-key.json deployment/secrets/michael2-service-key.json
```

Normalize the runtime env file the same way the workflow does:

```bash
raw_env_file=deployment/secrets/runtime.env
k8s_env_file=deployment/secrets/runtime.k8s.env
: > "$k8s_env_file"

while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ""|\#*) continue ;;
  esac
  line="${line#export }"
  [[ "$line" == *"="* ]] || continue
  key="${line%%=*}"
  value="${line#*=}"
  key="${key#"${key%%[![:space:]]*}"}"
  key="${key%"${key##*[![:space:]]}"}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  [[ "$value" == \"*\" && "$value" == *\" ]] && value="${value:1:${#value}-2}"
  [[ "$value" == \'*\' && "$value" == *\' ]] && value="${value:1:${#value}-2}"
  case "$key" in
    ENVIRONMENT|BACKEND_URL|LOG_DIR|LOCAL_STORAGE_ROOT|LOCAL_STORAGE_URL_BASE) continue ;;
  esac
  printf '%s=%s\n' "$key" "$value" >> "$k8s_env_file"
done < "$raw_env_file"

{
  echo "ENVIRONMENT=$DEPLOY_ENV"
  echo "BACKEND_URL=https://$HOSTNAME"
  echo "LOG_DIR=/logs"
  echo "LOCAL_STORAGE_ROOT=/data/local-storage"
  echo "LOCAL_STORAGE_URL_BASE=https://$HOSTNAME/local-storage"
} >> "$k8s_env_file"
```

Create or update Kubernetes Secrets:

```bash
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NAMESPACE" create secret docker-registry dockerhub-pull \
  --docker-server=https://index.docker.io/v1/ \
  --docker-username="$DOCKERHUB_USERNAME" \
  --docker-password="$DOCKERHUB_ACCESS_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NAMESPACE" create secret generic backend-runtime-env \
  --from-env-file=deployment/secrets/runtime.k8s.env \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NAMESPACE" create secret generic backend-runtime-files \
  --from-file=creds.json=deployment/secrets/creds.json \
  --from-file=michael2-service-key.json=deployment/secrets/michael2-service-key.json \
  --dry-run=client -o yaml | kubectl apply -f -
```

Render and apply the manifest:

```bash
RUNTIME_CONFIG_SHA="$(
  shasum -a 256 \
    deployment/secrets/runtime.k8s.env \
    deployment/secrets/creds.json \
    deployment/secrets/michael2-service-key.json \
    | shasum -a 256 \
    | awk '{print $1}'
)"

export DEPLOY_ENV NAMESPACE HOSTNAME STATIC_IP_NAME REPLICAS CPU_REQUEST MEMORY_REQUEST CPU_LIMIT MEMORY_LIMIT PERSISTENT_DISK_SIZE IMAGE DEPLOY_RUN_ID RUNTIME_CONFIG_SHA
envsubst < deployment/kubernetes/backend.yaml > deployment/kubernetes/rendered/backend.yaml
kubectl apply -f deployment/kubernetes/rendered/backend.yaml
kubectl -n "$NAMESPACE" rollout status deployment/backend --timeout=10m
```

## Status commands

Get credentials:

```bash
gcloud container clusters get-credentials uow-backend-gke \
  --region us-central1 \
  --project <PROJECT_ID>
```

List namespaces:

```bash
kubectl get namespaces | grep '^uow-'
```

Check one backend:

```bash
kubectl -n uow-staging get deployment backend
kubectl -n uow-staging get pods -l app.kubernetes.io/name=orphaned-wells-ui-server -o wide
kubectl -n uow-staging get ingress backend
kubectl -n uow-staging get managedcertificate backend-cert
```

Check every backend:

```bash
for ns in uow-staging uow-isgs uow-newts uow-osage uow-ca; do
  echo "== $ns =="
  kubectl -n "$ns" get deployment backend
  kubectl -n "$ns" get pods -l app.kubernetes.io/name=orphaned-wells-ui-server -o wide
done
```

Describe a problematic pod:

```bash
kubectl -n uow-newts describe pods -l app.kubernetes.io/name=orphaned-wells-ui-server
kubectl -n uow-newts get events --sort-by=.lastTimestamp
```

## Logs

Logs for one environment:

```bash
kubectl -n uow-newts logs deployment/backend --tail=200
kubectl -n uow-newts logs deployment/backend --tail=200 -f
```

Logs for a specific pod:

```bash
POD="$(kubectl -n uow-newts get pods -l app.kubernetes.io/name=orphaned-wells-ui-server -o jsonpath='{.items[0].metadata.name}')"
kubectl -n uow-newts logs "$POD" --tail=200
kubectl -n uow-newts logs "$POD" --tail=200 -f
```

Previous container logs after a restart:

```bash
kubectl -n uow-newts logs deployment/backend --previous --tail=200
```

## Resource usage

There is no direct GKE Autopilot equivalent to SSHing into a VM and running `free -h` on the host. The nodes are managed by GKE. Use Kubernetes metrics instead:

```bash
kubectl top pods -n uow-staging
kubectl top pods -n uow-newts
kubectl top nodes
```

Show requested and limited resources for a pod:

```bash
kubectl -n uow-staging describe pod \
  "$(kubectl -n uow-staging get pods -l app.kubernetes.io/name=orphaned-wells-ui-server -o jsonpath='{.items[0].metadata.name}')"
```

You can also inspect memory from inside the container:

```bash
kubectl -n uow-staging exec deployment/backend -- free -h
```

That reports the container runtime view, not a dedicated VM host.

## Restarting workloads

Restart one backend Deployment:

```bash
kubectl -n uow-staging rollout restart deployment/backend
kubectl -n uow-staging rollout status deployment/backend --timeout=10m
```

Delete one pod and let the Deployment recreate it:

```bash
POD="$(kubectl -n uow-staging get pods -l app.kubernetes.io/name=orphaned-wells-ui-server -o jsonpath='{.items[0].metadata.name}')"
kubectl -n uow-staging delete pod "$POD"
```

Restart all backend Deployments:

```bash
for ns in uow-staging uow-isgs uow-newts uow-osage uow-ca; do
  kubectl -n "$ns" rollout restart deployment/backend
done
```

GKE Autopilot does not expose a normal "restart the cluster" operation like restarting a VM. Restart the workload Deployment, or use Terraform/GKE upgrade operations when you need to change cluster infrastructure.

## Adding a new collaborator backend

1. Add the collaborator to `local.collaborators` in `deployment/terraform/main.tf`.

2. Add its canonical hostname if it should use a primary backend hostname:

```hcl
gke_backend_hostnames = {
  staging = "staging-server.uow-carbon.org"
  newts   = "newts-server.uow-carbon.org"
  boots   = "boots-server.uow-carbon.org"
}
```

3. Add it to `primary_dns_to_gke_backends` when the primary DNS record should point to GKE:

```hcl
primary_dns_to_gke_backends = ["staging", "newts", "boots"]
```

4. Apply Terraform:

```bash
cd orphaned-wells-ui-server/deployment/terraform
terraform plan -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars
```

5. Export and update `K8S_DEPLOY_TARGETS`:

```bash
terraform output -json kubernetes_deploy_targets | jq -c .
```

6. Add a GitHub environment-file secret for the collaborator, for example `BOOTS_ENV`.

7. Update `.github/workflows/deploy-k8s-dispatch.yml`:

- add the new environment to `workflow_dispatch.inputs.DEPLOY_ENV.options`
- add the new environment secret to `workflow_call.secrets`
- add the secret to the `Prepare runtime env file` env block
- add a case branch that maps the new `DEPLOY_ENV` to that secret

8. Optionally add a dedicated workflow like `.github/workflows/deploy-k8s-boots.yml`.

9. Deploy the new backend and verify:

```bash
kubectl -n uow-boots get deployment backend
kubectl -n uow-boots get pods -l app.kubernetes.io/name=orphaned-wells-ui-server -o wide
kubectl -n uow-boots get managedcertificate backend-cert
curl -f https://boots-server.uow-carbon.org/health
```

## Notes

- GKE replaces VM nginx/certbot with GKE Ingress, `ManagedCertificate`, `FrontendConfig`, and `BackendConfig`.
- Google-managed certificates require the DNS name to point at the GKE load balancer before they become active.
- DNS pointing at the load balancer is not enough by itself. The rendered Kubernetes Ingress `host` and ManagedCertificate domain must also match the hostname.
- The backend timeout is configured to 180 seconds through `BackendConfig`, matching the current nginx timeout.
- The Kubernetes Deployment uses pod-local `emptyDir` volumes for `/logs` and `/data`. Real document storage should continue using Google Cloud Storage.
- The app receives `creds.json` and `michael2-service-key.json` at `/code/ogrre/...`, matching the current Docker Compose paths.
- The default GKE backend resources request 2 CPU and 6 GiB memory because the current container starts 8 Uvicorn workers.
