# Zendesk CXAS bridge

A Cloud Run service that puts a Google CX Agent Studio (CXAS) agent in front of
Zendesk messaging. The customer chats in the Zendesk Web Widget or in the
Zendesk messaging SDK inside the Android or iOS app. All of them reach the
bridge the same way. The CXAS agent
answers first. When the agent calls `end_session(session_escalated=True, ...)`,
the conversation moves to a human in the Zendesk Agent Workspace, in the same
widget and with the bot transcript.

The service is modelled on
[ces-genesys-chat](https://github.com/GoogleCloudPlatform/ces-genesys-chat). It
uses the same `runSession` call, the same `endSession.metadata` handling and
the same fallback-to-a-human on errors. The difference is the Zendesk side.
Genesys calls the adapter and waits for a synchronous reply. Here, Zendesk
sends webhooks, and the bridge answers through the Sunshine Conversations API.
See [the architecture notes](../docs/architecture/escalation-options.md) for
why this approach was chosen.

## How a turn works

1. Zendesk posts a `conversation:message` (or `conversation:postback`) webhook
   to `POST /v1/sunco/webhook`, with the webhook secret in `X-API-Key`.
2. The bridge ignores anything that is not a customer message, and any
   conversation that the bot does not control (`activeSwitchboardIntegration`).
   It also skips redelivered events, using the event id.
3. It shows a typing indicator, then calls CES `runSession`:
   - The first message of a bot episode gets a fresh CES session and a
     `session_start` event.
   - Later messages reuse that session.
   - A quick-reply or postback payload is sent as the text.
4. It posts the CES `text` outputs back as business messages. A CES `payload`
   output with a `zendesk` key is posted verbatim as Sunshine Conversations
   content, which is how CXAS sends quick replies and carousels (see
   [Rich messages](#rich-messages)).
5. It then looks at `endSession`:
   - **`session_escalated` true**: the bridge calls `passControl` to
     `ESCALATION_TARGET` (default `zd-agentWorkspace`). The metadata carries
     `first_message_id` (so the ticket history starts at this bot episode) and
     any configured ticket fields.
   - **Normal end**: the bridge forgets the session. The next message starts a
     new one.
   - **CES error or timeout**: the bridge posts `FALLBACK_MESSAGE`, then hands
     off with the escalation reason `bridge_error`.

After an agent solves the ticket, Zendesk releases control and the bot is the
first responder again. Each bot episode uses a new CES session, because an
ended CES session can't be resumed.

## Configuration

| Variable | Required | Description |
| --- | --- | --- |
| `CES_DEPLOYMENT` | yes | `projects/*/locations/*/apps/*/deployments/*` |
| `ZENDESK_SUBDOMAIN` | yes | Builds `https://{subdomain}.zendesk.com/sc`. `SUNCO_API_BASE` overrides it. |
| `SUNCO_APP_ID` | yes | Sunshine Conversations app id. Webhooks for other apps are ignored. |
| `SUNCO_KEY_ID`, `SUNCO_KEY_SECRET` | yes | Conversations API key (Basic auth). The secret may be a Secret Manager name. |
| `SUNCO_WEBHOOK_SECRET` | yes | Webhook secret, checked against `X-API-Key`. Comma-separated values are allowed for rotation. Each may be a Secret Manager name. |
| `SWITCHBOARD_INTEGRATION_NAME` | no | This bot's switchboard integration name. Default `cxas-bot`. |
| `ESCALATION_TARGET` | no | `passControl` target: `zd-agentWorkspace` (default) or `next`. |
| `TICKET_FIELD_ESCALATION_REASON` | no | Ticket field id that receives the `reason` from `end_session`. |
| `TICKET_FIELD_CXAS_SESSION` | no | Ticket field id that receives the full CES session name. |
| `ESCALATION_PARAM_TICKET_FIELDS` | no | Maps other `end_session` params to ticket fields, e.g. `queue=360001,priority=360002`. |
| `CES_CONVERSATION_ID_VARIABLE` | no | If set, sends the Zendesk conversation id to CES under this variable at session start. The variable must exist in the CXAS app. |
| `CES_CHANNEL_VARIABLE` | no | If set, sends the customer's channel (`web`, `android`, `ios`, ...) to CES under this variable at session start. The variable must exist in the CXAS app. |
| `BOT_DISPLAY_NAME` | no | Name shown on bot messages. |
| `FALLBACK_MESSAGE` | no | Text shown before an error handoff. |
| `FIRESTORE_SESSIONS_COLLECTION` | prod | Shared state across instances. Without it, state is in memory, which is only fine for local testing. Add a Firestore TTL policy on `expiry_time` for this collection and for `<collection>_events`. |
| `FIRESTORE_DATABASE_ID` | no | Named Firestore database. Default `(default)`. |
| `CES_TIMEOUT_SECONDS` | no | Budget for one CES call, default `8.0`. Keep it under the webhook timeout. |
| `CES_EXCLUDE_DIAGNOSTIC_INFO` | no | Defaults to the inverse of `DEBUG`. |
| `DEBUG` | no | Logs full CES responses. Don't use it in production: it logs customer text. |

## Setup

### 1. Zendesk

1. Turn on messaging and add the Web Widget snippet to the website (see
   [Adding the chat to a website](../docs/website-widget.md)).
2. In Admin Center, go to Apps and integrations › Integrations ›
   Conversations integrations and create an integration:
   - **Webhook URL:** `https://<cloud-run-url>/v1/sunco/webhook`
   - **Triggers:** "Conversation message" and "Postbacks"
   - **Version:** v2
   Note its id and its webhook secret, and create an API key for it (key id and
   secret).
3. Optionally, create ticket fields for the escalation reason and the CXAS
   session, and note their ids.
4. Make the bot the first responder:
   ```bash
   # Reads the ids from script/values.sh. If the integration key gets a 401/403,
   # pass an app-level key instead: SUNCO_KEY_ID=app_... SUNCO_KEY_SECRET_VALUE=...
   read -r -s SUNCO_KEY_SECRET_VALUE && export SUNCO_KEY_SECRET_VALUE
   ./script/setup_switchboard.sh
   ```
   This creates the `cxas-bot` switchboard integration, sets its next
   integration to `zd-agentWorkspace`, and makes it the switchboard default.
   That turns off Zendesk's own AI agent as first responder for messaging. To
   limit the bot to the Web Widget only, set `defaultResponderId` on that
   channel's integration instead (see the
   [switchboard docs](https://developer.zendesk.com/documentation/conversations/messaging-platform/programmable-conversations/switchboard/)).

### 2. CX Agent Studio

Escalate from the agent with the `end_session` system tool, for example:

```python
end_session(reason="Billing issue", session_escalated=True, params={"queue": "billing"})
```

`reason` and any params mapped in `ESCALATION_PARAM_TICKET_FIELDS` end up on the
ticket. Whatever the agent says before ending reaches the customer as normal
text.

### 3. Google Cloud

In Cloud Shell, from `bridge/`:

```bash
cp script/values.sh.example script/values.sh   # set CES_DEPLOYMENT and the SUNCO_* ids
./script/setup_gcp.sh   # APIs, service account and roles, secrets (prompts), Firestore + TTL
./script/deploy.sh
```

### Order of operations

The Zendesk webhook secret only exists after the integration is created, and
the webhook URL only exists after the first deploy. So:

1. Create the Conversations integration in Zendesk (Zendesk step 2) with any
   placeholder webhook URL, and note the integration id, app id, API key id and
   secret, and webhook secret.
2. Run `setup_gcp.sh`. It asks for the two secrets.
3. Run `deploy.sh`, and copy the service URL it prints.
4. In Admin Center, set the integration's webhook URL to
   `<service URL>/v1/sunco/webhook`.
5. Run `setup_switchboard.sh` (Zendesk step 4).
6. Open the Web Widget and say hi.

The service is deployed with `--allow-unauthenticated`, because Zendesk can't
send Google identity tokens. Every webhook is authenticated with the shared
`X-API-Key` secret instead.

## Rich messages

To send structured messages, have the CXAS agent emit a `payload` output with a
`zendesk` key. It can hold one Sunshine Conversations content object or a list
of them:

```json
{"zendesk": [{"type": "text", "text": "Which one?", "actions": [
  {"type": "reply", "text": "Billing", "payload": "BILLING"},
  {"type": "reply", "text": "Technical", "payload": "TECH"}
]}]}
```

When the customer taps a quick reply, its `payload` (`BILLING`) is sent to CES
as the user's text. Postback buttons work the same way, through
`conversation:postback`.

Prefer quick replies. They work in the Web Widget and in both mobile SDKs.
Postback buttons don't render in the Web Widget, and Android shows only one
button on a text message (see
[Web Widget and SDK capabilities](https://developer.zendesk.com/documentation/zendesk-web-widget-sdks/capabilities/)).
If the agent needs to tailor content per surface, set `CES_CHANNEL_VARIABLE`.

## Local development

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest
uv run ruff check . && uv run ruff format --check .
gcloud auth application-default login
uv run uvicorn src.main:app --port 8080 --reload   # expose with ngrok for Zendesk
```

## Known limits and things to verify in the spike

- **Webhook timeout:** turns are processed inside the webhook request, so the
  CES budget (`CES_TIMEOUT_SECONDS`) must stay under Zendesk's webhook timeout.
  Redeliveries are de-duplicated by event id. If CES latency gets close to the
  limit, move turns to Cloud Tasks.
- **Escalation message:** check with a real escalation that the agent's message
  reaches the customer as a `text` output. The bridge doesn't post
  `params.ESCALATION_MESSAGE` separately, to avoid sending it twice.
- **Non-text messages:** images and files from the customer are ignored while
  the bot is in control.
