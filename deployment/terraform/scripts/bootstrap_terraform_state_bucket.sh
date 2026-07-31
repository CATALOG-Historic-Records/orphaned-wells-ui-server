#!/usr/bin/env bash

set -euo pipefail
IFS=$'\n\t'

PROJECT="${PROJECT:-tidy-outlet-412020}"
BUCKET="${BUCKET:-tidy-outlet-412020-ogrre-terraform-state}"
LOCATION="${LOCATION:-us-central1}"

command -v gcloud >/dev/null 2>&1 || {
  echo "ERROR: gcloud is required to create or update the Terraform state bucket." >&2
  exit 1
}

BUCKET_URL="gs://${BUCKET}"

if gcloud storage buckets describe "${BUCKET_URL}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo "Terraform state bucket already exists: ${BUCKET_URL}"
else
  echo "Creating Terraform state bucket: ${BUCKET_URL}"
  gcloud storage buckets create "${BUCKET_URL}" \
    --project="${PROJECT}" \
    --location="${LOCATION}" \
    --uniform-bucket-level-access \
    --public-access-prevention
fi

echo "Enabling recommended state bucket settings..."
gcloud storage buckets update "${BUCKET_URL}" \
  --project="${PROJECT}" \
  --uniform-bucket-level-access \
  --public-access-prevention \
  --versioning

echo "Terraform state bucket is ready: ${BUCKET_URL}"
