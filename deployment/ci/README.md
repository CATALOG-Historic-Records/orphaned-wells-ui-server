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
  Forks use the same `pull_request` checks, with a read-only repository token,
  no OIDC permission, no cloud secrets, and no remote state. There is no PR plan
  Environment, cloud plan, or approval request. Validation checks configuration
  consistency; it cannot predict live resource changes or test cloud permissions.
- `deploy-k8s-staging.yml` coordinates upstream `main` pushes and manual dispatches.
  Fork pushes skip the staging jobs, even when the fork has CI variables enabled.
  It builds the commit's image and calls `terraform-apply.yml`. The latter checks
  the last successful reconciliation against the tracked Terraform inputs and the remote
  state object's generation. When they match, no plan or apply is needed.
- When reconciliation is needed, CI saves a fresh plan for that merged commit
  and publishes its readable summary. Terraform's detailed exit code determines
  the next job: `0` means no changes, `2` means changes, and errors stop the run.
- A no-change plan runs `infrastructure / noop` automatically. Using the plan
  account, it verifies the saved plan checksum, commit, workspace, run/attempt,
  no-change outcome, and unchanged state generation. It checks live outputs and
  current `main` inputs, then publishes a `verified` readiness record. It never
  runs `terraform apply` or writes Terraform state.
- A plan with changes, including output-only changes, waits at the
  `terraform-apply` Environment. Apply verifies that exact saved plan and state,
  authenticates afresh, and refuses inputs superseded by a newer `main` commit.
- Only successful reconciliation releases staging deployment. Deployments read
  live targets and preserve the existing immutable image-tag behavior.

The no-change completion job, apply job, and every updated deployment share a concurrency group per
workspace with cancellation disabled. The staging orchestrator has a separate
group, avoiding nested-workflow deadlocks. GitHub may replace a pending run with
a newer one; comparing against the last reconciled inputs, rather than only the
latest push diff, means a later backend-only commit still reconciles any pending
infrastructure. Failed or rejected applies never release staging deployment.

Collaborator workflows retain their existing branch-promotion behavior. PRs
targeting collaborator branches do not run Terraform PR checks, and pushes or
merges to those branches do not start a Terraform plan or apply. Infrastructure
changes must reach `main` to be reconciled. Their shared deploy workflow checks
the current `main` infrastructure revision before
mutation. If it is pending or failed, deployment stops with a retry instruction;
it does not silently fall back to the secret. Retry after infrastructure reconciliation succeeds.
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
   export TF_CI_BUCKET=ogrre-terraform-ci
   export DEPLOY_SERVICE_ACCOUNT=ogrre-deployment-ci@tidy-outlet-412020.iam.gserviceaccount.com
   bash deployment/ci/bootstrap_terraform_ci.sh
   ```

   Bootstrap requires IAM policy administration, service-account and WIF
   administration, service activation, and bucket administration. These are
   operator permissions, not additional permissions for the CI plan account.

4. Create the GitHub apply Environment before enabling CI:

   | Environment | Required reviewers | Prevent self-review |
   | --- | --- | --- |
   | `terraform-apply` | People/team authorized to approve infrastructure changes | Independent apply policy; if checked, another reviewer must approve runs you initiate |

   Restrict deployment branches to the **branch** `main` and disable
   administrator bypass. Do not allow fork branches or tags to request this identity.
   Keep the normal PR approval requirement on `main`. Environment self-approval
   does not allow an author to approve their own PR for merging.

   Merely referencing the Environment in YAML creates it without approval rules.
   CI checks for required reviewers before main plan/apply. Check the branch and bypass
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
Only upstream `terraform-apply.yml@refs/heads/main`, called on `main` by a `push`
or `workflow_dispatch` run, can authenticate. Its normal plan job receives the
`main-plan` identity; its `terraform-apply` Environment job receives the apply
identity. PR and `workflow_run` events are rejected by the provider, even if a
PR changes YAML permissions to request an OIDC token. Updated bootstrap also
removes the former `pr-plan` impersonation grant when it exists.

- **Plan account:** Compute, GKE, DNS, project-service and bucket-metadata reads;
  state object reads; creation/deletion of this workspace's `.tflock` object.
  No-change completion can also replace only `status/<workspace>.json` in the CI
  bucket. This conditional grant cannot write other workspaces' readiness records.
  Bootstrap grants no Terraform state or managed-infrastructure writes or
  executable-plan publication to this account.
- **Apply account:** `container.admin`, `compute.networkAdmin`, `dns.admin`,
  `storage.admin`, and `serviceusage.serviceUsageAdmin`, matching the current GKE
  stack. Bootstrap grants no account-management or WIF-administration roles.
  If legacy VM management is deliberately enabled later, review the additional
  instance and VM-service-account permissions before planning that change.
- **Main plan publisher:** direct federation, without account impersonation,
  can create objects under `plans/` only. PR identities cannot publish executable
  plans. Existing objects cannot be overwritten. Apply checks the plan checksum,
  commit, workspace, state path, and run/attempt before use.
  The upload helper uses the GCS object-insert API with `ifGenerationMatch=0`.
  This supports the existing create-only grant without the destination get/list
  permissions required by `gcloud storage cp`.
- **Existing deploy account:** keeps its deployment key and workload permissions;
  bootstrap adds object reads on the state and CI buckets. No apply role is added.

State access exposes the complete Terraform state, not just outputs. Plan files
can also contain sensitive values. Binary plans and their metadata remain in the
private CI bucket; only Terraform's human-readable, sensitive-value-redacted
plan output is published in GitHub summaries. Do not upload raw state or binary
plans as public workflow artifacts.

### Why PR checks do not use cloud credentials

`terraform plan` normally refreshes resource information and proposes changes;
it does not apply those proposed changes. It also acquires/releases the state
lock. Planning is not a sandbox: providers are executable programs, and data
sources such as `external` can run programs during refresh. Those programs can
access the job's credentials, files, network, and readable Terraform state.
See HashiCorp's [plan behavior](https://developer.hashicorp.com/terraform/cli/commands/plan)
and [external data source](https://registry.terraform.io/providers/hashicorp/external/latest/docs/data-sources/external).

PR checks therefore receive no real state or cloud identity. Provider schema
validation can execute code, but it runs on an isolated GitHub-hosted runner with
no production credentials. There is no privileged follow-up workflow executing
the PR, and no checkout protection opt-in.

Live planning starts only from reviewed `main`. Protect merges into `main` and
review provider/module sources, lockfile/CLI changes, and executable programs.
Code merged into `main` is trusted with planning access before apply approval;
moving planning after merge does not make malicious merged code safe. The plan
account remains restricted to reads, lock management, and its workspace's CI
readiness record, while apply uses a
separate account and approval. Sensitive-value redaction in normal plan output
does not prevent malicious code from printing secrets.

Terraform uses `deployment/terraform/.terraform-version` (currently 1.13.5) and
the checked-in provider lockfile. Follow
[provider lockfile maintenance](../terraform/README.md#provider-lockfile-maintenance)
when updating providers so read-only initialization works on Linux and macOS.
Local operators must also install
`gke-gcloud-auth-plugin` (`gcloud components install gke-gcloud-auth-plugin`) and
authenticate with ADC. The Kubernetes provider invokes the plugin separately
for planning/applying instead of saving a short-lived token in the plan.
The deployment output reader copies both `backend.tf` and the provider lockfile
into its temporary directory. Terraform initialization discovers provider
dependencies in existing state even without resource configuration, so output
mode installs those pinned providers before reading outputs.
Setup keeps `TF_WORKSPACE` for initialization and later commands, but unsets it
for the explicit `workspace select` command, which rejects an environment override.
Selection requires an existing workspace; it never uses `workspace new`.

## Enable and verify

1. With the flag disabled, merge the implementation into `main` through
   one reviewed PR. Static checks require no cloud bootstrap. For an existing
   installation with approved fork plans, complete the migration below instead.
2. Propagate the updated reusable deployment workflow to every enabled
   collaborator branch **before** turning the flag on. Old branch workflows do
   not understand the readiness gate. Keep automatic collaborator promotion paused
   during this rollout using their existing deployment enablement variables.
3. Complete bootstrap, variable setup, and apply Environment protection. Preserve the
   existing secrets. Allow time for GCP IAM propagation.
4. Set `ENABLE_TERRAFORM_CI=true` and manually dispatch **Deploy Staging Server to
   GKE** on `main`. An absent readiness record requires a fresh plan. A no-change
   plan completes automatically; a plan with changes requires apply approval.
5. Verify no-change completion, approved apply, live target reads, and staging rollout. Test a Terraform PR from
   your normal fork: static checks pass without cloud authentication or a plan
   approval. Test a Kubernetes-only PR, a rejected main apply, a backend-only
   follow-up, and a collaborator deployment.
6. Restore collaborator promotion settings. Delete `K8S_DEPLOY_TARGETS` only after
   all active branches use the enabled path and the rollout is confirmed. Keep
   `DEPLOYMENT_SERVICE_KEY_JSON`; deployment WIF migration is phase two.

The existing GKE enablement variables still control automatic application
deployment. Infrastructure reconciliation is independently controlled by
`ENABLE_TERRAFORM_CI`, even when automatic staging application deployment is off.
Manual staging dispatch also reconciles infrastructure and always requires `main`.
The retired `Terraform PR plan` status is no longer produced. Remove it from
required checks if previously configured. Keep normal PR review requirements.

## Enabling automatic no-change completion on an existing installation

Before merging this update, rerun the updated
`deployment/ci/bootstrap_terraform_ci.sh` as the human administrator, with the
existing project, workspace, deployment account, and CI bucket (`ogrre-terraform-ci`
for the current installation). It adds a condition restricting the plan account's
readiness writes to exactly `status/ogrre.json` in that bucket. It does not grant
Terraform state or infrastructure writes. Stop any older bootstrap process before
running the updated file; do not edit a script while it is running.

No new repository variables, JSON keys, WIF mappings, or GitHub Environments are
needed. Keep `terraform-apply` protected. Merge the update, then dispatch a **new**
staging run with `force_terraform_plan=true`. For a no-change plan, expect `noop`
to succeed, `apply` to be skipped, and `status: verified` in the readiness record.
Do not rerun old attempts: saved-plan metadata now also binds the outcome and
state generation. Existing `status: applied` records remain valid.

If no-change completion fails to write the readiness record, confirm bootstrap
finished successfully and allow IAM propagation, then start a new full run.
It must not silently fall back to an unapproved apply.

## Migrating from approved fork plans

If the former `terraform-plan.yml` workflow was already enabled, complete these
steps using the updated files in the current reviewed PR. Fresh installations
can skip this section.

1. Disable the old workflow upstream before pushing more PR updates:

   ```bash
   export OGRRE_REPO=CATALOG-Historic-Records/orphaned-wells-ui-server
   gh workflow disable terraform-plan.yml --repo "$OGRRE_REPO"
   gh run list --repo "$OGRRE_REPO" --workflow terraform-plan.yml --limit 100 \
     --json databaseId,status,url --jq '.[] | select(.status != "completed")'
   ```

   Cancel any listed runs with `gh run cancel RUN_ID --repo "$OGRRE_REPO"`,
   including approval-waiting runs, and check Actions for older pending runs.
   Disabling a workflow stops new triggers; it does not stop existing runs.
   Keep `terraform-apply` and the staging coordinator enabled as appropriate.
2. Rerun **the updated bootstrap script** from this PR as the human administrator.
   Reuse the existing project, workspace, bucket, and deployment account. For
   the current installation:

   ```bash
   export PROJECT_ID=tidy-outlet-412020
   export TF_WORKSPACE=ogrre
   export TF_CI_BUCKET=ogrre-terraform-ci
   export DEPLOY_SERVICE_ACCOUNT=ogrre-deployment-ci@tidy-outlet-412020.iam.gserviceaccount.com
   bash deployment/ci/bootstrap_terraform_ci.sh
   ```

   This removes PR/`workflow_run` access from the WIF provider and removes the
   previous `pr-plan` impersonation grant. Main plan/apply grants and repository
   variables retain their values. Allow IAM changes to propagate. Previously
   issued service-account access tokens can remain usable until they expire;
   cancelling a run is not token revocation.
3. Push and merge the current PR after **Deployment checks** and normal reviews
   pass. If `Terraform PR plan` was configured as a required status, remove that
   obsolete requirement first. Its old failure remains historical; do not
   approve or rerun it. The workflow, helper, and old status publisher are removed.
4. The unused `terraform-plan` Environment may then be deleted in Settings.
   Keep the protected `terraform-apply` Environment and all main CI variables.
5. Start a new staging run on current `main` with `force_terraform_plan=true`.
   Review its plan. No-change completion is automatic; plans with changes require
   apply approval. Test a new fork PR: **Deployment checks** should run without a cloud
   plan or Environment approval. The remaining first-rollout checks are in
   [ROLLOUT.md](ROLLOUT.md#6-test-no-change-completion-and-apply-approval).

Until this migration is complete, an old workflow on upstream `main` can still
queue from PR checks, regardless of changes made only in the fork. Existing
runs retain their original workflow revision.

## Retry and recovery

An optional manual staging deployment with unapplied Terraform changes is
documented in the [manual staging override plan](MANUAL_STAGING_OVERRIDE_PLAN.md).
This is proposed follow-up work, not a currently available workflow option.

- **Bootstrap asks for an IAM condition:** older scripts omitted `--condition=None`
  on unconditional read/impersonation grants. Existing conditional policies then
  cause `gcloud` to prompt on reruns. Cancel with Ctrl-C and rerun the updated
  bootstrap with the same inputs. It explicitly specifies every grant's condition
  and preserves the restrictions on lock writes and plan publication. Wait for
  `Bootstrap complete` before proceeding.
- **First run with no readiness record:** a missing `status/<workspace>.json`
  means reconciliation is needed. CI generates a plan; successful no-change
  verification or approved apply creates the record. Do not create it manually.
  Permission, authentication, network,
  and malformed-record errors still stop the workflow; readiness-read errors
  include the underlying `gcloud` message for diagnosis.
- **PR checks fail:** fix formatting/configuration and push an updated commit.
  For a transient failure, rerun **Deployment checks**. No cloud-plan approval
  is involved. Historical failures from the retired workflow are not rerun.
- **Apply rejected, failed, or interrupted:** no successful readiness record is
  published. A marker written just before mutation remains `applying` after
  failure, including failed forced applies of unchanged inputs. Rerun the entire
  staging workflow on current `main` to generate a new plan. Approve it if changes remain.
- **Saved plan stale or expired:** run the entire workflow again. Do not rerun
  only the apply or no-change job: a new run attempt deliberately cannot reuse another
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
workflows, then disable those entry points. Credential-free PR checks can continue:

```bash
export OGRRE_REPO=CATALOG-Historic-Records/orphaned-wells-ui-server
OGRRE_PAUSE_DIR="$(mktemp -d)"
gh workflow list --repo "$OGRRE_REPO" --all --limit 100 --json id,path,state \
  > "$OGRRE_PAUSE_DIR/workflows.json"
jq -r '.[] | select(.state == "active") |
  select(.path | test("/deploy-k8s-.*\\.yml$")) | .id' \
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

Review the fresh plan; approve apply only if it contains changes. A no-change
plan restores readiness automatically. This also builds and deploys current `main`
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
python -m pytest ogrre/tests/test_terraform_ci.py ogrre/tests/test_terraform_noop.py ogrre/tests/test_terraform_pr_checks.py ogrre/tests/test_terraform_setup.py ogrre/tests/test_deployment_resources.py -q
python -m py_compile deployment/ci/*.py ogrre/tests/test_terraform_ci.py ogrre/tests/test_terraform_noop.py ogrre/tests/test_terraform_pr_checks.py ogrre/tests/test_terraform_setup.py ogrre/tests/test_deployment_resources.py
python deployment/ci/validate_manifest.py
shellcheck deployment/ci/bootstrap_terraform_ci.sh
actionlint .github/workflows/terraform-*.yml .github/workflows/deploy-k8s-*.yml
terraform -chdir=deployment/terraform fmt -check -recursive
```

Put the pinned Terraform CLI on `PATH` to include the offline setup regressions.
They verify validation without cloud backend initialization, and remote/output
setup with synthetic local state and a local provider fixture. The state remains
unchanged. Without the CLI, those tests are skipped. Bootstrap migration tests
use fake cloud CLIs; none of these tests needs cloud credentials or network access.

Use a clean temporary copy and `terraform init -backend=false -lockfile=readonly`
followed by `terraform validate` for provider-backed configuration validation
without contacting production state. Cloud IAM, Environment approvals, token
refresh, and real GKE rollout still require the staged integration checks above.
