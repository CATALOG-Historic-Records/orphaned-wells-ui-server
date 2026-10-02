# First rollout and live verification

Run these commands yourself from the backend repository root. They change
GitHub configuration and GCP IAM when indicated. The implementation's local
checks do not replace this first live validation. Terraform manages the shared
infrastructure for all environments; the staging coordinator does not limit
the Terraform plan to staging. Use plans with no resource changes for the
initial sequencing tests.

If approved fork plans were already enabled, follow the
[migration steps](README.md#migrating-from-approved-fork-plans) first. They disable
the old workflow and revoke its WIF grant while preserving main plan/apply.
If credential-free PR checks are already in use, follow the shorter
[no-change completion upgrade](README.md#enabling-automatic-no-change-completion-on-an-existing-installation).

Keep the shell open so the exported variables and rollout snapshot remain
available. Confirm `ogrre` is the correct existing workspace and that the
deployment-account email below matches the account in the existing GitHub key.

## 1. Set the rollout context and pause automatic application deployment

```bash
cd /Users/michaelpesce/Desktop/uow/OGRRE/orphaned-wells-ui-server
export OGRRE_REPO=CATALOG-Historic-Records/orphaned-wells-ui-server
export PROJECT_ID=tidy-outlet-412020
export TF_WORKSPACE=ogrre
export TF_CI_BUCKET=ogrre-terraform-ci
export DEPLOY_SERVICE_ACCOUNT=ogrre-deployment-ci@tidy-outlet-412020.iam.gserviceaccount.com

gh auth status
OGRRE_ROLLOUT_DIR="$(mktemp -d)"
gh variable list --repo "$OGRRE_REPO" --json name,value \
  > "$OGRRE_ROLLOUT_DIR/variables-before.json"
gh variable set ENABLE_TERRAFORM_CI --repo "$OGRRE_REPO" --body false
for flag in ENABLE_GKE_DEPLOYMENTS ENABLE_GKE_STAGING_DEPLOY ENABLE_GKE_CA_DEPLOY \
  ENABLE_GKE_ISGS_DEPLOY ENABLE_GKE_NEWTS_DEPLOY ENABLE_GKE_OSAGE_DEPLOY; do
  gh variable set "$flag" --repo "$OGRRE_REPO" --body false || break
done
gh variable list --repo "$OGRRE_REPO"
```

Verify all six GKE flags are `false`. The global flag overrides individual
collaborator flags, so changing only individual flags does not pause deployment.
Coordinate a pause in manual deployment dispatches too. These changes affect
future runs, not runs already active. Let existing deployments finish before
proceeding. Keep the existing deployment and target secrets intact.

## 2. Review, merge, and distribute the implementation

Merge the full backend implementation through one reviewed PR into upstream
`main`, keeping `ENABLE_TERRAFORM_CI=false`. Include the remaining backend
documentation changes in the same PR. The Terraform plan/apply jobs stay disabled;
with the GKE flags paused in step 1, the merge also skips application deployment.
If you choose to leave GKE deployment enabled, the merge deploys staging using
the existing deployment key and `K8S_DEPLOY_TARGETS` fallback.

Continue on the existing implementation branch/PR; no separate bootstrap PR is
needed. Credential-free checks run on `pull_request`, including forks, before
the implementation lands on `main`. Satisfy normal reviews and required checks.
Remove the retired `Terraform PR plan` status from requirements if configured.

PRs run formatting and backend-free validation without a live plan or approval.
A root README-only change does not trigger Deployment checks. The first main
plan/apply test can be dispatched manually in step 6 without another code change.

Commit and submit the frontend documentation through its own reviewed PR.
Both the manual and automated Terraform guides should ship together.

After the backend merge, use a clean checkout of the updated upstream `main` for
the remaining setup. Review and merge the existing `main`-to-collaborator sync
PRs for every active deployment branch (normally `isgs`, `newts`, `osage`, and
`rrc`; include `ca` only if it is actually being deployed):

```bash
for branch in isgs newts osage rrc; do
  gh pr list --repo "$OGRRE_REPO" --base "$branch" --head main
done
```

Confirm each active branch contains the new `deploy-k8s-dispatch.yml` live-output
steps and readiness check. Old branch workflows do not honor the new CI flag.
Automatic application deployment is still paused while these PRs merge.

## 3. Prepare the local tools and confirm state

Install Terraform at the version in `deployment/terraform/.terraform-version`
(1.13.5 for this change), plus `gcloud`, `gh`, `jq`, and Python 3. If the installed Terraform
is older, this Apple Silicon macOS example places the pinned CLI in a temporary
tools directory for this shell without replacing the system installation:

```bash
OGRRE_TF_TOOLS="$(mktemp -d)"
OGRRE_TF_VERSION="$(cat deployment/terraform/.terraform-version)"
OGRRE_TF_ARCHIVE="terraform_${OGRRE_TF_VERSION}_darwin_arm64.zip"
curl -fsSL "https://releases.hashicorp.com/terraform/$OGRRE_TF_VERSION/$OGRRE_TF_ARCHIVE" \
  -o "$OGRRE_TF_TOOLS/$OGRRE_TF_ARCHIVE"
curl -fsSL "https://releases.hashicorp.com/terraform/$OGRRE_TF_VERSION/terraform_${OGRRE_TF_VERSION}_SHA256SUMS" \
  -o "$OGRRE_TF_TOOLS/SHA256SUMS"
awk -v archive="$OGRRE_TF_ARCHIVE" '$2 == archive {print}' "$OGRRE_TF_TOOLS/SHA256SUMS" \
  > "$OGRRE_TF_TOOLS/archive.sha256"
(cd "$OGRRE_TF_TOOLS" && shasum -a 256 -c archive.sha256 && unzip "$OGRRE_TF_ARCHIVE")
export PATH="$OGRRE_TF_TOOLS:$PATH"
terraform version
```

Authenticate as the human infrastructure administrator and confirm the existing
workspace. This step plans but does not apply:

```bash
unset GOOGLE_APPLICATION_CREDENTIALS GOOGLE_AUTHORIZED_USER_CREDENTIALS CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE
gcloud auth login
gcloud config set project "$PROJECT_ID"
gcloud auth application-default login
gcloud components install gke-gcloud-auth-plugin
gcloud iam service-accounts describe "$DEPLOY_SERVICE_ACCOUNT" --format='value(email)'

terraform -chdir=deployment/terraform init -input=false -lockfile=readonly
env -u TF_WORKSPACE terraform -chdir=deployment/terraform workspace select "$TF_WORKSPACE"
terraform -chdir=deployment/terraform workspace show
terraform -chdir=deployment/terraform plan -input=false -lock-timeout=5m
```

Investigate unexpected resource creation, replacement, or destruction before
continuing. Do not create a new workspace or migrate/import state as part of this
test. Ensure the clean checkout's committed defaults reproduce production;
required local overrides must be reviewed and committed into shared configuration.

## 4. Bootstrap GCP access

Review the bootstrap script and run it with the context from step 1:

```bash
bash deployment/ci/bootstrap_terraform_ci.sh
```

This creates/configures WIF, separate plan/apply accounts, IAM grants, and the
private CI bucket. It adds state/CI-bucket reads to the existing deployment
account. It does not create JSON keys or apply the application Terraform stack.
Use a dedicated CI bucket; the script configures expiration of its `plans/`
objects. If the script fails, resolve the reported permission/configuration
problem and rerun it before continuing. Allow several minutes for IAM propagation.
The updated script grants the plan account writes to only this workspace's CI
readiness record so no-change plans can complete automatically. Existing
installations must rerun bootstrap before merging the no-change workflow update.
If you already ran the earlier version, rerun this updated script to deny PR and
`workflow_run` federation and remove the former `pr-plan` impersonation grant.

## 5. Create the apply Environment and repository variables

Open the backend repository's **Settings → Environments → New environment**.
Create **terraform-apply** with the following settings:

| Setting | `terraform-apply` |
| --- | --- |
| Required reviewers | Add the people/team authorized to approve infrastructure changes |
| Prevent self-review | Your apply policy; if checked, a second reviewer must approve a run you initiate |
| Deployment branches and tags | Selected branches and tags → **Branch** `main` only |
| Allow administrators to bypass configured protection rules | **Unchecked** |

Click **Save protection rules**. Do not add fork branches or tags to the allowed
deployment refs. A `terraform-plan` Environment is no longer used.

Normal code-review approvals remain separate. In **Settings → Rules → Rulesets**,
retain the rule requiring PR approval before merging to `main`. Environment
self-review does not let an author approve their own PR for merging. No PR
produces the old `Terraform PR plan` status; remove it from required checks.

Save the protection settings. The workflow checks that required reviewers exist;
creating only the Environment name is insufficient. GitHub documents the settings
in [Managing environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments).

Populate repository **variables**, using the values created by bootstrap:

```bash
OGRRE_PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
gh variable set TF_WIF_PROVIDER --repo "$OGRRE_REPO" \
  --body "projects/$OGRRE_PROJECT_NUMBER/locations/global/workloadIdentityPools/ogrre-terraform/providers/github"
gh variable set TF_PLAN_SERVICE_ACCOUNT --repo "$OGRRE_REPO" \
  --body "github-terraform-plan@$PROJECT_ID.iam.gserviceaccount.com"
gh variable set TF_APPLY_SERVICE_ACCOUNT --repo "$OGRRE_REPO" \
  --body "github-terraform-ci@$PROJECT_ID.iam.gserviceaccount.com"
gh variable set TF_WORKSPACE --repo "$OGRRE_REPO" --body "$TF_WORKSPACE"
gh variable set TF_CI_BUCKET --repo "$OGRRE_REPO" --body "$TF_CI_BUCKET"
gh variable list --repo "$OGRRE_REPO"
gh api "repos/$OGRRE_REPO/environments/terraform-apply" \
  --jq '{name,protection_rules,deployment_branch_policy}'
```

Retain `DEPLOYMENT_SERVICE_KEY_JSON`, runtime secrets, Docker credentials, and
`K8S_DEPLOY_TARGETS` throughout initial testing.

## 6. Test no-change completion and apply approval

Enable the new flow and staging application deployment. Leave collaborator
automatic deployment paused:

```bash
gh variable set ENABLE_TERRAFORM_CI --repo "$OGRRE_REPO" --body true
gh variable set ENABLE_GKE_STAGING_DEPLOY --repo "$OGRRE_REPO" --body true
gh workflow run deploy-k8s-staging.yml --repo "$OGRRE_REPO" --ref main \
  -f force_terraform_plan=true
gh run list --repo "$OGRRE_REPO" --workflow deploy-k8s-staging.yml --branch main --limit 5
```

Open the new run in Actions. Inspect the complete Terraform plan in the summary
or the **Generate Terraform plan** step in the **infrastructure / plan** job.
For **No changes**, expect `infrastructure / noop` to verify the saved plan and
record readiness automatically. `infrastructure / apply` should be skipped,
with no Environment approval. Require readiness publication, live target reads,
staging manifest application, and rollout/image verification to succeed.

If the plan contains changes, inspect them before deciding whether to apply.
To test rejection without changing cloud resources, submit a reviewed PR adding
a temporary root output in `deployment/terraform/ci_rollout_check.tf`:

```hcl
output "ci_rollout_check" {
  value = "approval-check"
}
```

After merge, an otherwise unchanged workspace should plan only this output
addition. Output-only changes require approval even with zero resource changes.
Confirm apply is waiting and staging deployment has not started, then reject
the request and confirm neither executes. An image build/push can finish
independently. Dispatch a **new full run**, review the saved plan, and approve
`terraform-apply` through **Review deployments**. Verify apply and deployment
succeed. Remove the temporary output through another reviewed PR and approve
its output-removal plan; do not leave rollout fixtures in the configuration.

Inspect the non-secret readiness metadata and staging health:

```bash
gcloud storage cat "gs://$TF_CI_BUCKET/status/$TF_WORKSPACE.json" \
  | jq '{status,has_changes,commit,workspace,generation}'
OGRRE_STAGING_HOST="$(terraform -chdir=deployment/terraform output -json kubernetes_deploy_targets | jq -er '.staging.host')"
curl --fail --silent --show-error "https://$OGRRE_STAGING_HOST/health"
```

Expect `status: verified` and `has_changes: false` after no-change completion,
or `status: applied` after apply, plus the intended commit, workspace `ogrre`,
and a successful health response. Start a new full coordinator run after apply
or no-change failures; do not reuse an old attempt's saved-plan metadata.

## 7. Verify change detection and pending-change sequencing

Run the coordinator again without forcing a plan:

```bash
gh workflow run deploy-k8s-staging.yml --repo "$OGRRE_REPO" --ref main \
  -f force_terraform_plan=false
```

When inputs and state are unchanged, the status check runs but the Terraform
plan/apply commands are skipped; deployment still reads live outputs.

Create the test changes in your **normal fork**, and open PRs targeting upstream
`main`.

For the Terraform-comment test below:

1. Wait for the PR's **Deployment checks** workflow to pass. GitHub may first
   require approval to run ordinary fork CI according to the repository's Actions
   policy; this authorizes ordinary CI, without GCP credentials.
2. Inspect the Terraform job: `init -backend=false`, formatting, and validation
   should succeed. There must be no cloud authentication, remote workspace
   selection, cloud plan, or Environment approval.
3. Confirm completion does not trigger a **Terraform PR plan** workflow.
4. Merge through normal review. The staging coordinator now produces the live
   plan. A comment-only change should produce no changes and complete
   automatically. Plans containing changes still wait for apply approval.

To retry transient static-check failures, rerun **Deployment checks**. For code
errors, push a corrected commit. Static checks cannot predict the live plan or
verify cloud IAM; those are tested by the main workflow after merge.

| Test | Expected result |
| --- | --- |
| Add only a comment to `deployment/terraform/main.tf` in your fork | Formatting and backend-free validation pass without cloud credentials or an approval request. |
| Push another Terraform commit while checks run | The older static-check run is cancelled and checks run for the new commit. No cloud-plan workflow starts. |
| Add only a comment to `deployment/kubernetes/backend.yaml` in a separate PR | Manifest rendering/validation; no Terraform plan. |
| Change only `deployment/terraform/README.md` | No cloud plan or plan approval. |
| Change only the root README | No PR Terraform plan; after merge, current infrastructure skips plan/apply. |
| Merge the Terraform-comment PR | No-change verification succeeds, apply is skipped, and staging can deploy without approval. |
| Sync the fork's `main` with upstream, even with fork CI variables enabled | Staging jobs are skipped; no failing `infrastructure / ready` job. |
| Merge the temporary output PR and leave approval waiting | Staging waits; zero resource changes does not make an output change a no-op. |
| While that run waits, merge a README-only PR, then reject the earlier apply | The later run still plans the pending output change and requires approval. Approve its latest reviewed plan to finish. |

Avoid introducing intentional IAM failures or destructive infrastructure changes
just to exercise failure handling. Cloud-free regression tests cover failed
applies, changed-state detection, invalid plans/targets, and missing reviewers.

## 8. Validate collaborator deployment and restore the previous flags

Use the normal reviewed promotion process for the first collaborator rollout.
For an explicit dispatch, the following example performs a real **ISGS**
deployment using an image already built and tested by staging. Confirm that
environment and image are the intended rollout before running it:

```bash
gh run list --repo "$OGRRE_REPO" --workflow deploy-k8s-staging.yml --branch main \
  --status success --limit 5 --json databaseId,headSha,url
```

Choose the successful staging run whose rollout you verified, enter its numeric
run ID at the `read` command, and dispatch its SHA image tag:

```bash
read -r OGRRE_TEST_RUN_ID
OGRRE_TEST_SHA="$(gh run view "$OGRRE_TEST_RUN_ID" --repo "$OGRRE_REPO" --json headSha --jq .headSha)"
gh workflow run deploy-k8s-dispatch.yml --repo "$OGRRE_REPO" --ref main \
  -f DEPLOY_ENV=isgs -f IMAGE_TAG="$OGRRE_TEST_SHA"
```

Verify readiness, live target reads, and rollout/image consistency on that run.
Restore each application's previous automatic-deployment setting from the
snapshot, preserving variables that were originally absent:

```bash
for flag in ENABLE_GKE_DEPLOYMENTS ENABLE_GKE_STAGING_DEPLOY ENABLE_GKE_CA_DEPLOY \
  ENABLE_GKE_ISGS_DEPLOY ENABLE_GKE_NEWTS_DEPLOY ENABLE_GKE_OSAGE_DEPLOY; do
  if jq -e --arg name "$flag" '.[] | select(.name == $name)' \
    "$OGRRE_ROLLOUT_DIR/variables-before.json" > /dev/null; then
    value="$(jq -r --arg name "$flag" '.[] | select(.name == $name) | .value' "$OGRRE_ROLLOUT_DIR/variables-before.json")"
    gh variable set "$flag" --repo "$OGRRE_REPO" --body "$value" || break
  else
    gh variable delete "$flag" --repo "$OGRRE_REPO" || break
  fi
done
gh variable list --repo "$OGRRE_REPO"
```

Leave `ENABLE_TERRAFORM_CI=true`. Keep `DEPLOYMENT_SERVICE_KEY_JSON` for phase one.
The target secret is unused by the enabled path. It can remain during rollout;
remove it only after every active branch has migrated and the new flow is proven.
If you later disable Terraform CI, recreate/refresh that fallback secret first.

Manual plan/apply instructions remain in the
[Terraform guide](../terraform/README.md#terraform-commands), with exact
[CI pause/resume commands](README.md#manual-plan-and-apply).
