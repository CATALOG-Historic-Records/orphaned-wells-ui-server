# Terraform CI and backend deployment

`ENABLE_TERRAFORM_CI` is off unless its repository-variable value is exactly
`true`. With the flag off, the existing GKE workflows continue to use
`K8S_DEPLOY_TARGETS`. With it on, Terraform uses WIF and every updated GKE
workflow reads `kubernetes_deploy_targets` from the existing remote workspace.
`DEPLOYMENT_SERVICE_KEY_JSON` remains the deployment credential in phase one.

For the ordered first rollout and live test commands, use the
[rollout checklist](ROLLOUT.md). Manual operations are documented in the
[Terraform guide](../terraform/README.md#terraform-commands) and remain supported.

## Workflow sequence

- `terraform-checks.yml` checks PR diffs against `main`. Terraform changes run
  formatting and backend-free validation. Kubernetes changes render and check
  the workload manifest, including its GKE resources, without cloud credentials.
- `terraform-plan.yml` runs from upstream `main` after a successful **Deployment
  checks** PR run (`workflow_run`). This includes fork PRs. It independently
  identifies the open PR and checks the complete diff using upstream helpers.
  Terraform input/helper changes queue a job behind the `terraform-plan`
  Environment. Kubernetes-only, application-only, and Terraform documentation
  changes do not request a cloud-plan approval.
- The plan job's name and preparation summary identify the PR and exact commit.
  A required reviewer can approve their own plan run when **Prevent self-review**
  is unchecked. Approval authorizes the reviewed configuration to use cloud/state
  read credentials; it does not approve merging or applying. After approval, the
  job rechecks that the PR is still open and its head/base commits are unchanged,
  then checks out that exact head SHA. Only then does it authenticate through WIF.
  PR plans are informational and are never uploaded for apply.
- The PR commit's **Terraform PR plan** status links to the upstream run and its
  summary. A failed, rejected, cancelled, or outdated plan cannot report success.
  The plan uses the PR head configuration; update the fork branch from `main`
  before reviewing a plan if the branch is behind. Merging always generates the
  authoritative plan from `main` when reconciliation is needed.
- `deploy-k8s-staging.yml` coordinates every `main` push and manual dispatch.
  It builds the commit's image and calls `terraform-apply.yml`. The latter checks
  the last successful apply against the tracked Terraform inputs and the remote
  state object's generation. When they match, no plan or apply is needed.
- When reconciliation is needed, CI saves a fresh plan for that merged commit
  and publishes its readable summary. The `terraform-apply` Environment holds
  the apply job for approval. Apply downloads and verifies that exact saved plan,
  authenticates afresh, and refuses inputs superseded by a newer `main` commit.
- Only successful reconciliation releases staging deployment. Deployments read
  live targets and preserve the existing immutable image-tag behavior.

The apply job and every updated deployment share a concurrency group per
workspace with cancellation disabled. The staging orchestrator has a separate
group, avoiding nested-workflow deadlocks. GitHub may replace a pending run with
a newer one; comparing against the last applied inputs, rather than only the
latest push diff, means a later backend-only commit still reconciles any pending
infrastructure. Failed or rejected applies never release staging deployment.

Collaborator workflows retain their existing branch-promotion behavior. PRs
targeting collaborator branches do not run Terraform PR checks, and pushes or
merges to those branches do not start a Terraform plan or apply. Infrastructure
changes must reach `main` to be reconciled. Their shared deploy workflow checks
the current `main` infrastructure revision before
mutation. If it is pending or failed, deployment stops with a retry instruction;
it does not silently fall back to the secret. Retry after the gated apply succeeds.
No application version is automatically promoted to collaborator environments
just because shared infrastructure changed.

## One-time setup

1. Confirm `backend.tf` points to the existing state and confirm the workspace
   (`ogrre` in the current operator documentation). Complete existing-resource
   imports first. CI never creates a replacement workspace or migrates state.
2. Reproduce production inputs from tracked `variables.tf` defaults and modules.
   Required local `terraform.tfvars` overrides must be incorporated into reviewed
   non-secret configuration before enabling CI. CI does not load a developer's
   local overrides. Keep desired shared infrastructure configuration reviewed
   and committed to `main`, whether an operator or CI performs the apply.
3. Review and run the bootstrap script below using a human administrator. It
   creates WIF and CI accounts, grants permissions, and configures a **dedicated**
   private plan bucket. It does not enable workflows or apply application
   Terraform. The plan bucket's lifecycle deletes `plans/` objects after seven
   days; `status/` readiness records do not expire.

   ```bash
   export PROJECT_ID=tidy-outlet-412020
   export TF_WORKSPACE=ogrre
   export TF_CI_BUCKET=tidy-outlet-412020-ogrre-terraform-ci
   export DEPLOY_SERVICE_ACCOUNT=ogrre-deployment-ci@tidy-outlet-412020.iam.gserviceaccount.com
   bash deployment/ci/bootstrap_terraform_ci.sh
   ```

   Bootstrap requires IAM policy administration, service-account and WIF
   administration, service activation, and bucket administration. These are
   operator permissions, not additional permissions for the CI plan account.

4. Create both GitHub Environments before enabling CI:

   | Environment | Required reviewers | Prevent self-review |
   | --- | --- | --- |
   | `terraform-plan` | You and/or your infrastructure maintainer team | **Unchecked**, so an authorized PR author can approve planning |
   | `terraform-apply` | People/team authorized to approve infrastructure changes | Independent apply policy; if checked, another reviewer must approve runs you initiate |

   For both, restrict deployment branches to the **branch** `main` and disable
   administrator bypass. The fork plan's upstream `workflow_run` also runs on
   `main`; do not allow fork branches or tags to request this identity.
   Keep the normal PR approval requirement on `main`. Environment self-approval
   does not allow an author to approve their own PR for merging.

   Merely referencing the Environment in YAML creates it without approval rules.
   CI checks for required reviewers before queueing a PR plan and again before
   its authentication, as well as before main plan/apply. Check the branch and bypass
   settings explicitly; the Environment is the human approval boundary.

5. Add the repository variables printed by bootstrap:

   | Variable | Meaning |
   | --- | --- |
   | `TF_WIF_PROVIDER` | Full Google WIF provider resource name |
   | `TF_PLAN_SERVICE_ACCOUNT` | `github-terraform-plan` email |
   | `TF_APPLY_SERVICE_ACCOUNT` | `github-terraform-ci` email |
   | `TF_WORKSPACE` | Existing remote workspace |
   | `TF_CI_BUCKET` | Private CI bucket name, without `gs://` |
   | `ENABLE_TERRAFORM_CI` | Leave unset or `false` until rollout is ready |

   These identifiers are variables, not keys or secrets. For example:

   ```bash
   gh variable set TF_WORKSPACE --repo CATALOG-Historic-Records/orphaned-wells-ui-server --body ogrre
   ```

## Identities and artifact permissions

The WIF provider checks immutable repository/owner IDs and workflow claims.
The PR plan identity requires `workflow_ref` for upstream `terraform-plan.yml`
on `main`, event `workflow_run`, and subject Environment `terraform-plan`.
The preparation and status-reporting jobs cannot authenticate to GCP. The plan
job uses upstream helpers and a separately checked-out approved PR revision;
it never restores PR artifacts or caches. Ordinary fork `pull_request` runs
cannot obtain this identity, even if they modify their workflow permissions.
Only the main reconciliation workflow's environment-gated job can impersonate
the apply account, using its separate `job_workflow_ref` and Environment claims.
Rerun bootstrap if the previous same-repository-only WIF configuration was installed.

- **Plan account:** Compute, GKE, DNS, project-service and bucket-metadata reads;
  state object reads; creation/deletion of this workspace's `.tflock` object.
  It cannot write state, change infrastructure, publish plans, or mark an apply successful.
- **Apply account:** `container.admin`, `compute.networkAdmin`, `dns.admin`,
  `storage.admin`, and `serviceusage.serviceUsageAdmin`, matching the current GKE
  stack. Bootstrap grants no account-management or WIF-administration roles.
  If legacy VM management is deliberately enabled later, review the additional
  instance and VM-service-account permissions before planning that change.
- **Main plan publisher:** direct federation, without account impersonation,
  can create objects under `plans/` only. PR identities cannot publish executable
  plans. Existing objects cannot be overwritten. Apply checks the plan checksum,
  commit, workspace, state path, and run/attempt before use.
- **Existing deploy account:** keeps its deployment key and workload permissions;
  bootstrap adds object reads on the state and CI buckets. No apply role is added.

State access exposes the complete Terraform state, not just outputs. Plan files
can also contain sensitive values. Binary plans and their metadata remain in the
private CI bucket; only Terraform's human-readable, sensitive-value-redacted
plan output is published in GitHub summaries. Do not upload raw state or binary
plans as public workflow artifacts.

Terraform uses `deployment/terraform/.terraform-version` (currently 1.13.5) and
the checked-in provider lockfile. Follow
[provider lockfile maintenance](../terraform/README.md#provider-lockfile-maintenance)
when updating providers so read-only initialization works on Linux and macOS.
Local operators must also install
`gke-gcloud-auth-plugin` (`gcloud components install gke-gcloud-auth-plugin`) and
authenticate with ADC. The Kubernetes provider invokes the plugin separately
for planning/applying instead of saving a short-lived token in the plan.

## Enable and verify

1. With the flag disabled, merge the full implementation into `main` through
   one reviewed PR. Static checks no longer call a reusable PR-plan workflow,
   so there is no two-PR bootstrap dependency. The automatic fork-plan workflow
   becomes available after it lands on upstream `main`; verify it with a new
   fork PR after completing setup.
2. Propagate the updated reusable deployment workflow to every enabled
   collaborator branch **before** turning the flag on. Old branch workflows do
   not understand the readiness gate. Keep automatic collaborator promotion paused
   during this rollout using their existing deployment enablement variables.
3. Complete bootstrap, variable setup, and both Environments' protection. Preserve the
   existing secrets. Allow time for GCP IAM propagation.
4. Set `ENABLE_TERRAFORM_CI=true` and manually dispatch **Deploy Staging Server to
   GKE** on `main`. The initial absent readiness record requires a reviewed apply,
   even if the plan has no resource changes. Inspect the complete plan before approval.
5. Verify apply, live target reads, and staging rollout. Test a Terraform PR from
   your normal fork: static checks pass, the upstream plan queues, you approve
   `terraform-plan`, and the PR status links to the plan. Test a newer commit
   while approval waits, a rejected plan, a Kubernetes-only PR, a rejected apply,
   a backend-only follow-up, and a collaborator deployment.
6. Restore collaborator promotion settings. Delete `K8S_DEPLOY_TARGETS` only after
   all active branches use the enabled path and the rollout is confirmed. Keep
   `DEPLOYMENT_SERVICE_KEY_JSON`; deployment WIF migration is phase two.

The existing GKE enablement variables still control automatic application
deployment. Infrastructure reconciliation is independently controlled by
`ENABLE_TERRAFORM_CI`, even when automatic staging application deployment is off.
Manual staging dispatch also reconciles infrastructure and always requires `main`.
The PR plan status is only produced for Terraform-related PRs. Do not make it an
unconditional required check for all PRs; application-only PRs will not produce it.

## Retry and recovery

An optional manual staging deployment with unapplied Terraform changes is
documented in the [manual staging override plan](MANUAL_STAGING_OVERRIDE_PLAN.md).
This is proposed follow-up work, not a currently available workflow option.

- **PR plan rejected, failed, stale, or cancelled:** rerun the PR's **Deployment
  checks** workflow (all jobs) to queue a new upstream plan and approval. No PR
  number or SHA needs to be entered. A new PR commit also runs checks and queues
  a fresh approval. The PR must still be open and target `main`. If the base moved
  during approval, update the fork from `main` and rerun checks. Existing queued
  approvals cannot authorize the newer head/base revision.
- **Apply rejected, failed, or interrupted:** no successful readiness record is
  published. A marker written just before mutation remains `applying` after
  failure, including failed forced applies of unchanged inputs. Rerun the entire
  staging workflow on current `main` to generate a new plan and approval.
- **Saved plan stale or expired:** run the entire workflow again. Do not rerun
  only the apply job: a new run attempt deliberately cannot reuse another
  attempt's approval artifact. Do not regenerate a plan inside an approved job.
- **Application rollout failed after apply:** the infrastructure success remains
  recorded. A new staging run skips Terraform unless inputs or state changed.
- **Drift or emergency local apply:** coordinate with CI so no operation runs
  concurrently. Dispatch staging with `force_terraform_plan=true` for a fresh
  drift check and gated reconciliation. Out-of-band state writes invalidate
  readiness through the GCS generation check. No automatic force-unlock or
  state migration is performed.
- **Revert infrastructure:** merge a reviewed revert and approve its new plan.
  Do not apply an old saved plan or merely switch application image tags.
- **Disable automation:** setting the flag to `false` restores the rollout
  fallback, including its dependency on a current `K8S_DEPLOY_TARGETS` secret.
  Coordinate the switch with running workflows, refresh/recreate that secret,
  and verify there is no pending or failed infrastructure apply first.

## Manual plan and apply

Manual planning and applying remain supported. Use the
[manual Terraform commands](../terraform/README.md#terraform-commands) for
authentication, workspace selection, plan review, and saved-plan apply.

Speculative local plans use Terraform's state lock and can coexist with CI.
For a manual **apply** while CI is enabled, reserve a maintenance window with
other operators and pause workflow entry points first. Do not toggle
`ENABLE_TERRAFORM_CI` off just to run Terraform locally; that would restore
secret-based deployment behavior.

From the backend repository root, save the IDs of currently active deployment
and Terraform-check workflows, then disable those entry points:

```bash
export OGRRE_REPO=CATALOG-Historic-Records/orphaned-wells-ui-server
OGRRE_PAUSE_DIR="$(mktemp -d)"
gh workflow list --repo "$OGRRE_REPO" --all --limit 100 --json id,path,state \
  > "$OGRRE_PAUSE_DIR/workflows.json"
jq -r '.[] | select(.state == "active") |
  select((.path | test("/deploy-k8s-.*\\.yml$")) or
         (.path == ".github/workflows/terraform-checks.yml") or
         (.path == ".github/workflows/terraform-plan.yml")) | .id' \
  "$OGRRE_PAUSE_DIR/workflows.json" > "$OGRRE_PAUSE_DIR/paused-ids.txt"
while IFS= read -r workflow_id; do
  gh workflow disable "$workflow_id" --repo "$OGRRE_REPO" || break
done < "$OGRRE_PAUSE_DIR/paused-ids.txt"
```

Check that every listed workflow is disabled before continuing. Disabling a
workflow does **not** stop an existing run. Inspect all incomplete runs:

```bash
gh run list --repo "$OGRRE_REPO" --limit 100 \
  --json databaseId,workflowName,status,url \
  --jq '.[] | select(.status != "completed")'
```

Let running applies finish; reject approval-waiting deployment runs and cancel
queued deployment runs as appropriate. Check Actions for older pending runs if
the first 100 results are insufficient. Proceed only when none of the paused
workflows has an active, queued, or approval-waiting run, and other operators
have agreed not to apply concurrently. Keep the pause-directory path available
in the shell while following the manual Terraform commands.

After the manual apply, enable **only** the staging coordinator first and run a
new full reconciliation on `main`:

```bash
gh workflow enable deploy-k8s-staging.yml --repo "$OGRRE_REPO"
gh workflow run deploy-k8s-staging.yml --repo "$OGRRE_REPO" --ref main \
  -f force_terraform_plan=true
```

Review and approve the fresh plan. This also builds and deploys current `main`
to staging. Local state writes change its GCS generation, so CI's old readiness
record deliberately fails until reconciliation succeeds. Investigate any
unexpected changes, especially uncommitted local overrides, rather than letting
CI silently undo them.

Once staging reconciliation succeeds, re-enable only the workflows that were
active before the maintenance window:

```bash
while IFS= read -r workflow_id; do
  gh workflow enable "$workflow_id" --repo "$OGRRE_REPO" || break
done < "$OGRRE_PAUSE_DIR/paused-ids.txt"
```

Verify each saved workflow is active. If staging was disabled before the
maintenance window, disable it again after reconciliation to restore that
setting too. Do not delete the pause record until restoration is verified.
If CI has never been enabled, use the manual Terraform path and refresh the
`K8S_DEPLOY_TARGETS` fallback secret before deployment; no CI readiness record
is required in that mode.

## Local checks

```bash
python -m pytest ogrre/tests/test_terraform_ci.py ogrre/tests/test_terraform_pr_plan.py ogrre/tests/test_deployment_resources.py -q
python -m py_compile deployment/ci/*.py ogrre/tests/test_terraform_ci.py ogrre/tests/test_terraform_pr_plan.py ogrre/tests/test_deployment_resources.py
python deployment/ci/validate_manifest.py
shellcheck deployment/ci/bootstrap_terraform_ci.sh
actionlint .github/workflows/terraform-*.yml .github/workflows/deploy-k8s-*.yml
terraform -chdir=deployment/terraform fmt -check -recursive
```

Use a clean temporary copy and `terraform init -backend=false -lockfile=readonly`
followed by `terraform validate` for provider-backed configuration validation
without contacting production state. Cloud IAM, Environment approvals, token
refresh, and real GKE rollout still require the staged integration checks above.
