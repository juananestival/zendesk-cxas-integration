#!/bin/bash
# One-time Google Cloud setup for the bridge. Run in Cloud Shell from bridge/:
#
#   ./script/setup_gcp.sh
#
# It reads script/values.sh, then:
# - enables the APIs;
# - creates the service account and grants it CES client, Secret Manager and
#   Firestore access;
# - creates the two Zendesk secrets (it prompts for their values, so they never
#   land in shell history);
# - creates a Firestore database with TTL policies.
# It is safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."
source script/values.sh

DB="${FIRESTORE_DATABASE_ID:-(default)}"
SA_NAME="${SERVICE_ACCOUNT%%@*}"
gcloud config set project "$PROJECT_ID" >/dev/null

echo "Enabling APIs..."
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  firestore.googleapis.com ces.googleapis.com

if ! gcloud iam service-accounts describe "$SERVICE_ACCOUNT" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" --display-name "Zendesk CXAS bridge"
fi

# roles/ces.client grants sessions.runSession and nothing else.
for ROLE in roles/ces.client roles/datastore.user; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:$SERVICE_ACCOUNT" --role "$ROLE" --condition None >/dev/null
  echo "Granted $ROLE"
done

create_secret() {
  local secret_name="$1" prompt="$2" value
  if gcloud secrets describe "$secret_name" >/dev/null 2>&1; then
    echo "Secret $secret_name exists; leaving it."
  else
    read -r -s -p "$prompt: " value; echo
    printf '%s' "$value" | gcloud secrets create "$secret_name" --data-file=- >/dev/null
    echo "Created secret $secret_name"
  fi
  gcloud secrets add-iam-policy-binding "$secret_name" \
    --member "serviceAccount:$SERVICE_ACCOUNT" --role roles/secretmanager.secretAccessor >/dev/null
}
# Secret names are taken from the Secret Manager paths in values.sh.
create_secret "$(echo "$SUNCO_KEY_SECRET" | cut -d/ -f4)" "Conversations API key secret"
create_secret "$(echo "$SUNCO_WEBHOOK_SECRET" | cut -d/ -f4)" "Conversations webhook secret"

if ! gcloud firestore databases describe --database="$DB" >/dev/null 2>&1; then
  gcloud firestore databases create --database="$DB" --location="$REGION" --type=firestore-native
fi
for GROUP in "$FIRESTORE_SESSIONS_COLLECTION" "${FIRESTORE_SESSIONS_COLLECTION}_events"; do
  gcloud firestore fields ttls update expiry_time --collection-group="$GROUP" \
    --database="$DB" --enable-ttl --async >/dev/null || true
  echo "TTL policy requested on $GROUP.expiry_time"
done

echo "Done. Next: ./script/deploy.sh"
