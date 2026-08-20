#!/usr/bin/env bash
# One-time Google Cloud setup for fluffy-memory.
#
# Idempotent: safe to re-run. Every step is skipped if the resource exists.
#
#   ./deploy/setup.sh YOUR_PROJECT_ID [REGION]
#
# Requires: gcloud authenticated, billing enabled on the project.

set -euo pipefail

PROJECT_ID="${1:-${GOOGLE_CLOUD_PROJECT:-}}"
REGION="${2:-${GOOGLE_CLOUD_LOCATION:-us-central1}}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "Usage: ./deploy/setup.sh PROJECT_ID [REGION]" >&2
  exit 1
fi

BUCKET="${PROJECT_ID}-fluffy-memory-staging"
TEMPLATE_ID="${MODEL_ARMOR_TEMPLATE_ID:-fluffy-memory-fleet-filter}"
TOPIC="${PUBSUB_TOPIC:-fluffy-memory-triggers}"

echo "==> Project: ${PROJECT_ID}   Region: ${REGION}"
gcloud config set project "${PROJECT_ID}" --quiet

echo "==> Enabling APIs (this can take a couple of minutes)"
gcloud services enable \
  aiplatform.googleapis.com \
  storage.googleapis.com \
  cloudtrace.googleapis.com \
  logging.googleapis.com \
  telemetry.googleapis.com \
  modelarmor.googleapis.com \
  pubsub.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudfunctions.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  eventarc.googleapis.com \
  --project "${PROJECT_ID}"

echo "==> Staging bucket: gs://${BUCKET}"
if gcloud storage buckets describe "gs://${BUCKET}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "    already exists, skipping"
else
  gcloud storage buckets create "gs://${BUCKET}" \
    --project "${PROJECT_ID}" --location "${REGION}" --uniform-bucket-level-access
fi

echo "==> Model Armor template: ${TEMPLATE_ID}"
if gcloud model-armor templates describe "${TEMPLATE_ID}" \
     --location "${REGION}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "    already exists, skipping"
else
  gcloud model-armor templates create "${TEMPLATE_ID}" \
    --location "${REGION}" \
    --project "${PROJECT_ID}" \
    --pi-and-jailbreak-filter-settings-enforcement=enabled \
    --pi-and-jailbreak-filter-settings-confidence-level=LOW_AND_ABOVE \
    --malicious-uri-filter-settings-enforcement=enabled \
    --basic-config-filter-enforcement=enabled \
    --template-metadata-log-sanitize-operations
fi

echo "==> Model Armor floor settings (project-level baseline)"
# Floor settings work at project scope and need no Cloud Organization, so this
# holds even on a personal account where org-level policy is unavailable.
gcloud model-armor floor-settings update \
  --full-uri="projects/${PROJECT_ID}/locations/global/floorSetting" \
  --pi-and-jailbreak-filter-settings-enforcement=enabled \
  --pi-and-jailbreak-filter-settings-confidence-level=LOW_AND_ABOVE \
  || echo "    WARNING: floor settings update failed (non-fatal; template still enforces)"

echo "==> Pub/Sub topic: ${TOPIC}"
if gcloud pubsub topics describe "${TOPIC}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "    already exists, skipping"
else
  gcloud pubsub topics create "${TOPIC}" --project "${PROJECT_ID}"
fi

echo "==> Application default credentials"
if gcloud auth application-default print-access-token >/dev/null 2>&1; then
  echo "    already present"
else
  echo "    run: gcloud auth application-default login"
fi

cat <<EOF

Setup complete.

Write these into your .env:

  GOOGLE_CLOUD_PROJECT=${PROJECT_ID}
  GOOGLE_CLOUD_LOCATION=${REGION}
  STAGING_BUCKET=${BUCKET}
  MODEL_ARMOR_TEMPLATE_ID=${TEMPLATE_ID}
  PUBSUB_TOPIC=${TOPIC}

Next: make smoke   (verifies Agent Runtime works on this account, then cleans up)
EOF
