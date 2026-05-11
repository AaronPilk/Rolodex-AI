from __future__ import annotations

from fastapi.testclient import TestClient

from agent.channels.base import SendResult
from agent.web import META_DM_FIRST_HINT, create_app


class _MetaChannelStub:
    def __init__(self, *, configured: bool = True, conversations=None) -> None:
        self._configured = configured
        self._conversations = conversations if conversations is not None else []
        self.sent: list[tuple[str, str]] = []

    def is_configured(self) -> bool:
        return self._configured

    def list_conversations(self, limit: int = 20):
        return self._conversations[:limit]

    def send(self, handle: str, text: str) -> SendResult:
        self.sent.append((handle, text))
        return SendResult(ok=True, channel="instagram", handle=handle, message_id="mid.123")


def test_meta_inbox_reply_and_send_test_endpoints(monkeypatch) -> None:
    conversations = [
        {
            "id": "thread-1",
            "participant_id": "17841",
            "participant_username": "tester_account",
            "last_message": {"text": "test from Aaron", "from_them": True, "at": "2026-05-11T02:03:04+00:00"},
            "message_count": 1,
            "updated_time": "2026-05-11T02:03:04+00:00",
        }
    ]
    stub = _MetaChannelStub(conversations=conversations)
    monkeypatch.setattr("agent.web.get_channel", lambda name: stub)

    client = TestClient(create_app())

    inbox = client.get("/api/connections/instagram/inbox?limit=20")
    assert inbox.status_code == 200
    assert inbox.json() == {
        "channel": "instagram",
        "conversations": conversations,
        "error": None,
    }

    reply = client.post(
        "/api/connections/instagram/reply",
        json={"participant_id": "17841", "text": "Reply from Rolodex"},
    )
    assert reply.status_code == 200
    assert reply.json() == {
        "ok": True,
        "channel": "instagram",
        "message_id": "mid.123",
        "error": None,
    }

    send_test = client.post(
        "/api/connections/instagram/send_test",
        json={"handle": "17841"},
    )
    assert send_test.status_code == 200
    assert send_test.json() == {
        "ok": True,
        "message_id": "mid.123",
        "error": None,
    }

    assert stub.sent == [
        ("17841", "Reply from Rolodex"),
        ("17841", "Hey, this is a Rolodex AI test message — feel free to ignore."),
    ]


def test_meta_inbox_capability_error_returns_friendly_hint(monkeypatch) -> None:
    class _CapabilityErrorChannel(_MetaChannelStub):
        def list_conversations(self, limit: int = 20):
            raise RuntimeError('400: {"error":{"message":"(#3) Application does not have the capability to make this API call"}}')

    monkeypatch.setattr("agent.web.get_channel", lambda name: _CapabilityErrorChannel())
    client = TestClient(create_app())

    inbox = client.get("/api/connections/instagram/inbox")

    assert inbox.status_code == 200
    assert inbox.json() == {
        "channel": "instagram",
        "conversations": [],
        "error": META_DM_FIRST_HINT,
    }
