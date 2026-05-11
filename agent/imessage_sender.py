"""
imessage_sender.py — standalone iMessage send + Twilio SMS fallback.

Replaces PILK's `messages_send` tool.

Sends iMessages by driving Messages.app via AppleScript. Apple permits this with
Automation permission. The first send to a new contact prompts Aaron to allow
'Python -> Messages' in Privacy & Security → Automation.

If the AppleScript send fails (timeout, app not running, Automation denied,
network error), falls back to Twilio SMS — same content, real number-to-number.
Twilio creds from env: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER.
If no Twilio creds, raises SendUnavailable rather than silently dropping.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

_OSASCRIPT = shutil.which("osascript")


class SendUnavailable(RuntimeError):
    """Raised when neither iMessage nor Twilio can send."""


class SendError(RuntimeError):
    """Raised when a send attempt fails after all fallbacks."""


@dataclass
class SendReceipt:
    handle: str
    message: str
    channel: Literal["imessage", "sms"]
    sent_at: datetime
    provider_id: str | None = None


def send_imessage(handle: str, message: str, *, timeout: float = 12.0) -> SendReceipt:
    """Send via Messages.app. Raises SendError on any failure."""
    if not _OSASCRIPT:
        raise SendError("osascript not found (not on macOS?)")

    # AppleScript escape: backslashes and double quotes
    escaped = message.replace("\\", "\\\\").replace('"', '\\"')
    handle_escaped = handle.replace('"', '\\"')

    script = f'''
        tell application "Messages"
            set targetService to 1st service whose service type = iMessage
            set targetBuddy to buddy "{handle_escaped}" of targetService
            send "{escaped}" to targetBuddy
        end tell
    '''
    try:
        proc = subprocess.run(
            [_OSASCRIPT, "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise SendError(f"iMessage send timed out: {e}") from e

    if proc.returncode != 0:
        raise SendError(f"iMessage send failed: {proc.stderr.strip()}")

    return SendReceipt(
        handle=handle,
        message=message,
        channel="imessage",
        sent_at=datetime.now(UTC),
    )


def send_sms_via_twilio(handle: str, message: str) -> SendReceipt:
    """Send via Twilio SMS. Raises SendUnavailable if creds missing."""
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_FROM_NUMBER")
    if not (sid and token and from_number):
        raise SendUnavailable(
            "Twilio not configured. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
            "TWILIO_FROM_NUMBER to enable SMS fallback."
        )

    try:
        from twilio.rest import Client
    except ImportError as e:
        raise SendUnavailable("twilio package not installed") from e

    client = Client(sid, token)
    msg = client.messages.create(body=message, from_=from_number, to=handle)

    return SendReceipt(
        handle=handle,
        message=message,
        channel="sms",
        sent_at=datetime.now(UTC),
        provider_id=msg.sid,
    )


def send_with_fallback(handle: str, message: str) -> SendReceipt:
    """Try iMessage first, fall back to Twilio SMS, raise SendUnavailable if both fail."""
    try:
        return send_imessage(handle, message)
    except SendError as e:
        # Fall through to SMS
        try:
            return send_sms_via_twilio(handle, message)
        except SendUnavailable as sms_error:
            raise sms_error from e


def health_check() -> dict:
    """Return a structured status used by the CLI status command."""
    info = {
        "osascript_available": bool(_OSASCRIPT),
        "twilio_configured": bool(
            os.environ.get("TWILIO_ACCOUNT_SID")
            and os.environ.get("TWILIO_AUTH_TOKEN")
            and os.environ.get("TWILIO_FROM_NUMBER")
        ),
    }
    return info
