# Kubernetes deployment for orphaned-wells-ui-server

This directory contains the Kubernetes deployment template used to run each backend environment on GKE. Terraform owns the cloud infrastructure and long-lived Kubernetes identity/RBAC objects. GitHub Actions renders and applies the application-workload template for each backend environment.

Legacy Compute Engine VM definitions remain in Terraform, but VMs are disabled unless explicitly listed in `enabled_legacy_backend_vms`. New GKE backends do not need a VM entry.

## Architecture

Terraform in `deployment/terraform` creates:

- one shared GKE Autopilot cluster
- one global static IP per backend environment
- one Cloud Storage upload bucket per unique backend bucket name
- optional `<env>-k8s-server.uow-carbon.org` test DNS records
- primary DNS records, such as `staging-server.uow-carbon.org`, pointing to the GKE static IP
- one namespace, two workload ServiceAccounts, and the processing Job Role/RoleBinding per backend environment
- `kubernetes_deploy_targets`, the JSON map consumed by GitHub Actions

Terraform creates one namespace per backend environment:

| Environment | Namespace |
| --- | --- |
| staging | `uow-staging` |
| isgs | `uow-isgs` |
| newts | `uow-newts` |
| osage | `uow-osage` |
| rrc | `uow-rrc` |

CA retains its Terraform-managed cloud configuration but currently has
`enable_kubernetes_workloads = false`. It has no `uow-ca` namespace, Kubernetes
runtime identities/RBAC, or deployment target until that setting is changed to
`true`.

Terraform manages these long-lived namespace resources:

- `ServiceAccount/backend-api`, which may create and inspect only processing Jobs in its namespace
- `ServiceAccount/processing-worker`, used by short-lived document-processing Pods
- `Role` and `RoleBinding` named `processing-job-dispatcher`
- namespace labels

The two runtime identities have deliberately different responsibilities:

| Pod type | Kubernetes ServiceAccount | Kubernetes API permission | Purpose |
| --- | --- | --- | --- |
| Always-running `Deployment/backend` API Pod | `backend-api` | Create and inspect processing Jobs; read their Pods | Dispatch and monitor a batch worker. |
| Short-lived processing Job Pod | `processing-worker` | None granted by this configuration | Process documents and update durable MongoDB job state. |

The Role is the namespace-scoped permission policy. The RoleBinding attaches
that policy to `backend-api`; without the binding, the Role grants nothing.
The processing worker intentionally is not bound to the Role, so a worker Pod
cannot create more Jobs or inspect unrelated Pods.

GitHub Actions manages these application resources:

- `Deployment/backend`
- `Service/backend`
- `BackendConfig/backend-config`
- `ManagedCertificate/backend-cert`
- `FrontendConfig/backend-frontend-config`
- `Ingress/backend`
- `Secret/backend-runtime-env`
- `Secret/backend-runtime-files`
- `Secret/dockerhub-pull`

The API is the only always-running workload. When a user submits a GCS batch,
the API writes a durable MongoDB job record and asks the Kubernetes API to
create a `batch/v1 Job`. Kubernetes then starts one high-memory worker Pod with
the same immutable image and runtime secrets. The worker updates the MongoDB
job record, exits, and Kubernetes removes it after the configured retention
period. The worker does not serve HTTP and is not a second Deployment.

## Rendered manifests

`deployment/kubernetes/backend.yaml` is the application-workload manifest template. Terraform manages namespaces, ServiceAccounts, and RBAC in `deployment/terraform/kubernetes_rbac.tf`; the workflow renders this template with environment-specific values like:

- `NAMESPACE`
- `DEPLOY_ENV`
- `IMAGE`
- `HOSTNAME`
- `STATIC_IP_NAME`
- `STORAGE_BUCKET_NAME`
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
- Cloud Storage API: `storage.googleapis.com`

The identity running Terraform needs permissions to manage GKE, Compute addresses, Cloud DNS, and Cloud Storage buckets. A practical setup is:

- `roles/container.admin`
- `roles/compute.networkAdmin`
- `roles/dns.admin`
- `roles/storage.admin`
- `roles/serviceusage.serviceUsageAdmin` if Terraform manages project services

## Identities and Kubernetes authorization

Use three service accounts for the backend system:

| Service account | Used for | GitHub/local credential |
| --- | --- | --- |
| Storage runtime, for example `ogrre-storage-runtime` | Backend Cloud Storage upload bucket reads, writes, deletes, and signed/download URL interactions | `STORAGE_SERVICE_KEY_JSON` in GitHub; local `STORAGE_SERVICE_KEY` in `ogrre/.env` |
| Document AI runtime, for example `ogrre-document-ai` | Backend online/batch Document AI processing and processor deployment/undeployment | `DOCUMENT_AI_SERVICE_KEY_JSON` in GitHub; local `DOCUMENT_AI_SERVICE_KEY` in `ogrre/.env` |
| Terraform platform identity, for example a privileged human operator or dedicated infrastructure account | Terraform cloud infrastructure plus namespaces, runtime ServiceAccounts, and runtime RBAC | Local ADC or a dedicated Terraform credential |
| GitHub deployment identity, `ogrre-deployment-ci` | Creates deployment Secrets and applies the workload-only manifest | `DEPLOYMENT_SERVICE_KEY_JSON` in GitHub |

The Terraform platform identity must be authorized to create Kubernetes Roles
and RoleBindings. The documented `roles/container.admin` is sufficient, though
a separate platform identity is preferable to giving that broad role to the
GitHub deployment identity. `roles/container.developer`, which the GitHub
identity already has, is sufficient for the workload-only GitHub Actions
manifest but intentionally cannot create or delegate Kubernetes RBAC roles.

Keep Cloud Storage and Document AI runtime access on the dedicated runtime service accounts, not on the deployment service account.

There is no GitHub Actions RBAC bootstrap command in this design. A Terraform
apply creates the runtime identities and their least-privilege permissions
before a deployment is attempted. Existing namespaces must be imported into
Terraform state once; see `../terraform/README.md#existing-gke-namespaces`.
Do not run the previously documented `ogrre-backend-namespace-manager`
bootstrap: it is superseded by Terraform ownership.

## Deploy or update GKE infrastructure

From the Terraform directory:

```bash
cd orphaned-wells-ui-server/deployment/terraform
terraform init
terraform plan
terraform apply
```

`enable_gke` defaults to `true`, so the GKE cluster, static IPs, and related GKE resources are included unless explicitly disabled.

To explicitly disable GKE planning:

```bash
terraform plan -var='enable_gke=false'
```

## Derive `K8S_DEPLOY_TARGETS`

After Terraform apply, export the deployment target map:

```bash
cd orphaned-wells-ui-server/deployment/terraform
terraform output -json kubernetes_deploy_targets | jq -c .
```

Store that exact JSON as the GitHub repository secret:

```bash
gh auth login
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

The target map also includes worker resource configuration. GitHub Actions
generates `UVICORN_WORKERS`, `PROCESSING_JOB_*`, and
`PROCESSING_JOB_MODE=kubernetes` in `backend-runtime-env`; do not add those
generated values to collaborator runtime-env secrets. See
`../terraform/README.md#batch-worker-resource-configuration` for the source
settings and defaults.

## Required GitHub secrets

Keep the existing deployment secrets:

- `PROJECT_ID`
- `DOCKERHUB_USERNAME`
- `DOCKERHUB_ACCESS_TOKEN`
- `DEPLOYMENT_SERVICE_KEY_JSON`
- `STORAGE_SERVICE_KEY_JSON`
- `DOCUMENT_AI_SERVICE_KEY_JSON`
- `K8S_DEPLOY_TARGETS`

Each backend environment also needs an environment-file secret:

- `STAGING_ENV`
- `ISGS_ENV`
- `NEWTS_ENV`
- `OSAGE_ENV`
- `RRC_ENV`

The environment-file secret should contain the same key/value pairs used by the VM `.env` file. The workflow overrides these Kubernetes-owned values:

- `ENVIRONMENT`
- `BACKEND_URL`
- `LOG_DIR`
- `LOCAL_STORAGE_ROOT`
- `LOCAL_STORAGE_URL_BASE`
- `STORAGE_BUCKET_NAME`
- `STORAGE_SERVICE_KEY`
- `DOCUMENT_AI_SERVICE_KEY`

Keep `COLLABORATOR` in the environment secret when the backend needs it.

Use `ogrre/.env.example` as the source of truth for environment-file contents. Omit local file paths and local-only settings that the workflow owns, and store real values in the environment-specific GitHub secret such as `STAGING_ENV` or `ISGS_ENV`.

## GitHub Actions deployment

The staging workflow builds and pushes both `michaelpescelbl/orphaned-wells-ui-server:latest` and an immutable commit-SHA tag, then deploys staging with the SHA tag. Environment-specific backend deployments promote an existing commit-SHA tag rather than deploying `latest`:

```bash
gh workflow run deploy-k8s-staging.yml \
  --repo CATALOG-Historic-Records/orphaned-wells-ui-server \
  --ref main
```

The environment-specific workflows default `IMAGE_TAG` to `auto`. On collaborator branch merge commits, `auto` resolves to the second parent commit, which is the main commit that staging built and tested. On fast-forward or non-merge commits, `auto` resolves to the current commit. Manually entering `latest` is allowed as an explicit override, but it is a mutable tag and can recreate mixed-image replicas after later pod replacement:

```bash
gh workflow run deploy-k8s-isgs.yml --repo CATALOG-Historic-Records/orphaned-wells-ui-server --ref isgs
gh workflow run deploy-k8s-newts.yml --repo CATALOG-Historic-Records/orphaned-wells-ui-server --ref newts
gh workflow run deploy-k8s-osage.yml --repo CATALOG-Historic-Records/orphaned-wells-ui-server --ref osage
gh workflow run deploy-k8s-rrc.yml --repo CATALOG-Historic-Records/orphaned-wells-ui-server --ref rrc
```

Automatic deploys are controlled by repository variables:

```text
ENABLE_GKE_DEPLOYMENTS=true
```

Or one environment at a time:

```text
ENABLE_GKE_STAGING_DEPLOY=true
ENABLE_GKE_ISGS_DEPLOY=true
ENABLE_GKE_NEWTS_DEPLOY=true
ENABLE_GKE_OSAGE_DEPLOY=true
```

When Kubernetes deployment behavior changes, update the operator-facing frontend docs in `../orphaned-wells-ui/docs/docs/deploy-gcp` as part of the same work so the two repos stay aligned.

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
STORAGE_BUCKET_NAME="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].storage_bucket_name' <<< "$TARGETS_JSON")"
REPLICAS="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].replicas // 2' <<< "$TARGETS_JSON")"
CPU_REQUEST="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].cpu_request // "1850m"' <<< "$TARGETS_JSON")"
MEMORY_REQUEST="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].memory_request // "12Gi"' <<< "$TARGETS_JSON")"
CPU_LIMIT="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].cpu_limit // "1850m"' <<< "$TARGETS_JSON")"
MEMORY_LIMIT="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].memory_limit // "12Gi"' <<< "$TARGETS_JSON")"
PERSISTENT_DISK_SIZE="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].persistent_disk_size // "20Gi"' <<< "$TARGETS_JSON")"
API_UVICORN_WORKERS="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].api_uvicorn_workers // 2' <<< "$TARGETS_JSON")"
PROCESSING_JOB_CPU_REQUEST="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].processing_job_cpu_request // "1850m"' <<< "$TARGETS_JSON")"
PROCESSING_JOB_MEMORY_REQUEST="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].processing_job_memory_request // "12Gi"' <<< "$TARGETS_JSON")"
PROCESSING_JOB_CPU_LIMIT="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].processing_job_cpu_limit // "1850m"' <<< "$TARGETS_JSON")"
PROCESSING_JOB_MEMORY_LIMIT="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].processing_job_memory_limit // "12Gi"' <<< "$TARGETS_JSON")"
PROCESSING_JOB_EPHEMERAL_STORAGE="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].processing_job_ephemeral_storage // "10Gi"' <<< "$TARGETS_JSON")"
PROCESSING_JOB_ACTIVE_DEADLINE_SECONDS="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].processing_job_active_deadline_seconds // 86400' <<< "$TARGETS_JSON")"
PROCESSING_JOB_TTL_SECONDS_AFTER_FINISHED="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].processing_job_ttl_seconds_after_finished // 604800' <<< "$TARGETS_JSON")"
PROCESSING_JOB_MAX_ACTIVE="$(jq -r --arg env "$DEPLOY_ENV" '.[$env].processing_job_max_active // 1' <<< "$TARGETS_JSON")"
IMAGE_TAG=<tested-commit-sha>
IMAGE=michaelpescelbl/orphaned-wells-ui-server:"$IMAGE_TAG"
DEPLOY_RUN_ID="local-$(date +%s)"
```

Prepare local secrets:

```bash
mkdir -p deployment/secrets deployment/kubernetes/rendered

cp ogrre/.env_"$DEPLOY_ENV" deployment/secrets/runtime.env
cp ogrre/storage-service-key.json deployment/secrets/storage-service-key.json
cp ogrre/document-ai-service-key.json deployment/secrets/document-ai-service-key.json
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
    ENVIRONMENT|BACKEND_URL|LOG_DIR|LOCAL_STORAGE_ROOT|LOCAL_STORAGE_URL_BASE|STORAGE_BUCKET_NAME|STORAGE_SERVICE_KEY|DOCUMENT_AI_SERVICE_KEY|GOOGLE_APPLICATION_CREDENTIALS|UVICORN_WORKERS|PROCESSING_JOB_*) continue ;;
  esac
  printf '%s=%s\n' "$key" "$value" >> "$k8s_env_file"
done < "$raw_env_file"

{
  echo "ENVIRONMENT=$DEPLOY_ENV"
  echo "BACKEND_URL=https://$HOSTNAME"
  echo "LOG_DIR=/logs"
  echo "LOCAL_STORAGE_ROOT=/data/local-storage"
  echo "LOCAL_STORAGE_URL_BASE=https://$HOSTNAME/local-storage"
  echo "STORAGE_BUCKET_NAME=$STORAGE_BUCKET_NAME"
  echo "STORAGE_SERVICE_KEY=/code/ogrre/storage-service-key.json"
  echo "DOCUMENT_AI_SERVICE_KEY=/code/ogrre/document-ai-service-key.json"
  echo "UVICORN_WORKERS=$API_UVICORN_WORKERS"
  echo "PROCESSING_JOB_MODE=kubernetes"
  echo "PROCESSING_JOB_NAMESPACE=$NAMESPACE"
  echo "PROCESSING_JOB_IMAGE=$IMAGE"
  echo "PROCESSING_JOB_CPU_REQUEST=$PROCESSING_JOB_CPU_REQUEST"
  echo "PROCESSING_JOB_MEMORY_REQUEST=$PROCESSING_JOB_MEMORY_REQUEST"
  echo "PROCESSING_JOB_CPU_LIMIT=$PROCESSING_JOB_CPU_LIMIT"
  echo "PROCESSING_JOB_MEMORY_LIMIT=$PROCESSING_JOB_MEMORY_LIMIT"
  echo "PROCESSING_JOB_EPHEMERAL_STORAGE=$PROCESSING_JOB_EPHEMERAL_STORAGE"
  echo "PROCESSING_JOB_ACTIVE_DEADLINE_SECONDS=$PROCESSING_JOB_ACTIVE_DEADLINE_SECONDS"
  echo "PROCESSING_JOB_TTL_SECONDS_AFTER_FINISHED=$PROCESSING_JOB_TTL_SECONDS_AFTER_FINISHED"
  echo "PROCESSING_JOB_MAX_ACTIVE=$PROCESSING_JOB_MAX_ACTIVE"
} >> "$k8s_env_file"
```

Verify that Terraform has created the namespace, then create or update Kubernetes Secrets:

```bash
kubectl get namespace "$NAMESPACE"

kubectl -n "$NAMESPACE" create secret docker-registry dockerhub-pull \
  --docker-server=https://index.docker.io/v1/ \
  --docker-username="$DOCKERHUB_USERNAME" \
  --docker-password="$DOCKERHUB_ACCESS_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NAMESPACE" create secret generic backend-runtime-env \
  --from-env-file=deployment/secrets/runtime.k8s.env \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NAMESPACE" create secret generic backend-runtime-files \
  --from-file=storage-service-key.json=deployment/secrets/storage-service-key.json \
  --from-file=document-ai-service-key.json=deployment/secrets/document-ai-service-key.json \
  --dry-run=client -o yaml | kubectl apply -f -
```

Render and apply the manifest:

```bash
RUNTIME_CONFIG_SHA="$(
  shasum -a 256 \
    deployment/secrets/runtime.k8s.env \
    deployment/secrets/storage-service-key.json \
    deployment/secrets/document-ai-service-key.json \
    | shasum -a 256 \
    | awk '{print $1}'
)"

export DEPLOY_ENV NAMESPACE HOSTNAME STATIC_IP_NAME STORAGE_BUCKET_NAME REPLICAS CPU_REQUEST MEMORY_REQUEST CPU_LIMIT MEMORY_LIMIT PERSISTENT_DISK_SIZE IMAGE DEPLOY_RUN_ID RUNTIME_CONFIG_SHA
envsubst < deployment/kubernetes/backend.yaml > deployment/kubernetes/rendered/backend.yaml
kubectl apply -f deployment/kubernetes/rendered/backend.yaml
kubectl -n "$NAMESPACE" rollout status deployment/backend --timeout=10m

IMAGE_IDS="$(
  kubectl -n "$NAMESPACE" get pods \
    -l app.kubernetes.io/name=orphaned-wells-ui-server,app.kubernetes.io/component=api,uow.lbl.gov/environment="$DEPLOY_ENV" \
    -o json \
    | jq -r '.items[] | select(.metadata.deletionTimestamp == null) | .status.containerStatuses[]? | select(.name == "backend" and .ready == true) | .imageID' \
    | sort -u
)"
if [ "$(printf '%s\n' "$IMAGE_IDS" | sed '/^$/d' | wc -l | tr -d ' ')" -ne 1 ]; then
  echo "ready backend pods are running different image IDs"
  printf '%s\n' "$IMAGE_IDS"
  exit 1
fi
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
kubectl -n uow-staging get jobs -l app.kubernetes.io/component=processor
```

Check every backend:

```bash
for ns in uow-staging uow-isgs uow-newts uow-osage uow-rrc; do
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
kubectl -n uow-newts logs job/<worker-job-name> --tail=200
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
for ns in uow-staging uow-isgs uow-newts uow-osage uow-rrc; do
  kubectl -n "$ns" rollout restart deployment/backend
done
```

GKE Autopilot does not expose a normal "restart the cluster" operation like restarting a VM. Restart the workload Deployment, or use Terraform/GKE upgrade operations when you need to change cluster infrastructure.

## Adding a new collaborator backend

1. Add the collaborator to `gke_backend_overrides` in `deployment/terraform/terraform.tfvars`, or add it to the default `gke_backends` map in `variables.tf` when the collaborator should be part of the shared defaults.

For a standard GKE-only collaborator, the override is enough:

```hcl
gke_backend_overrides = {
  boots = {}
}
```

Set `enable_kubernetes_workloads = false` for a collaborator whose cloud
configuration should remain managed but which is not ready to deploy to GKE.
Terraform then omits the namespace, runtime ServiceAccounts/RBAC, and
`K8S_DEPLOY_TARGETS` entry. CA currently uses this setting; change it to
`true` before deploying CA through its existing workflow.

Optional per-backend settings can be added in the same map:

```hcl
gke_backend_overrides = {
  boots = {
    # Only set this when the bucket cannot use the default "boots_uploads" name.
    upload_bucket_name   = "existing-bucket-name"
    replicas             = 1
    cpu_request          = "1"
    memory_request       = "6Gi"
    cpu_limit            = "1"
    memory_limit         = "6Gi"
    persistent_disk_size = "20Gi"
  }
}
```

2. Apply Terraform:

```bash
cd orphaned-wells-ui-server/deployment/terraform
terraform plan
terraform apply
```

3. Export and update `K8S_DEPLOY_TARGETS`. Run `gh auth login` first if this machine has not been authenticated:

```bash
gh secret set K8S_DEPLOY_TARGETS \
  --repo CATALOG-Historic-Records/orphaned-wells-ui-server \
  --body "$(terraform output -json kubernetes_deploy_targets | jq -c .)"
```

4. Add a GitHub environment-file secret for the collaborator, for example `BOOTS_ENV`.

5. Update `.github/workflows/deploy-k8s-dispatch.yml`:

- add the new environment to `workflow_dispatch.inputs.DEPLOY_ENV.options`
- add the new environment secret to `workflow_call.secrets`
- add the secret to the `Prepare runtime env file` env block
- add a case branch that maps the new `DEPLOY_ENV` to that secret

6. Optionally add a dedicated workflow like `.github/workflows/deploy-k8s-boots.yml`.

7. Deploy the new backend and verify:

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
- The app receives `storage-service-key.json` and `document-ai-service-key.json` at `/code/ogrre/...`. The runtime env sets `STORAGE_SERVICE_KEY` and `DOCUMENT_AI_SERVICE_KEY` to those absolute paths so packaged Python imports do not resolve key filenames relative to `site-packages`.
- The default collaborator GKE backend resources request 1850m CPU and 12 GiB memory. Staging is intentionally smaller at 1 replica with 1 CPU and 6 GiB memory.
- Batch workers use separate per-environment resource requests. The initial rollout permits one active batch worker per environment and sets `backoffLimit: 0`; retry failed batches only after reviewing their durable job status and affected records.
