# Plan: manual staging deployment with unapplied Terraform changes

Status: proposed follow-up; not implemented and not required for the initial
Terraform CI rollout. No override input is available in the current workflows.

## Purpose and current behavior

Allow an authorized operator to deploy an independent backend fix to the
existing staging infrastructure after postponing or rejecting a Terraform
apply. The operator must establish that the selected application and deployment
configuration are compatible with the existing resources.

Currently, manually running **Deploy Staging Server to GKE** performs Terraform
reconciliation and requires any necessary apply approval. Manually running
**Deploy to GKE Environment** for staging skips reconciliation but still rejects
deployment when the infrastructure readiness check fails. Neither manual path
currently provides this override.

Rejecting an apply before execution leaves resources untouched by that run.
An apply that starts and then fails can leave infrastructure partially changed;
this proposal must continue to block deployment in that situation.

## Proposed operator flow

Extend the existing **Deploy to GKE Environment** manual workflow with an option
labeled **Deploy using currently applied infrastructure**, defaulting to off,
and a reason required when that option is selected. Initially allow it only for
an upstream `main` manual run targeting staging. Keep automatic staging and all
collaborator deployment behavior unchanged.

The operator selects an existing immutable image tag through `IMAGE_TAG` and
reviews its compatibility with staging. Do not accept `auto` or `latest` for
this mode. The image built by a run whose Terraform apply was rejected can be
used if its build succeeded and the application is compatible.

The existing deployment also reapplies Kubernetes manifests and secrets; it is
not an image-only update. Show the workflow/configuration commit alongside the
image, and require the operator to review those changes too. This workflow
must not build a new image, run Terraform apply, or approve a pending apply.

## Implementation boundaries

1. Add the manual inputs to `deploy-k8s-dispatch.yml`. Enforce the upstream
   repository, `workflow_dispatch` event, `main` ref, staging target, explicit
   image, and nonempty reason before accepting the override. A boolean passed
   by an automatic caller must not be enough to enable it.
2. Extend the readiness helper in `terraform_ci.py` with an explicit mode that
   permits only a Terraform input revision mismatch. Require a valid previous
   record with `status=applied`, the expected workspace/backend state path, and
   the same current GCS state generation recorded by that apply. Missing,
   malformed, or `applying` records, changed state, backend/workspace mismatches,
   and IAM/network errors must still stop deployment. Reuse these checks rather
   than skipping the entire readiness step.
3. Retain the workspace concurrency group shared by applies and deployments.
   Perform the readiness checks and state reads while holding that group, so a
   waiting override rechecks state after any preceding apply finishes. Operators
   must still coordinate Terraform changes made outside GitHub Actions.
4. Read deployment targets from that validated remote state using the existing
   backend/workspace. Do not use unapplied Terraform defaults or switch to an
   unverified state location or stale `K8S_DEPLOY_TARGETS` secret. Retain target,
   image, rollout, and health validation.
5. Record the actor, reason, image, workflow/configuration commit, pending and
   last-applied Terraform revisions, and state generation in the run summary.
   Keep existing deployment permissions; this feature grants no apply rights.
6. Leave the Terraform readiness record unchanged after an override deployment.
   Normal runs must continue to detect the outstanding infrastructure change
   and require its reconciliation. Do not toggle `ENABLE_TERRAFORM_CI` or mark
   the rejected apply successful.

A matching state generation establishes that the recorded state has not been
rewritten; it does not prove that no one changed live resources outside Terraform
or that the new application is compatible. The operator owns that review.

## Verification before enabling

| Scenario | Required result |
| --- | --- |
| Apply rejected before execution; previous applied record and state remain valid | Normal deployment stays blocked; explicit manual staging override can deploy a compatible image. |
| Previous apply started and failed, or state was changed outside the recorded apply | Override remains blocked. |
| Missing/malformed record, different backend/workspace, or cloud-read failure | Override remains blocked. |
| Automatic caller, fork run, non-main ref, collaborator target, missing reason, or implicit/mutable image selection | Override is refused. |
| Another apply or deployment is active | Preserve shared concurrency and recheck readiness before deployment. |
| Invalid targets, missing image, or failed rollout | Deployment fails through the existing checks. |
| Override succeeds while Terraform inputs remain unapplied | Backend is deployed; readiness record is unchanged and the next normal run still requires reconciliation. |
| Override is off or infrastructure is already current | Preserve existing workflow behavior. |

Add focused helper and workflow tests, then test a compatible staging image
after rejecting a no-resource-change Terraform plan. Verify the run summary,
unchanged readiness record, rollout health, and subsequent normal reconciliation.
Update the [CI guide](README.md), [rollout checklist](ROLLOUT.md), and
[Kubernetes deployment guide](../kubernetes/README.md) with actual operator
instructions when the feature is implemented. Preserve the existing
[manual Terraform plan/apply instructions](README.md#manual-plan-and-apply).
