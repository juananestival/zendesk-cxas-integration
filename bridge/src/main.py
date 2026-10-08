"""Zendesk messaging (Sunshine Conversations) bot bridge for CX Agent Studio.

Zendesk sends customer messages to POST /v1/sunco/webhook while this bot holds
the conversation's switchboard. Each message becomes a CES runSession turn, and
the replies are posted back. When CES ends the session with
`session_escalated`, control passes to Zendesk agents with the transcript.
"""

import asyncio
import hmac
import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from cachetools import TTLCache
from fastapi import FastAPI, Header, HTTPException, Request

from src import config
from src.ces import CesClient, TurnResult
from src.store import BotSession, build_store
from src.sunco import SunshineClient


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, which Cloud Logging parses into jsonPayload."""

    _RESERVED = set(vars(logging.makeLogRecord({})))

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {"severity": record.levelname, "message": record.getMessage()}
        entry.update({k: v for k, v in vars(record).items() if k not in self._RESERVED})
        if record.exc_info:
            entry["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
logger = logging.getLogger("bridge")


@asynccontextmanager
async def lifespan(app: FastAPI):
    for problem in config.validate():
        logger.error("Configuration problem: %s", problem)
    app.state.ces = CesClient()
    app.state.sunco = SunshineClient()
    app.state.store = build_store()
    try:
        yield
    finally:
        await app.state.ces.aclose()
        await app.state.sunco.aclose()


app = FastAPI(title="Zendesk CXAS bridge", lifespan=lifespan)

# Serialises turns per conversation within an instance, so two quick messages
# don't both start a CES session. Cross-instance races are rare and only cost a
# duplicate session_start.
_conversation_locks: TTLCache = TTLCache(maxsize=10_000, ttl=3600)


def _verify_secret(supplied: str | None) -> bool:
    """Constant-time comparison against every configured webhook secret."""
    candidate = supplied or ""
    matched = False
    for expected in config.WEBHOOK_SECRETS:
        if hmac.compare_digest(candidate, expected):
            matched = True
    return matched


def _to_metadata_value(value: Any) -> str:
    """passControl metadata values must be strings."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, default=str)


def extract_user_input(event: dict[str, Any]) -> tuple[str, str | None, str | None] | None:
    """Returns (text for CES, message id, channel) for events the bot should answer,
    or None. The channel is the message source type: "web", "android", "ios", ...

    Quick-reply taps arrive as messages whose content carries the reply payload;
    postback buttons arrive as conversation:postback events."""
    payload = event.get("payload") or {}
    if event.get("type") == "conversation:message":
        message = payload.get("message") or {}
        if (message.get("author") or {}).get("type") != "user":
            return None  # our own replies and agent messages echo back here
        content = message.get("content") or {}
        text = content.get("payload") or content.get("text")
        if not text:
            logger.info(
                "Ignoring non-text customer message.", extra={"content_type": content.get("type")}
            )
            return None
        return text, message.get("id"), (message.get("source") or {}).get("type")
    if event.get("type") == "conversation:postback":
        postback = payload.get("postback") or {}
        text = postback.get("payload") or postback.get("text")
        return (text, None, None) if text else None
    return None


def _bot_is_active(conversation: dict[str, Any]) -> bool:
    """True unless the event says another integration (e.g. agents) has control."""
    active = conversation.get("activeSwitchboardIntegration")
    if not isinstance(active, dict) or "name" not in active:
        return True
    return active["name"] == config.SWITCHBOARD_INTEGRATION_NAME


def _rich_contents(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """CES `payload` outputs under a `zendesk` key are posted verbatim as
    Sunshine Conversations content objects (quick replies, carousels, ...)."""
    contents = payload.get("zendesk")
    if isinstance(contents, dict):
        contents = [contents]
    if not isinstance(contents, list):
        return []
    return [c for c in contents if isinstance(c, dict) and c.get("type")]


def build_handoff_metadata(
    session: BotSession, result: TurnResult | None, ces_session: str
) -> dict[str, str]:
    metadata: dict[str, str] = {}
    if session.first_message_id:
        metadata["first_message_id"] = session.first_message_id
    if config.TICKET_FIELD_CXAS_SESSION:
        metadata[f"dataCapture.ticketField.{config.TICKET_FIELD_CXAS_SESSION}"] = ces_session
    reason = result.escalation_reason if result else "bridge_error"
    if config.TICKET_FIELD_ESCALATION_REASON and reason:
        metadata[f"dataCapture.ticketField.{config.TICKET_FIELD_ESCALATION_REASON}"] = reason
    params = result.escalation_params if result else {}
    for param, field_id in config.ESCALATION_PARAM_TICKET_FIELDS.items():
        if param in params:
            metadata[f"dataCapture.ticketField.{field_id}"] = _to_metadata_value(params[param])
    return metadata


async def handle_turn(
    app_id: str,
    conversation_id: str,
    text: str,
    message_id: str | None,
    channel: str | None = None,
) -> None:
    ces: CesClient = app.state.ces
    sunco: SunshineClient = app.state.sunco
    store = app.state.store
    log = {"conversation_id": conversation_id}

    session = await store.get(conversation_id)
    is_new = session is None
    if session is None:
        session = BotSession(ces_session_id=uuid.uuid4().hex, first_message_id=message_id)
    ces_session = CesClient.session_name(config.CES_DEPLOYMENT, session.ces_session_id)
    log["ces_session"] = ces_session

    variables = {}
    if is_new and config.CES_CONVERSATION_ID_VARIABLE:
        variables[config.CES_CONVERSATION_ID_VARIABLE] = conversation_id
    if is_new and config.CES_CHANNEL_VARIABLE and channel:
        variables[config.CES_CHANNEL_VARIABLE] = channel

    await sunco.typing(app_id, conversation_id)
    try:
        result = await ces.run_turn(
            session.ces_session_id, text, is_new_session=is_new, variables=variables
        )
    except Exception as e:  # noqa: BLE001 - any CES failure becomes a handoff
        extra = {**log, "bridge_error": True}
        if isinstance(e, httpx.HTTPStatusError):
            extra["ces_status_code"] = e.response.status_code
        if isinstance(e, httpx.TimeoutException):
            extra["ces_timeout"] = True
        logger.error("CES turn failed; handing off to agents.", extra=extra, exc_info=True)
        await sunco.post_text(app_id, conversation_id, config.FALLBACK_MESSAGE)
        await sunco.pass_control(
            app_id,
            conversation_id,
            config.ESCALATION_TARGET,
            build_handoff_metadata(session, None, ces_session),
        )
        await store.delete(conversation_id)
        return

    for kind, value in result.replies:
        if kind == "text":
            await sunco.post_text(app_id, conversation_id, value)
        else:
            for content in _rich_contents(value):
                await sunco.post_content(app_id, conversation_id, content)

    if result.escalated:
        logger.info(
            "CES escalated; passing control.", extra={**log, "reason": result.escalation_reason}
        )
        await sunco.pass_control(
            app_id,
            conversation_id,
            config.ESCALATION_TARGET,
            build_handoff_metadata(session, result, ces_session),
        )
        await store.delete(conversation_id)
    elif result.ended:
        logger.info("CES ended the session.", extra=log)
        await store.delete(conversation_id)
    elif is_new:
        await store.put(conversation_id, session)


@app.post("/v1/sunco/webhook")
async def webhook(request: Request, x_api_key: str | None = Header(None, alias="x-api-key")):
    if not _verify_secret(x_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

    body = await request.json()
    app_id = (body.get("app") or {}).get("id")
    if not app_id or (config.SUNCO_APP_ID and app_id != config.SUNCO_APP_ID):
        logger.warning("Ignoring webhook for unexpected app.", extra={"app_id": app_id})
        return {}

    for event in body.get("events") or []:
        conversation = (event.get("payload") or {}).get("conversation") or {}
        conversation_id = conversation.get("id")
        user_input = extract_user_input(event)
        if not conversation_id or user_input is None or not _bot_is_active(conversation):
            continue
        # Zendesk redelivers on timeouts; process each event once.
        if event.get("id") and not await app.state.store.claim_event(event["id"]):
            logger.info("Skipping redelivered event.", extra={"event_id": event["id"]})
            continue

        text, message_id, channel = user_input
        lock = _conversation_locks.get(conversation_id)
        if lock is None:
            lock = _conversation_locks[conversation_id] = asyncio.Lock()
        async with lock:
            try:
                await handle_turn(app_id, conversation_id, text, message_id, channel)
            except Exception:  # noqa: BLE001 - never fail the whole batch
                logger.error(
                    "Turn failed.",
                    extra={"conversation_id": conversation_id, "bridge_error": True},
                    exc_info=True,
                )

    # Always 200 once authenticated: a non-2xx makes Zendesk retry the batch,
    # replaying turns that already ran.
    return {}


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    """Zendesk sends a HEAD to the webhook's root domain when the webhook is saved."""
    return {}


@app.get("/healthz")
async def healthz():
    return {"ok": not config.validate()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
