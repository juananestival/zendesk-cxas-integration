#!/bin/bash
# Makes the CXAS bridge the first responder on Zendesk messaging and points its
# escalation at the Agent Workspace. Run once, after creating the
# Conversations integration in Admin Center (which gives the webhook secret).
#
#   SUNCO_KEY_SECRET_VALUE=... ./script/setup_switchboard.sh
#
# Reads ZENDESK_SUBDOMAIN, SUNCO_APP_ID, SUNCO_KEY_ID and INTEGRATION_ID from
# script/values.sh when present (environment variables win), plus a plain-text
# SUNCO_KEY_SECRET_VALUE from the environment. Requires curl and jq.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f script/values.sh ]]; then
  _ENV_KEY_ID="${SUNCO_KEY_ID:-}" _ENV_INTEGRATION_ID="${INTEGRATION_ID:-}"
  source script/values.sh
  SUNCO_KEY_ID="${_ENV_KEY_ID:-$SUNCO_KEY_ID}"
  INTEGRATION_ID="${_ENV_INTEGRATION_ID:-$INTEGRATION_ID}"
fi

: "${ZENDESK_SUBDOMAIN:?}" "${SUNCO_APP_ID:?}" "${SUNCO_KEY_ID:?}" "${SUNCO_KEY_SECRET_VALUE:?}" "${INTEGRATION_ID:?}"
NAME="${SWITCHBOARD_INTEGRATION_NAME:-cxas-bot}"
API="https://${ZENDESK_SUBDOMAIN}.zendesk.com/sc/v2/apps/${SUNCO_APP_ID}"

call() {
  curl -sS --fail-with-body -u "${SUNCO_KEY_ID}:${SUNCO_KEY_SECRET_VALUE}" \
    -H 'content-type: application/json' "$@"
}

SWITCHBOARD_ID=$(call "$API/switchboards" | jq -r '.switchboards[0].id')
echo "Switchboard: $SWITCHBOARD_ID"

INTEGRATIONS=$(call "$API/switchboards/$SWITCHBOARD_ID/switchboardIntegrations")
AGENT_WORKSPACE_ID=$(echo "$INTEGRATIONS" | jq -r '.switchboardIntegrations[] | select(.name=="zd-agentWorkspace") | .id')
EXISTING_ID=$(echo "$INTEGRATIONS" | jq -r --arg n "$NAME" '.switchboardIntegrations[] | select(.name==$n) | .id')
echo "Agent Workspace switchboard integration: $AGENT_WORKSPACE_ID"

if [[ -z "$EXISTING_ID" ]]; then
  BOT_ID=$(call -X POST "$API/switchboards/$SWITCHBOARD_ID/switchboardIntegrations" \
    -d "{\"name\":\"$NAME\",\"integrationId\":\"$INTEGRATION_ID\",\"deliverStandbyEvents\":false,\"nextSwitchboardIntegrationId\":\"$AGENT_WORKSPACE_ID\"}" \
    | jq -r '.switchboardIntegration.id')
  echo "Created switchboard integration $NAME: $BOT_ID"
else
  BOT_ID="$EXISTING_ID"
  call -X PATCH "$API/switchboards/$SWITCHBOARD_ID/switchboardIntegrations/$BOT_ID" \
    -d "{\"nextSwitchboardIntegrationId\":\"$AGENT_WORKSPACE_ID\"}" >/dev/null
  echo "Updated existing switchboard integration $NAME: $BOT_ID"
fi

call -X PATCH "$API/switchboards/$SWITCHBOARD_ID" \
  -d "{\"defaultSwitchboardIntegrationId\":\"$BOT_ID\"}" >/dev/null
echo "Set $NAME as the default responder. New conversations now start with the CXAS bot."
