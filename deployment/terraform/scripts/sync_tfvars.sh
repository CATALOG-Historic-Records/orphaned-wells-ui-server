#!/usr/bin/env bash

set -euo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_DIR="$(cd "${TF_DIR}/../.." && pwd)"
BACKEND_TF="${TF_DIR}/backend.tf"
TARGET_FILE="${TF_DIR}/terraform.tfvars"

# Extract bucket from backend.tf if not provided in environment
BUCKET="${BUCKET:-}"
if [ -z "${BUCKET}" ] && [ -f "${BACKEND_TF}" ]; then
  BUCKET="$(grep -E '^\s*bucket\s*=' "${BACKEND_TF}" | head -n1 | sed -E 's/.*"([^"]+)".*/\1/' | tr -d '[:space:]')"
fi
BUCKET="${BUCKET:-tidy-outlet-412020-ogrre-terraform-state}"
GCS_PATH="gs://${BUCKET}/config/terraform.tfvars"
GH_REPO="${GH_REPO:-CATALOG-Historic-Records/orphaned-wells-ui-server}"

usage() {
  local exit_code="${1:-1}"
  cat <<EOF
Usage: $(basename "$0") <pull|push|diff|sync-secret> [options]

Manage and sync the shared terraform.tfvars file stored in Google Cloud Storage.

Commands:
  pull           Download terraform.tfvars from GCS (${GCS_PATH}) to local directory.
  push [options] Upload local terraform.tfvars to GCS (${GCS_PATH}).
  diff           Compare local terraform.tfvars with remote GCS version.
  sync-secret    Stream GCS terraform.tfvars directly into the GitHub Secret PROD_TERRAFORM_TFVARS.

Options for push:
  --secret, -s   Also update the PROD_TERRAFORM_TFVARS secret in GitHub via gh CLI.

General Options:
  --help, -h     Show this help message.

Environment Overrides:
  BUCKET         Override GCS bucket name (defaults to bucket declared in backend.tf: ${BUCKET})
  GH_REPO        Override GitHub repository (defaults to CATALOG-Historic-Records/orphaned-wells-ui-server)
EOF
  exit "$exit_code"
}

require_gcloud() {
  command -v gcloud >/dev/null 2>&1 || {
    echo "ERROR: gcloud CLI is required to interact with Google Cloud Storage." >&2
    exit 1
  }
}

pull_tfvars() {
  require_gcloud
  echo "Pulling terraform.tfvars from ${GCS_PATH}..."
  if [ -f "${TARGET_FILE}" ]; then
    cp "${TARGET_FILE}" "${TARGET_FILE}.bak"
    echo "Backed up existing local file to ${TARGET_FILE}.bak"
  fi

  if ! gcloud storage cp "${GCS_PATH}" "${TARGET_FILE}"; then
    echo "ERROR: Failed to download ${GCS_PATH}." >&2
    exit 1
  fi
  echo "Successfully pulled terraform.tfvars to ${TARGET_FILE}"
}

push_tfvars() {
  local update_secret=false
  while [ $# -gt 0 ]; do
    case "$1" in
      --secret|-s)
        update_secret=true
        shift
        ;;
      *)
        echo "Unknown option for push: $1" >&2
        usage
        ;;
    esac
  done

  if [ ! -s "${TARGET_FILE}" ]; then
    echo "ERROR: Local file ${TARGET_FILE} does not exist or is empty." >&2
    exit 1
  fi

  if command -v terraform >/dev/null 2>&1; then
    echo "Formatting ${TARGET_FILE} with terraform fmt..."
    terraform fmt "${TARGET_FILE}" >/dev/null 2>&1 || true
  fi

  require_gcloud
  echo "Uploading ${TARGET_FILE} to ${GCS_PATH}..."
  gcloud storage cp "${TARGET_FILE}" "${GCS_PATH}"
  echo "Successfully uploaded terraform.tfvars to ${GCS_PATH}"

  if [ "$update_secret" = true ]; then
    command -v gh >/dev/null 2>&1 || {
      echo "ERROR: gh CLI is required to update GitHub Secrets." >&2
      exit 1
    }
    echo "Updating GitHub secret PROD_TERRAFORM_TFVARS..."
    gh secret set PROD_TERRAFORM_TFVARS \
      --repo "${GH_REPO}" \
      --body "$(<"${TARGET_FILE}")"
    echo "Successfully updated GitHub secret PROD_TERRAFORM_TFVARS"
  fi
}

diff_tfvars() {
  require_gcloud
  if [ ! -f "${TARGET_FILE}" ]; then
    echo "ERROR: Local file ${TARGET_FILE} does not exist." >&2
    exit 1
  fi

  TMP_REMOTE="$(mktemp -t remote_terraform_tfvars.XXXXXX)"
  trap 'rm -f "${TMP_REMOTE}"' EXIT

  if ! gcloud storage cp "${GCS_PATH}" "${TMP_REMOTE}"; then
    echo "ERROR: Unable to download remote file ${GCS_PATH} for diff." >&2
    exit 1
  fi

  if diff -u "${TMP_REMOTE}" "${TARGET_FILE}" >/dev/null 2>&1; then
    echo "Local terraform.tfvars is identical to remote ${GCS_PATH}"
  else
    echo "Differences between remote (---) and local (+++):"
    diff -u --color=auto "${TMP_REMOTE}" "${TARGET_FILE}" || true
  fi
}

sync_secret() {
  require_gcloud
  command -v gh >/dev/null 2>&1 || {
    echo "ERROR: gh CLI is required to update GitHub Secrets." >&2
    exit 1
  }

  TMP_REMOTE="$(mktemp -t remote_terraform_tfvars.XXXXXX)"
  trap 'rm -f "${TMP_REMOTE}"' EXIT

  echo "Fetching terraform.tfvars from ${GCS_PATH}..."
  if ! gcloud storage cp "${GCS_PATH}" "${TMP_REMOTE}"; then
    echo "ERROR: Unable to download remote file ${GCS_PATH}." >&2
    exit 1
  fi

  if command -v terraform >/dev/null 2>&1; then
    echo "Ensuring canonical formatting with terraform fmt..."
    terraform fmt "${TMP_REMOTE}" >/dev/null 2>&1 || true
  fi

  echo "Updating GitHub secret PROD_TERRAFORM_TFVARS in repo ${GH_REPO}..."
  gh secret set PROD_TERRAFORM_TFVARS --repo "${GH_REPO}" < "${TMP_REMOTE}"
  echo "Successfully updated GitHub secret PROD_TERRAFORM_TFVARS."
}

main() {
  if [ $# -eq 0 ]; then
    usage
  fi

  case "$1" in
    pull)
      pull_tfvars
      ;;
    push)
      shift
      push_tfvars "$@"
      ;;
    diff)
      diff_tfvars
      ;;
    sync-secret)
      sync_secret
      ;;
    -h|--help|help)
      usage 0
      ;;
    *)
      echo "Unknown command: $1" >&2
      usage
      ;;
  esac
}

main "$@"
