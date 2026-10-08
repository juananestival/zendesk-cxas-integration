"""Client for the Zendesk-hosted Sunshine Conversations v2 API."""

import logging
from typing import Any

import httpx

from src import config

logger = logging.getLogger(__name__)


class SunshineClient:
    def __init__(self, http: httpx.AsyncClient | None = None):
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0))

    async def aclose(self) -> None:
        await self._http.aclose()

    def _url(self, app_id: str, conversation_id: str, suffix: str) -> str:
        return f"{config.SUNCO_API_BASE}/v2/apps/{app_id}/conversations/{conversation_id}/{suffix}"

    async def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._http.post(
            url, json=body, auth=(config.SUNCO_KEY_ID or "", config.SUNCO_KEY_SECRET or "")
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def _author(self) -> dict[str, Any]:
        author: dict[str, Any] = {"type": "business"}
        if config.BOT_DISPLAY_NAME:
            author["displayName"] = config.BOT_DISPLAY_NAME
        return author

    async def post_content(
        self, app_id: str, conversation_id: str, content: dict[str, Any]
    ) -> None:
        """Posts one message. `content` is a Sunshine Conversations content object
        (text, image, carousel, ... with optional actions)."""
        await self._post(
            self._url(app_id, conversation_id, "messages"),
            {"author": self._author(), "content": content},
        )

    async def post_text(self, app_id: str, conversation_id: str, text: str) -> None:
        await self.post_content(app_id, conversation_id, {"type": "text", "text": text})

    async def typing(self, app_id: str, conversation_id: str) -> None:
        """Best effort: a typing indicator must never break the turn."""
        try:
            await self._post(
                self._url(app_id, conversation_id, "activity"),
                {"author": self._author(), "type": "typing:start"},
            )
        except Exception:  # noqa: BLE001
            logger.warning("Could not send typing indicator.", exc_info=True)

    async def pass_control(
        self, app_id: str, conversation_id: str, target: str, metadata: dict[str, str]
    ) -> None:
        body: dict[str, Any] = {"switchboardIntegration": target}
        if metadata:
            body["metadata"] = metadata
        await self._post(self._url(app_id, conversation_id, "passControl"), body)
