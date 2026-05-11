from __future__ import annotations

from agent.channels import get_channel
from agent.channels.base import SendResult
from agent.imessage_sender import send_sms_via_twilio
from agent.models import PersonRecord

CHANNEL_PREFIXES = {
    "telegram": "telegram:",
    "whatsapp": "whatsapp:",
    "instagram": "instagram:",
    "facebook": "facebook:",
    "x": "x:",
}
SOCIAL_CHANNELS = {"instagram", "facebook", "x", "telegram", "whatsapp"}


def infer_channels_from_handles(handles: list[str]) -> list[str]:
    discovered: list[str] = []
    for handle in handles:
        value = (handle or "").strip()
        lowered = value.lower()
        if lowered.startswith("whatsapp:+"):
            _append(discovered, "whatsapp")
            continue
        for name, prefix in CHANNEL_PREFIXES.items():
            if lowered.startswith(prefix):
                _append(discovered, name)
                break
        else:
            if value and ("@" in value or any(ch.isdigit() for ch in value)):
                _append(discovered, "imessage")
    return discovered


def _append(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def handle_for_channel(person: PersonRecord, channel_name: str) -> str | None:
    prefix = CHANNEL_PREFIXES.get(channel_name)
    for handle in person.handles:
        lowered = handle.lower()
        if prefix and lowered.startswith(prefix):
            return handle[len(prefix):]
        if channel_name == "whatsapp" and lowered.startswith("whatsapp:+"):
            return handle
        if channel_name == "imessage" and not any(lowered.startswith(f"{name}:") for name in CHANNEL_PREFIXES):
            return handle
    return None


def preferred_channels(person: PersonRecord) -> list[str]:
    connected = list(person.connected_channels or infer_channels_from_handles(person.handles))
    chain = connected + ["imessage", "whatsapp"]
    ordered: list[str] = []
    for name in chain:
        _append(ordered, name)
    return ordered


def route_message(person: PersonRecord, text: str) -> SendResult:
    last_error: str | None = None
    for name in preferred_channels(person):
        handle = handle_for_channel(person, name)
        if not handle:
            continue
        try:
            return get_channel(name).send(handle, text)
        except Exception as exc:
            last_error = str(exc)
    sms_handle = handle_for_channel(person, "imessage")
    if sms_handle:
        try:
            receipt = send_sms_via_twilio(sms_handle, text)
            return SendResult(
                ok=True,
                channel=receipt.channel,
                handle=sms_handle,
                message_id=receipt.provider_id,
            )
        except Exception as exc:
            last_error = str(exc)
    return SendResult(
        ok=False,
        channel="unavailable",
        handle=person.handles[0] if person.handles else person.person_id,
        error=last_error or "No available channel",
    )
