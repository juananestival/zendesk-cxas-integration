"""Runtime configuration, read once from environment variables at import."""

import logging
import os
import re
import time

logger = logging.getLogger(__name__)

_GSM_FETCH_ATTEMPTS = 3
_GSM_FETCH_BACKOFF_SECONDS = 0.5

_DEPLOYMENT_PATTERN = re.compile(r"^projects/[^/]+/locations/[^/]+/apps/[^/]+/deployments/[^/]+$")


def _env_str(name: str) -> str | None:
    """Optional string env var; blank counts as unset."""
    return (os.environ.get(name) or "").strip() or None


def _env_flag(name: str, default: bool) -> bool:
    raw = _env_str(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    """Float env var; bad or out-of-range values fall back to the default."""
    raw = _env_str(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using default %s.", name, raw, default)
        return default
    if not minimum <= value <= maximum:
        logger.warning(
            "%s=%s is outside [%s, %s]; using default %s.", name, value, minimum, maximum, default
        )
        return default
    return value


def resolve_secret(value: str) -> str:
    """Resolves a `projects/.../secrets/.../versions/...` value via Secret Manager,
    otherwise returns it verbatim. Retried because this runs at import time."""
    if not value.startswith("projects/"):
        return value
    from google.cloud import secretmanager

    last_error: Exception | None = None
    for attempt in range(1, _GSM_FETCH_ATTEMPTS + 1):
        try:
            client = secretmanager.SecretManagerServiceClient()
            response = client.access_secret_version(name=value)
            return response.payload.data.decode("UTF-8").strip()
        except Exception as e:  # noqa: BLE001 - retried and re-raised below
            last_error = e
            logger.warning(
                "Secret Manager lookup failed, attempt %d/%d: %s", attempt, _GSM_FETCH_ATTEMPTS, e
            )
            if attempt < _GSM_FETCH_ATTEMPTS:
                time.sleep(_GSM_FETCH_BACKOFF_SECONDS * attempt)
    raise last_error  # type: ignore[misc]


def _resolve_list(raw: str | None) -> list[str]:
    """Comma-separated list, so a new secret can be accepted alongside the old one
    during rotation. Each entry may be a literal or a Secret Manager name."""
    if not raw:
        return []
    return [
        resolved
        for entry in raw.split(",")
        if entry.strip() and (resolved := resolve_secret(entry.strip()))
    ]


def _parse_field_map(raw: str | None) -> dict[str, str]:
    """Parses `param=ticketFieldId,other=ticketFieldId` into a dict."""
    mapping: dict[str, str] = {}
    for entry in (raw or "").split(","):
        if not entry.strip():
            continue
        key, sep, field_id = entry.partition("=")
        if not sep or not key.strip() or not field_id.strip():
            logger.warning("Ignoring malformed ESCALATION_PARAM_TICKET_FIELDS entry %r.", entry)
            continue
        mapping[key.strip()] = field_id.strip()
    return mapping


DEBUG_MODE = _env_flag("DEBUG", False)

# --- Zendesk / Sunshine Conversations ---------------------------------------

ZENDESK_SUBDOMAIN = _env_str("ZENDESK_SUBDOMAIN")
# Override for non-standard hosts (e.g. a custom domain). Defaults to the
# Zendesk-hosted Sunshine Conversations API for the subdomain.
SUNCO_API_BASE = _env_str("SUNCO_API_BASE") or (
    f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/sc" if ZENDESK_SUBDOMAIN else None
)
SUNCO_APP_ID = _env_str("SUNCO_APP_ID")
SUNCO_KEY_ID = _env_str("SUNCO_KEY_ID")
SUNCO_KEY_SECRET = resolve_secret(_env_str("SUNCO_KEY_SECRET") or "") or None

# Secret(s) of the Sunshine Conversations webhook, sent by Zendesk in X-API-Key.
WEBHOOK_SECRETS = _resolve_list(os.environ.get("SUNCO_WEBHOOK_SECRET"))

# Name of this bot's switchboard integration. Events for conversations that
# another integration (e.g. zd-agentWorkspace) controls are ignored.
SWITCHBOARD_INTEGRATION_NAME = _env_str("SWITCHBOARD_INTEGRATION_NAME") or "cxas-bot"
# Where to pass control on escalation. "next" follows the switchboard's
# nextSwitchboardIntegrationId; "zd-agentWorkspace" targets agents directly.
ESCALATION_TARGET = _env_str("ESCALATION_TARGET") or "zd-agentWorkspace"

# Optional Zendesk ticket field ids filled on escalation via passControl metadata.
TICKET_FIELD_ESCALATION_REASON = _env_str("TICKET_FIELD_ESCALATION_REASON")
TICKET_FIELD_CXAS_SESSION = _env_str("TICKET_FIELD_CXAS_SESSION")
# Maps end_session params to ticket field ids, e.g. "queue=360001,priority=360002".
ESCALATION_PARAM_TICKET_FIELDS = _parse_field_map(_env_str("ESCALATION_PARAM_TICKET_FIELDS"))

BOT_DISPLAY_NAME = _env_str("BOT_DISPLAY_NAME")
FALLBACK_MESSAGE = _env_str("FALLBACK_MESSAGE") or (
    "Sorry, I'm having trouble right now. Let me connect you with someone who can help."
)

# --- CX Agent Studio --------------------------------------------------------

# projects/{project}/locations/{location}/apps/{app}/deployments/{deployment}
CES_DEPLOYMENT = _env_str("CES_DEPLOYMENT")
CES_API_BASE = _env_str("CES_API_BASE") or "https://ces.googleapis.com/v1"
# If set, the Zendesk conversation id is sent to CES under this variable name at
# session start. The variable must be declared in the CXAS app.
CES_CONVERSATION_ID_VARIABLE = _env_str("CES_CONVERSATION_ID_VARIABLE")
# If set, sends the customer's channel (Sunshine Conversations source type, e.g.
# "web", "android", "ios") under this variable at session start, so the agent can
# pick rich content each surface supports. Must be declared in the CXAS app.
CES_CHANNEL_VARIABLE = _env_str("CES_CHANNEL_VARIABLE")
CES_EXCLUDE_DIAGNOSTIC_INFO = _env_flag("CES_EXCLUDE_DIAGNOSTIC_INFO", not DEBUG_MODE)
# Whole-call budget for one CES turn. Keep it under the Sunshine Conversations
# webhook timeout so the reply (or fallback handoff) lands before Zendesk retries.
CES_TIMEOUT_SECONDS = _env_float("CES_TIMEOUT_SECONDS", 8.0, minimum=0.5, maximum=30.0)
CES_CONNECT_TIMEOUT_SECONDS = min(3.0, CES_TIMEOUT_SECONDS)

# --- State ------------------------------------------------------------------

FIRESTORE_SESSIONS_COLLECTION = _env_str("FIRESTORE_SESSIONS_COLLECTION")
FIRESTORE_DATABASE_ID = _env_str("FIRESTORE_DATABASE_ID")


def validate() -> list[str]:
    """Returns human-readable problems with the configuration; empty when usable."""
    problems = []
    for name, value in (
        ("ZENDESK_SUBDOMAIN or SUNCO_API_BASE", SUNCO_API_BASE),
        ("SUNCO_APP_ID", SUNCO_APP_ID),
        ("SUNCO_KEY_ID", SUNCO_KEY_ID),
        ("SUNCO_KEY_SECRET", SUNCO_KEY_SECRET),
        ("SUNCO_WEBHOOK_SECRET", WEBHOOK_SECRETS),
        ("CES_DEPLOYMENT", CES_DEPLOYMENT),
    ):
        if not value:
            problems.append(f"{name} is not set")
    if CES_DEPLOYMENT and not _DEPLOYMENT_PATTERN.match(CES_DEPLOYMENT):
        problems.append("CES_DEPLOYMENT must look like projects/*/locations/*/apps/*/deployments/*")
    return problems
