#!/bin/bash
# Deploys the bridge to Cloud Run from source (Buildpacks, via the Procfile).
set -euo pipefail
cd "$(dirname "$0")/.."
source script/values.sh

ENV_VARS=""
for NAME in CES_DEPLOYMENT ZENDESK_SUBDOMAIN SUNCO_APP_ID SUNCO_KEY_ID SUNCO_KEY_SECRET \
  SUNCO_WEBHOOK_SECRET SWITCHBOARD_INTEGRATION_NAME ESCALATION_TARGET \
  TICKET_FIELD_ESCALATION_REASON TICKET_FIELD_CXAS_SESSION BOT_DISPLAY_NAME \
  FIRESTORE_SESSIONS_COLLECTION FIRESTORE_DATABASE_ID CES_TIMEOUT_SECONDS DEBUG; do
  VALUE="${!NAME:-}"
  if [[ -n "$VALUE" ]]; then
    ENV_VARS+="${NAME}=${VALUE}@@"
  fi
done
# The field map contains commas, so values are joined with a custom delimiter.
if [[ -n "${ESCALATION_PARAM_TICKET_FIELDS:-}" ]]; then
  ENV_VARS+="ESCALATION_PARAM_TICKET_FIELDS=${ESCALATION_PARAM_TICKET_FIELDS}@@"
fi

gcloud run deploy "$SERVICE_NAME" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --source . \
  --service-account "$SERVICE_ACCOUNT" \
  --set-env-vars "^@@^${ENV_VARS%@@}" \
  --allow-unauthenticated \
  --cpu 1 --memory 512Mi \
  --quiet
