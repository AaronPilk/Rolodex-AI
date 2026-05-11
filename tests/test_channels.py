from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.channels import get_channel
from agent.channels.dispatcher import route_message
from agent.channels.instagram import InstagramChannel
from agent.channels.base import SendResult
from agent.models import PersonRecord


def test_get_channel_telegram_configured(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    channel = get_channel("telegram")
    assert channel.name == "telegram"
    assert channel.is_configured() is True


def test_dispatcher_route_message_falls_back(monkeypatch) -> None:
    person = PersonRecord(
        person_id="p1",
        handles=["telegram:12345", "+14155551234"],
        connected_channels=["telegram", "imessage"],
    )

    class FailingChannel:
        def send(self, handle: str, text: str) -> SendResult:
            raise RuntimeError(f"fail:{handle}:{text}")

    class PassingChannel:
        def send(self, handle: str, text: str) -> SendResult:
            return SendResult(ok=True, channel="imessage", handle=handle, message_id="mid")

    monkeypatch.setattr("agent.channels.dispatcher.get_channel", lambda name: FailingChannel() if name == "telegram" else PassingChannel())

    result = route_message(person, "hello")

    assert result.ok is True
    assert result.channel == "imessage"
    assert result.handle == "+14155551234"


def test_instagram_channel_configured_from_env(monkeypatch) -> None:
    monkeypatch.delenv("META_PAGE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("META_IG_BUSINESS_ID", raising=False)
    channel = InstagramChannel()
    assert channel.is_configured() is False

    monkeypatch.setenv("META_PAGE_ACCESS_TOKEN", "token")
    monkeypatch.setenv("META_IG_BUSINESS_ID", "ig-id")
    assert channel.is_configured() is True
