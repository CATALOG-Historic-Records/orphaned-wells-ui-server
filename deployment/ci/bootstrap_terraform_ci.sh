#!/usr/bin/env bash
# Operator-run bootstrap, deliberately outside the application Terraform state.
# Requires gcloud, gh, and python3 on PATH.
# Requires project IAM/service-account/WIF administration and bucket administration.
set -euo pipefail

: "${PROJECT_ID:?Set PROJECT_ID}"
: "${TF_WORKSPACE:?Set TF_WORKSPACE to the existing remote workspace}"
: "${TF_CI_BUCKET:?Set TF_CI_BUCKET to a new, private CI artifact bucket name}"
: "${DEPLOY_SERVICE_ACCOUNT:?Set DEPLOY_SERVICE_ACCOUNT to the existing deployment account email}"
REPOSITORY=CATALOG-Historic-Records/orphaned-wells-ui-server
STATE_BUCKET=tidy-outlet-412020-ogrre-terraform-state
STATE_PREFIX=orphaned-wells-ui-server
POOL=ogrre-terraform
PROVIDER=github
PLAN_ACCOUNT="github-terraform-plan@${PROJECT_ID}.iam.gserviceaccount.com"
APPLY_ACCOUNT="github-terraform-ci@${PROJECT_ID}.iam.gserviceaccount.com"

[[ "$TF_WORKSPACE" =~ ^[a-zA-Z0-9_-]+$ ]] || { echo 'Invalid workspace'; exit 1; }
[[ "$TF_CI_BUCKET" =~ ^[a-z0-9][a-z0-9._-]+[a-z0-9]$ ]] || { echo 'Invalid CI bucket name'; exit 1; }
[[ "$TF_CI_BUCKET" != "$STATE_BUCKET" ]] || { echo 'Use a separate CI bucket; its plans expire automatically.'; exit 1; }

project_number="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
repository_id="$(gh api "repos/$REPOSITORY" --jq .id)"
owner_id="$(gh api "repos/$REPOSITORY" --jq .owner.id)"
gcloud storage objects describe "gs://$STATE_BUCKET/$STATE_PREFIX/$TF_WORKSPACE.tfstate" --format='value(name)'

gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com --project="$PROJECT_ID"
for account in github-terraform-plan github-terraform-ci; do
  if ! gcloud iam service-accounts describe "$account@$PROJECT_ID.iam.gserviceaccount.com" --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$account" --project="$PROJECT_ID"
  fi
done
if ! gcloud iam workload-identity-pools describe "$POOL" --location=global --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "$POOL" --location=global --project="$PROJECT_ID" --display-name='OGRRE Terraform CI'
fi

# Only main reconciliation can authenticate; PR and workflow_run events are denied.
apply_workflow="$REPOSITORY/.github/workflows/terraform-apply.yml@refs/heads/main"
pipeline="('job_workflow_ref' in assertion && assertion.job_workflow_ref == '$apply_workflow' && assertion.ref == 'refs/heads/main' && assertion.event_name in ['push', 'workflow_dispatch']) ? (assertion.sub == 'repo:$REPOSITORY:environment:terraform-apply' ? 'apply' : 'main-plan') : 'denied'"
condition="assertion.repository_id == '$repository_id' && assertion.repository_owner_id == '$owner_id' && attribute.pipeline != 'denied'"
if gcloud iam workload-identity-pools providers describe "$PROVIDER" --workload-identity-pool="$POOL" --location=global --project="$PROJECT_ID" >/dev/null 2>&1; then
  provider_operation=update-oidc
else
  provider_operation=create-oidc
fi
# Use an alternate dictionary delimiter: the CEL expression contains commas.
gcloud iam workload-identity-pools providers "$provider_operation" "$PROVIDER" \
  --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL" \
  --issuer-uri=https://token.actions.githubusercontent.com \
  --attribute-mapping="^~^google.subject=assertion.sub~attribute.pipeline=$pipeline" \
  --attribute-condition="$condition"

principal_base="principalSet://iam.googleapis.com/projects/$project_number/locations/global/workloadIdentityPools/$POOL/attribute.pipeline"
# Retire the grant from the former approval-gated fork plan workflow. Fetch and
# parse separately so an IAM read failure cannot be mistaken for an absent grant.
plan_policy="$(gcloud iam service-accounts get-iam-policy "$PLAN_ACCOUNT" --project="$PROJECT_ID" --format=json)"
has_pr_binding="$(python3 -c '
import json, sys
policy = json.load(sys.stdin)
print(any(binding.get("role") == "roles/iam.workloadIdentityUser"
          and not binding.get("condition") and sys.argv[1] in binding.get("members", [])
          for binding in policy.get("bindings", [])))
' "$principal_base/pr-plan" <<< "$plan_policy")"
if [[ "$has_pr_binding" == True ]]; then
  gcloud iam service-accounts remove-iam-policy-binding "$PLAN_ACCOUNT" --project="$PROJECT_ID" \
    --role=roles/iam.workloadIdentityUser --member="$principal_base/pr-plan" --condition=None >/dev/null
fi
# Explicit None avoids prompts when a policy already has conditional bindings.
gcloud iam service-accounts add-iam-policy-binding "$PLAN_ACCOUNT" --project="$PROJECT_ID" \
  --role=roles/iam.workloadIdentityUser --member="$principal_base/main-plan" --condition=None >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$APPLY_ACCOUNT" --project="$PROJECT_ID" \
  --role=roles/iam.workloadIdentityUser --member="$principal_base/apply" --condition=None >/dev/null

for role in roles/container.admin roles/compute.networkAdmin roles/dns.admin roles/storage.admin roles/serviceusage.serviceUsageAdmin; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$APPLY_ACCOUNT" --role="$role" --condition=None >/dev/null
done
for role in roles/container.viewer roles/compute.viewer roles/dns.reader roles/serviceusage.serviceUsageViewer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$PLAN_ACCOUNT" --role="$role" --condition=None >/dev/null
done

# Bucket metadata reads are sufficient to refresh the managed upload buckets.
storage_role=ogrreTerraformStorageReader
if gcloud iam roles describe "$storage_role" --project="$PROJECT_ID" >/dev/null 2>&1; then
  role_operation=update
else
  role_operation=create
fi
gcloud iam roles "$role_operation" "$storage_role" --project="$PROJECT_ID" \
  --title='OGRRE Terraform bucket metadata reader' \
  --permissions=storage.buckets.get,storage.buckets.list,storage.buckets.getIamPolicy --stage=GA
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$PLAN_ACCOUNT" \
  --role="projects/$PROJECT_ID/roles/$storage_role" --condition=None >/dev/null

for account in "$PLAN_ACCOUNT" "$DEPLOY_SERVICE_ACCOUNT"; do
  gcloud storage buckets add-iam-policy-binding "gs://$STATE_BUCKET" \
    --member="serviceAccount:$account" --role=roles/storage.objectViewer --condition=None >/dev/null
done
# Planning can acquire/release this workspace's lock, but cannot write state.
gcloud storage buckets add-iam-policy-binding "gs://$STATE_BUCKET" \
  --member="serviceAccount:$PLAN_ACCOUNT" --role=roles/storage.objectAdmin \
  --condition="expression=resource.name == 'projects/_/buckets/$STATE_BUCKET/objects/$STATE_PREFIX/$TF_WORKSPACE.tflock',title=terraform-plan-lock" >/dev/null

if ! gcloud storage buckets describe "gs://$TF_CI_BUCKET" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://$TF_CI_BUCKET" --project="$PROJECT_ID" --location=us-central1 --uniform-bucket-level-access
fi
gcloud storage buckets update "gs://$TF_CI_BUCKET" --public-access-prevention --uniform-bucket-level-access
lifecycle_file="$(mktemp)"
trap 'rm -f "$lifecycle_file"' EXIT
cat > "$lifecycle_file" <<'JSON'
{"rule":[{"action":{"type":"Delete"},"condition":{"age":7,"matchesPrefix":["plans/"]}}]}
JSON
gcloud storage buckets update "gs://$TF_CI_BUCKET" --lifecycle-file="$lifecycle_file"
for account in "$PLAN_ACCOUNT" "$DEPLOY_SERVICE_ACCOUNT"; do
  gcloud storage buckets add-iam-policy-binding "gs://$TF_CI_BUCKET" \
    --member="serviceAccount:$account" --role=roles/storage.objectViewer --condition=None >/dev/null
done
# No-change completion may replace only this workspace's CI readiness record.
# It gains no Terraform state writes, artifact writes, or infrastructure writes.
gcloud storage buckets add-iam-policy-binding "gs://$TF_CI_BUCKET" \
  --member="serviceAccount:$PLAN_ACCOUNT" --role=roles/storage.objectAdmin \
  --condition="expression=resource.name == 'projects/_/buckets/$TF_CI_BUCKET/objects/status/$TF_WORKSPACE.json',title=terraform-readiness" >/dev/null
# No PR identity can publish plans or readiness records. Object Creator cannot
# overwrite another run's saved plan; apply additionally verifies its checksum.
gcloud storage buckets add-iam-policy-binding "gs://$TF_CI_BUCKET" \
  --member="$principal_base/main-plan" --role=roles/storage.objectCreator \
  --condition="expression=resource.name.startsWith('projects/_/buckets/$TF_CI_BUCKET/objects/plans/'),title=main-plan-artifacts" >/dev/null

cat <<EOF
Bootstrap complete. Configure these repository variables after reviewing the IAM grants:
TF_WIF_PROVIDER=projects/$project_number/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER
TF_PLAN_SERVICE_ACCOUNT=$PLAN_ACCOUNT
TF_APPLY_SERVICE_ACCOUNT=$APPLY_ACCOUNT
TF_WORKSPACE=$TF_WORKSPACE
TF_CI_BUCKET=$TF_CI_BUCKET

Create and protect the terraform-apply GitHub Environment before setting
ENABLE_TERRAFORM_CI=true. See deployment/ci/README.md for the rollout procedure.
EOF
