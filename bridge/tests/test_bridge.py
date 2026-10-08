import json

import httpx
import pytest
from fastapi.testclient import TestClient

from src import config, main
from src.ces import CesClient, parse_outputs
from src.store import MemoryStore
from src.sunco import SunshineClient

DEPLOYMENT = "projects/p/locations/us/apps/a1/deployments/d1"
APP_ID = "app123"
SECRET = "s3cret"


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_SECRETS", [SECRET])
    monkeypatch.setattr(config, "SUNCO_APP_ID", APP_ID)
    monkeypatch.setattr(config, "SUNCO_API_BASE", "https://acme.zendesk.com/sc")
    monkeypatch.setattr(config, "SUNCO_KEY_ID", "kid")
    monkeypatch.setattr(config, "SUNCO_KEY_SECRET", "ksecret")
    monkeypatch.setattr(config, "CES_DEPLOYMENT", DEPLOYMENT)
    monkeypatch.setattr(config, "CES_EXCLUDE_DIAGNOSTIC_INFO", True)
    monkeypatch.setattr(config, "TICKET_FIELD_ESCALATION_REASON", "111")
    monkeypatch.setattr(config, "TICKET_FIELD_CXAS_SESSION", "222")
    monkeypatch.setattr(config, "ESCALATION_PARAM_TICKET_FIELDS", {"queue": "333"})


class _Creds:
    valid = True
    token = "tok"


class Harness:
    """Wires real CES and Sunshine clients to a mock transport that records calls."""

    def __init__(self, ces_responses):
        self.ces_responses = list(ces_responses)
        self.ces_requests: list[dict] = []
        self.sunco_requests: list[tuple[str, dict]] = []

        def ces_handler(request: httpx.Request) -> httpx.Response:
            self.ces_requests.append({"url": str(request.url), "body": json.loads(request.content)})
            response = self.ces_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            if isinstance(response, int):
                return httpx.Response(response, json={"error": "boom"})
            return httpx.Response(200, json=response)

        def sunco_handler(request: httpx.Request) -> httpx.Response:
            self.sunco_requests.append((request.url.path, json.loads(request.content)))
            return httpx.Response(201, json={})

        main.app.state.ces = CesClient(
            http=httpx.AsyncClient(transport=httpx.MockTransport(ces_handler)), credentials=_Creds()
        )
        main.app.state.sunco = SunshineClient(
            http=httpx.AsyncClient(transport=httpx.MockTransport(sunco_handler))
        )
        main.app.state.store = MemoryStore()
        self.client = TestClient(main.app)

    def send(self, event, secret=SECRET):
        return self.client.post(
            "/v1/sunco/webhook",
            json={"app": {"id": APP_ID}, "webhook": {"version": "v2"}, "events": [event]},
            headers={"X-API-Key": secret},
        )

    def posted(self, kind):
        return [body for path, body in self.sunco_requests if path.endswith(f"/{kind}")]


def user_message(text, event_id="e1", message_id="m1", active="cxas-bot", content=None):
    return {
        "id": event_id,
        "type": "conversation:message",
        "payload": {
            "conversation": {"id": "c1", "activeSwitchboardIntegration": {"name": active}},
            "message": {
                "id": message_id,
                "author": {"type": "user"},
                "content": content or {"type": "text", "text": text},
            },
        },
    }


def test_rejects_bad_secret():
    h = Harness([])
    assert h.send(user_message("hi"), secret="wrong").status_code == 401
    assert h.ces_requests == []


def test_first_turn_starts_session_and_replies():
    h = Harness([{"outputs": [{"text": "Hello!"}, {"text": "How can I help?"}]}])
    assert h.send(user_message("hi")).status_code == 200

    [req] = h.ces_requests
    assert req["url"].startswith(
        "https://ces.googleapis.com/v1/projects/p/locations/us/apps/a1/sessions/"
    )
    assert req["url"].endswith(":runSession")
    assert req["body"]["config"]["deployment"] == DEPLOYMENT
    assert req["body"]["inputs"] == [{"event": {"event": "session_start"}}, {"text": "hi"}]
    assert [m["content"]["text"] for m in h.posted("messages")] == ["Hello!", "How can I help?"]
    assert all(m["author"]["type"] == "business" for m in h.posted("messages"))
    assert h.posted("passControl") == []


def test_second_turn_reuses_session_without_session_start():
    h = Harness([{"outputs": [{"text": "a"}]}, {"outputs": [{"text": "b"}]}])
    h.send(user_message("one", event_id="e1"))
    h.send(user_message("two", event_id="e2", message_id="m2"))

    first, second = h.ces_requests
    assert first["body"]["config"]["session"] == second["body"]["config"]["session"]
    assert second["body"]["inputs"] == [{"text": "two"}]


def test_redelivered_event_is_processed_once():
    h = Harness([{"outputs": [{"text": "a"}]}])
    h.send(user_message("hi"))
    h.send(user_message("hi"))
    assert len(h.ces_requests) == 1


def test_ignores_business_messages_and_agent_controlled_conversations():
    h = Harness([])
    echo = user_message("bot reply")
    echo["payload"]["message"]["author"]["type"] = "business"
    h.send(echo)
    h.send(user_message("hi agent", event_id="e2", active="zd-agentWorkspace"))
    assert h.ces_requests == []


def test_quick_reply_payload_is_sent_to_ces():
    h = Harness([{"outputs": [{"text": "ok"}]}])
    h.send(user_message(None, content={"type": "text", "text": "Yes please", "payload": "YES"}))
    assert h.ces_requests[0]["body"]["inputs"][-1] == {"text": "YES"}


def test_postback_is_sent_to_ces():
    h = Harness([{"outputs": [{"text": "ok"}]}])
    h.send(
        {
            "id": "e9",
            "type": "conversation:postback",
            "payload": {"conversation": {"id": "c1"}, "postback": {"payload": "TACOS"}},
        }
    )
    assert h.ces_requests[0]["body"]["inputs"][-1] == {"text": "TACOS"}


def test_rich_payload_is_posted_verbatim():
    carousel = {"type": "carousel", "items": [{"title": "T", "actions": []}]}
    h = Harness([{"outputs": [{"payload": {"zendesk": [carousel]}}]}])
    h.send(user_message("show me"))
    assert h.posted("messages")[0]["content"] == carousel


def test_escalation_passes_control_with_ticket_fields():
    h = Harness(
        [
            {"outputs": [{"text": "a"}]},
            {
                "outputs": [
                    {"text": "Connecting you to an agent."},
                    {
                        "endSession": {
                            "metadata": {
                                "session_escalated": True,
                                "params": {
                                    "reason": "Billing issue",
                                    "queue": "billing",
                                    "unused": 1,
                                },
                            }
                        }
                    },
                ]
            },
            {"outputs": [{"text": "fresh"}]},
        ]
    )
    h.send(user_message("hi", event_id="e1", message_id="m1"))
    h.send(user_message("agent please", event_id="e2", message_id="m2"))

    assert h.posted("messages")[-1]["content"]["text"] == "Connecting you to an agent."
    [handoff] = h.posted("passControl")
    assert handoff["switchboardIntegration"] == "zd-agentWorkspace"
    session = h.ces_requests[0]["body"]["config"]["session"]
    assert handoff["metadata"] == {
        "first_message_id": "m1",
        "dataCapture.ticketField.222": session,
        "dataCapture.ticketField.111": "Billing issue",
        "dataCapture.ticketField.333": "billing",
    }

    # When control comes back to the bot later, a new CES session starts.
    h.send(user_message("back again", event_id="e3", message_id="m3"))
    third = h.ces_requests[2]["body"]
    assert third["config"]["session"] != session
    assert third["inputs"][0] == {"event": {"event": "session_start"}}


def test_normal_end_does_not_pass_control():
    h = Harness([{"outputs": [{"text": "Bye!"}, {"endSession": {"metadata": {}}}]}])
    h.send(user_message("bye"))
    assert h.posted("passControl") == []
    assert main.app.state.store._sessions == {}


def test_ces_failure_hands_off_with_fallback_message():
    h = Harness([500])
    assert h.send(user_message("hi")).status_code == 200
    assert h.posted("messages")[-1]["content"]["text"] == config.FALLBACK_MESSAGE
    [handoff] = h.posted("passControl")
    assert handoff["metadata"]["dataCapture.ticketField.111"] == "bridge_error"


def test_ces_timeout_hands_off():
    h = Harness([httpx.ReadTimeout("slow")])
    h.send(user_message("hi"))
    assert len(h.posted("passControl")) == 1


def test_parse_outputs_accepts_string_flag():
    result = parse_outputs(
        {"outputs": [{"endSession": {"metadata": {"session_escalated": "true"}}}]}
    )
    assert result.ended and result.escalated


def test_head_root_for_zendesk_domain_check():
    h = Harness([])
    assert h.client.head("/").status_code == 200


def test_channel_and_conversation_id_sent_at_session_start(monkeypatch):
    monkeypatch.setattr(config, "CES_CHANNEL_VARIABLE", "channel")
    monkeypatch.setattr(config, "CES_CONVERSATION_ID_VARIABLE", "zendesk_conversation_id")
    h = Harness([{"outputs": [{"text": "a"}]}, {"outputs": [{"text": "b"}]}])
    first = user_message("hi")
    first["payload"]["message"]["source"] = {"type": "ios"}
    h.send(first)
    h.send(user_message("again", event_id="e2", message_id="m2"))

    assert h.ces_requests[0]["body"]["inputs"][0] == {
        "variables": {"zendesk_conversation_id": "c1", "channel": "ios"}
    }
    assert h.ces_requests[1]["body"]["inputs"] == [{"text": "again"}]


def test_health_reports_config(monkeypatch):
    h = Harness([])
    assert h.client.get("/health").json() == {"ok": True}
    monkeypatch.setattr(config, "CES_DEPLOYMENT", None)
    assert h.client.get("/health").json() == {"ok": False}
