#!/opt/homebrew/bin/bash

set -euo pipefail
IFS=$'\n\t'

unset GOOGLE_APPLICATION_CREDENTIALS
unset GOOGLE_AUTHORIZED_USER_CREDENTIALS
unset CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE

PROJECT="tidy-outlet-412020"

declare -A ZONES=(
  ["isgs"]="us-central1-a"
  ["osage"]="us-central1-f"
  ["ca"]="us-central1-f"
  ["newts"]="us-central1-b"
  ["staging"]="us-central1-a"
)

COLLABORATORS_DEFAULT=(
  "isgs"
  "osage"
  "ca"
  "newts"
  "staging"
)

# Optional input: collaborator name (e.g. ./import.sh staging)
COLLABORATOR_INPUT="${1-}"

if [[ -n "${COLLABORATOR_INPUT}" ]]; then
  # (Assuming input is valid per your note, but still guard with a helpful message.)
  if [[ -z "${ZONES[$COLLABORATOR_INPUT]+x}" ]]; then
    echo "❌ Unknown collaborator: '${COLLABORATOR_INPUT}'. Available: ${!ZONES[*]}"
    exit 1
  fi
  COLLABORATORS=("${COLLABORATOR_INPUT}")
else
  COLLABORATORS=("${COLLABORATORS_DEFAULT[@]}")
fi

echo "Starting Terraform imports (zone-aware)..."
echo "Collaborators: ${COLLABORATORS[*]}"

for c in "${COLLABORATORS[@]}"; do
  ZONE="${ZONES[$c]:-}"
  if [[ -z "$ZONE" ]]; then
    echo "❌ No zone defined for collaborator: $c"
    continue
  fi

  echo ""
  echo "=============================="
  echo "Importing: $c (zone: $ZONE)"
  echo "=============================="

  INSTANCE_NAME="${c}-uow-server"
  INSTANCE_ID="projects/${PROJECT}/zones/${ZONE}/instances/${INSTANCE_NAME}"

  IP_ID="projects/${PROJECT}/regions/us-central1/addresses/${c}-static-ip-address"

  DNS_ID="uow-carbon-org/${c}-server.uow-carbon.org./A"

  #
  # 1. Compute Instance
  #
  echo "Importing compute instance..."
  terraform import \
    "module.backend_vms[\"$c\"].google_compute_instance.vm" \
    "$INSTANCE_ID" \
    || echo "⚠️  Instance import failed for $c"

  #
  # 2. Static IP
  #
  echo "Importing static IP..."
  terraform import \
    "module.backend_vms[\"$c\"].google_compute_address.ip" \
    "$IP_ID" \
    || echo "⚠️  IP import failed for $c"

  #
  # 3. DNS record
  #
  echo "Importing DNS record..."
  terraform import \
    "module.backend_vms[\"$c\"].google_dns_record_set.dns" \
    "$DNS_ID" \
    || echo "⚠️  DNS import failed for $c"

done

echo ""
echo "✅ All imports attempted."