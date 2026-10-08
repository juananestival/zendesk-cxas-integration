"""Conversation state and webhook de-duplication.

Firestore when FIRESTORE_SESSIONS_COLLECTION is set (needed once Cloud Run runs
more than one instance), otherwise an in-process TTL cache for local testing."""

import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from cachetools import TTLCache

from src import config

logger = logging.getLogger(__name__)

_SESSION_TTL = timedelta(hours=24)
_EVENT_TTL = timedelta(hours=1)


@dataclass
class BotSession:
    """One bot episode inside a Zendesk conversation.

    A Zendesk conversation outlives a CES session: after an agent solves the
    ticket, control returns to the bot on the same conversation, but an ended
    CES session cannot be resumed. So each episode gets its own CES session id."""

    ces_session_id: str
    # First customer message of the episode, passed as first_message_id on
    # handoff so the ticket's history starts here, not at earlier episodes.
    first_message_id: str | None = None


class MemoryStore:
    def __init__(self):
        self._sessions: TTLCache = TTLCache(maxsize=10_000, ttl=_SESSION_TTL.total_seconds())
        self._events: TTLCache = TTLCache(maxsize=50_000, ttl=_EVENT_TTL.total_seconds())

    async def get(self, conversation_id: str) -> BotSession | None:
        return self._sessions.get(conversation_id)

    async def put(self, conversation_id: str, session: BotSession) -> None:
        self._sessions[conversation_id] = session

    async def delete(self, conversation_id: str) -> None:
        self._sessions.pop(conversation_id, None)

    async def claim_event(self, event_id: str) -> bool:
        """True the first time an event id is seen; False for a redelivery."""
        if event_id in self._events:
            return False
        self._events[event_id] = True
        return True


class FirestoreStore:
    """Documents carry `expiry_time`; add a Firestore TTL policy on that field
    for both collections, or they accumulate."""

    def __init__(self, collection: str, database: str | None):
        from google.cloud import firestore

        self._db = firestore.AsyncClient(database=database)
        self._sessions = self._db.collection(collection)
        self._events = self._db.collection(f"{collection}_events")

    async def get(self, conversation_id: str) -> BotSession | None:
        snapshot = await self._sessions.document(conversation_id).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        return BotSession(data["ces_session_id"], data.get("first_message_id"))

    async def put(self, conversation_id: str, session: BotSession) -> None:
        await self._sessions.document(conversation_id).set(
            {**asdict(session), "expiry_time": datetime.now(UTC) + _SESSION_TTL}
        )

    async def delete(self, conversation_id: str) -> None:
        await self._sessions.document(conversation_id).delete()

    async def claim_event(self, event_id: str) -> bool:
        from google.api_core.exceptions import AlreadyExists

        try:
            # create() fails if the document exists, so the claim is atomic
            # across instances.
            await self._events.document(event_id).create(
                {"expiry_time": datetime.now(UTC) + _EVENT_TTL}
            )
            return True
        except AlreadyExists:
            return False


def build_store() -> MemoryStore | FirestoreStore:
    if config.FIRESTORE_SESSIONS_COLLECTION:
        logger.info(
            "Using Firestore collection %s for state.", config.FIRESTORE_SESSIONS_COLLECTION
        )
        return FirestoreStore(config.FIRESTORE_SESSIONS_COLLECTION, config.FIRESTORE_DATABASE_ID)
    logger.warning("Using in-memory state. Fine for local testing, not for production.")
    return MemoryStore()
