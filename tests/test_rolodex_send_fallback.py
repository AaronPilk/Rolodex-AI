from __future__ import annotations

import pytest

from agent.imessage_sender import SendError, SendUnavailable, send_with_fallback


def test_send_with_fallback_uses_twilio_when_imessage_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def _imessage(_handle: str, _message: str):
        raise SendError("AppleScript timed out")

    class _SmsResult:
        channel = "sms"
        provider_id = "SM123"

    def _twilio(handle: str, message: str):
        calls.append((handle, message))
        return _SmsResult()

    monkeypatch.setattr("agent.imessage_sender.send_imessage", _imessage)
    monkeypatch.setattr("agent.imessage_sender.send_sms_via_twilio", _twilio)

    receipt = send_with_fallback("+14155551234", "hello there")

    assert calls == [("+14155551234", "hello there")]
    assert receipt.channel == "sms"
    assert receipt.provider_id == "SM123"


def test_send_with_fallback_raises_cleanly_when_twilio_also_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def _imessage(_handle: str, _message: str):
        raise SendError("Apple permission denied")

    def _twilio(_handle: str, _message: str):
        raise SendUnavailable("Twilio fallback failed: bad auth")

    monkeypatch.setattr("agent.imessage_sender.send_imessage", _imessage)
    monkeypatch.setattr("agent.imessage_sender.send_sms_via_twilio", _twilio)

    with pytest.raises(SendUnavailable, match="Twilio fallback failed: bad auth"):
        send_with_fallback("+14155551234", "hello there")
