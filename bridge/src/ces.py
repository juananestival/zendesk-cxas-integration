"""Client for CX Agent Studio `sessions:runSession` over REST."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from src import config

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


@dataclass
class TurnResult:
    """What one CES turn produced, flattened for the Zendesk side."""

    # Replies in CES order, as ("text", str) or ("payload", dict).
    replies: list[tuple[str, Any]] = field(default_factory=list)
    ended: bool = False
    escalated: bool = False
    end_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def escalation_params(self) -> dict[str, Any]:
        params = self.end_metadata.get("params")
        return params if isinstance(params, dict) else {}

    @property
    def escalation_reason(self) -> str | None:
        reason = self.escalation_params.get("reason") or self.end_metadata.get("reason")
        return str(reason) if reason else None


def parse_outputs(data: dict[str, Any]) -> TurnResult:
    """Turns a RunSessionResponse into a TurnResult.

    `endSession.metadata` carries `session_escalated` and the `params` given to the
    agent's end_session tool, which is how a goodbye is told apart from a handoff."""
    result = TurnResult()
    for output in data.get("outputs", []) or []:
        if not isinstance(output, dict):
            continue
        if isinstance(output.get("text"), str) and output["text"]:
            result.replies.append(("text", output["text"]))
        if isinstance(output.get("payload"), dict):
            result.replies.append(("payload", output["payload"]))
        if "endSession" in output:
            result.ended = True
            metadata = (output.get("endSession") or {}).get("metadata") or {}
            result.end_metadata = metadata if isinstance(metadata, dict) else {}
            escalated = result.end_metadata.get("session_escalated", False)
            result.escalated = escalated is True or str(escalated).lower() == "true"
    return result


class CesClient:
    """Thin async wrapper around runSession with a shared connection pool."""

    def __init__(self, http: httpx.AsyncClient | None = None, credentials=None):
        self._http = http or httpx.AsyncClient(
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
            timeout=httpx.Timeout(
                config.CES_TIMEOUT_SECONDS, connect=config.CES_CONNECT_TIMEOUT_SECONDS
            ),
        )
        self._credentials = credentials
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _access_token(self) -> str:
        """credentials.refresh blocks, so it runs in a thread behind a lock."""
        if self._credentials is None:
            import google.auth

            self._credentials, _ = google.auth.default(scopes=_SCOPES)
        if self._credentials.valid:
            return self._credentials.token
        async with self._token_lock:
            if not self._credentials.valid:
                import google.auth.transport.requests

                await asyncio.to_thread(
                    self._credentials.refresh, google.auth.transport.requests.Request()
                )
        return self._credentials.token

    @staticmethod
    def session_name(deployment: str, session_id: str) -> str:
        app = "/".join(deployment.split("/")[:6])
        return f"{app}/sessions/{session_id}"

    async def run_turn(
        self,
        session_id: str,
        text: str,
        *,
        is_new_session: bool,
        variables: dict[str, Any] | None = None,
    ) -> TurnResult:
        deployment = config.CES_DEPLOYMENT
        session = self.session_name(deployment, session_id)
        location = deployment.split("/")[3]

        inputs: list[dict[str, Any]] = []
        if variables:
            inputs.append({"variables": variables})
        if is_new_session:
            inputs.append({"event": {"event": "session_start"}})
        inputs.append({"text": text})

        session_config: dict[str, Any] = {"session": session, "deployment": deployment}
        if config.CES_EXCLUDE_DIAGNOSTIC_INFO:
            session_config["excludeDiagnosticInfo"] = True

        response = await self._http.post(
            f"{config.CES_API_BASE}/{session}:runSession",
            json={"config": session_config, "inputs": inputs},
            headers={
                "Authorization": f"Bearer {await self._access_token()}",
                "x-goog-request-params": f"location=locations/{location}",
            },
        )
        response.raise_for_status()
        data = response.json()
        if config.DEBUG_MODE:
            logger.info("CES response", extra={"ces_response": data})
        return parse_outputs(data)
