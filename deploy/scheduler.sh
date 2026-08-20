#!/usr/bin/env bash
# Wire Cloud Scheduler -> Pub/Sub so the fleet runs without a human.
#
# This is the autonomy proof and the named Google Cloud infrastructure service
# the submission rules ask for. Run after ./deploy/setup.sh.
#
#   ./deploy/scheduler.sh PROJECT_ID [REGION] [CRON]

set -euo pipefail

PROJECT_ID="${1:-${GOOGLE_CLOUD_PROJECT:-}}"
REGION="${2:-${GOOGLE_CLOUD_LOCATION:-us-central1}}"
CRON="${3:-0 */6 * * *}"
TOPIC="${PUBSUB_TOPIC:-fluffy-memory-triggers}"
JOB="fluffy-memory-sweep"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "Usage: ./deploy/scheduler.sh PROJECT_ID [REGION] [CRON]" >&2
  exit 1
fi

PAYLOAD='{"query":"all units requiring assessment","reason":"scheduled sweep"}'

echo "==> Scheduler job ${JOB} on '${CRON}' -> topic ${TOPIC}"
if gcloud scheduler jobs describe "${JOB}" \
     --location "${REGION}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud scheduler jobs update pubsub "${JOB}" \
    --location "${REGION}" \
    --project "${PROJECT_ID}" \
    --schedule "${CRON}" \
    --topic "${TOPIC}" \
    --message-body "${PAYLOAD}"
  echo "    updated"
else
  gcloud scheduler jobs create pubsub "${JOB}" \
    --location "${REGION}" \
    --project "${PROJECT_ID}" \
    --schedule "${CRON}" \
    --topic "${TOPIC}" \
    --message-body "${PAYLOAD}" \
    --description "Autonomous fluffy-memory fleet sweep"
  echo "    created"
fi

cat <<EOF

Scheduler wired.

Fire one run immediately (useful for the demo video):

  gcloud scheduler jobs run ${JOB} --location ${REGION} --project ${PROJECT_ID}

Watch the trigger function invoke Agent Runtime:

  gcloud functions logs read fluffy-memory-trigger --gen2 --region ${REGION} --project ${PROJECT_ID} --limit 30
EOF
