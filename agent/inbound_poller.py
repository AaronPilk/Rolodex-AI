from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Callable

from pydantic import BaseModel, Field

from agent.channels import get_channel
from agent.channels.base import ChannelMessage, NotConfigured
from agent.channels.meta_common import request_json
from agent.channels.x import _request_json as x_request_json
from agent.config import get_settings
from agent.connections import ConnectionStore
from agent.ingest import _merge_recent_messages
from agent.models import Channel, MessageSample, PersonRecord, RolodexStore
from agent.store import get_person_by_handle, load_store, store_path, store_transaction

log = logging.getLogger("rolodex.inbound")

_POLL_CHANNELS = ("telegram", "whatsapp", "instagram", "facebook", "x")
_HTTP_TIMEOUT = 12


class PollReport(BaseModel):
    channel_results: dict[str, dict[str, int | list[str]]] = Field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _message_time(message: ChannelMessage) -> datetime | None:
    return _parse_iso(message.sent_at)


def _normalize_handle(handle: str) -> str:
    return handle.strip()


def _synthetic_rowid(channel: str, message: ChannelMessage) -> int:
    identity = message.message_id or f"{message.handle}|{message.sent_at or ''}|{message.text}"
    digest = hashlib.sha1(f"{channel}|{identity}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False) & 0x7FFF_FFFF_FFFF_FFFF


def _channel_only_source(channel: str) -> str:
    return f"{channel}_only"


def _person_id_for_handle(channel: str, handle: str) -> str:
    safe = "".join(ch for ch in handle.strip().lower() if ch.isalnum()) or "unknown"
    return f"{channel}:{safe[:48]}"


def _display_name_for_handle(channel: str, handle: str) -> str:
    return f"{channel.title()} {handle}"


def _find_person_by_channel_handle(store: RolodexStore, channel_name: str, handle: str) -> PersonRecord | None:
    normalized = handle.strip().lower()
    for person in store.people:
        for existing in person.handles:
            if existing.strip().lower() == normalized:
                return person
        for existing in person.channels:
            if existing.type == channel_name and existing.handle.strip().lower() == normalized:
                return person
    return get_person_by_handle(store, handle)


def _ensure_person(store: RolodexStore, channel_name: str, handle: str) -> PersonRecord:
    person = _find_person_by_channel_handle(store, channel_name, handle)
    if person is not None:
        return person
    normalized = _normalize_handle(handle)
    person = PersonRecord(
        person_id=_person_id_for_handle(channel_name, normalized),
        display_name=_display_name_for_handle(channel_name, normalized),
        handles=[normalized],
        source=_channel_only_source(channel_name),
        connected_channels=[channel_name],
        channels=[Channel(type=channel_name, handle=normalized, chat_id=int(normalized) if normalized.isdigit() else None)],
        created_at=_now().isoformat(),
    )
    store.people.append(person)
    return person


def _ensure_channel_record(person: PersonRecord, channel_name: str, handle: str) -> Channel:
    for channel in person.channels:
        if channel.type == channel_name and channel.handle == handle:
            return channel
    channel = Channel(type=channel_name, handle=handle, chat_id=int(handle) if handle.isdigit() else None)
    person.channels.append(channel)
    return channel


def _apply_inbound_message(store: RolodexStore, channel_name: str, message: ChannelMessage) -> bool:
    handle = _normalize_handle(message.handle)
    person = _ensure_person(store, channel_name, handle)
    if handle not in person.handles:
        person.handles.append(handle)
    if channel_name not in person.connected_channels:
        person.connected_channels.append(channel_name)

    sample = MessageSample(
        rowid=_synthetic_rowid(channel_name, message),
        at=message.sent_at,
        direction="inbound",
        text=message.text or "",
        handle=handle,
        channel=channel_name,
    )
    merged = _merge_recent_messages(person.recent_messages, [sample])
    changed = len(merged) != len(person.recent_messages)
    person.recent_messages = merged
    if changed:
        person.inbound_message_count = int(person.inbound_message_count or 0) + 1
    if sample.at and (not person.last_message_at or (_parse_iso(sample.at) or datetime.min.replace(tzinfo=UTC)) >= (_parse_iso(person.last_message_at) or datetime.min.replace(tzinfo=UTC))):
        person.last_message_at = sample.at
        person.last_message_direction = "inbound"
    channel_record = _ensure_channel_record(person, channel_name, handle)
    if changed:
        channel_record.message_count = int(channel_record.message_count or 0) + 1
    if sample.at and (not channel_record.last_message_at or (_parse_iso(sample.at) or datetime.min.replace(tzinfo=UTC)) >= (_parse_iso(channel_record.last_message_at) or datetime.min.replace(tzinfo=UTC))):
        channel_record.last_message_at = sample.at
        channel_record.last_message_direction = "inbound"
    person.updated_at = _now().isoformat()
    return changed


async def _fetch_telegram_messages(store: RolodexStore) -> tuple[list[ChannelMessage], str | None]:
    channel = get_channel("telegram")
    if not channel.is_configured():
        raise NotConfigured("telegram not configured")
    from telegram import Bot

    offset = int(store.inbound_poll_offsets.get("telegram", "0") or "0")
    bot = Bot(token=os.environ["TELEGRAM_BOT_TOKEN"])
    updates = await bot.get_updates(
        offset=offset or None,
        limit=100,
        timeout=0,
        allowed_updates=["message"],
        read_timeout=12,
        write_timeout=12,
        connect_timeout=12,
        pool_timeout=12,
    )
    messages: list[ChannelMessage] = []
    next_offset = offset
    for update in updates:
        next_offset = max(next_offset, int(update.update_id) + 1)
        msg = getattr(update, "message", None)
        if not msg or getattr(msg.from_user, "is_bot", False):
            continue
        text = getattr(msg, "text", None) or getattr(msg, "caption", None) or ""
        if not text:
            continue
        handle = str(msg.chat_id)
        messages.append(
            ChannelMessage(
                handle=handle,
                text=text,
                direction="inbound",
                sent_at=msg.date.isoformat() if getattr(msg, "date", None) else None,
                message_id=str(msg.message_id),
                channel="telegram",
            )
        )
    return messages, str(next_offset) if next_offset else None


def _twilio_headers() -> dict[str, str]:
    token = base64.b64encode(
        f"{os.environ['TWILIO_ACCOUNT_SID']}:{os.environ['TWILIO_AUTH_TOKEN']}".encode("utf-8")
    ).decode("ascii")
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}


def _twilio_request(url: str) -> dict:
    request = urllib.request.Request(url, headers=_twilio_headers(), method="GET")
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw or "{}")


def _parse_twilio_datetime(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(UTC).isoformat()
    except Exception:
        return None


def _fetch_whatsapp_messages(store: RolodexStore) -> list[ChannelMessage]:
    channel = get_channel("whatsapp")
    if not channel.is_configured():
        raise NotConfigured("whatsapp not configured")
    since = _parse_iso(store.inbound_poll_state.get("whatsapp")) or (_now() - timedelta(days=2))
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    page_url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json?"
        f"PageSize=100&To={urllib.parse.quote(os.environ['TWILIO_FROM_NUMBER'])}"
    )
    messages: list[ChannelMessage] = []
    seen_pages = 0
    while page_url and seen_pages < 5:
        seen_pages += 1
        data = _twilio_request(page_url)
        for item in data.get("messages", []):
            if (item.get("direction") or "").lower() not in {"inbound", "inbound-api"}:
                continue
            sent_at = _parse_twilio_datetime(item.get("date_sent") or item.get("date_created"))
            sent_dt = _parse_iso(sent_at)
            if sent_dt is not None and sent_dt <= since:
                continue
            handle = str(item.get("from") or "")
            if not handle:
                continue
            messages.append(
                ChannelMessage(
                    handle=handle,
                    text=str(item.get("body") or ""),
                    direction="inbound",
                    sent_at=sent_at,
                    message_id=str(item.get("sid") or ""),
                    channel="whatsapp",
                    raw=item,
                )
            )
        next_page = data.get("next_page_uri")
        page_url = f"https://api.twilio.com{next_page}" if next_page else ""
    return messages


def _fetch_meta_messages(channel_name: str, platform: str, account_env: str) -> list[ChannelMessage]:
    channel = get_channel(channel_name)
    if not channel.is_configured():
        raise NotConfigured(f"{channel_name} not configured")
    token = os.environ["META_PAGE_ACCESS_TOKEN"] if channel_name == "instagram" else os.environ["META_FB_PAGE_ACCESS_TOKEN"]
    account_id = os.environ.get(account_env) or "me"
    url = (
        f"https://graph.facebook.com/v19.0/{account_id}/conversations?"
        + urllib.parse.urlencode(
            {
                "access_token": token,
                "platform": platform,
                "fields": "participants,messages.limit(20){message,created_time,from,id}",
                "limit": "50",
            }
        )
    )
    data = request_json(url)
    messages: list[ChannelMessage] = []
    for convo in data.get("data", []):
        participants = convo.get("participants", {}).get("data", [])
        participant_ids = [str(item.get("id") or "") for item in participants if item.get("id")]
        for item in convo.get("messages", {}).get("data", []):
            sender_id = str(item.get("from", {}).get("id") or "")
            if not sender_id or sender_id not in participant_ids:
                continue
            text = str(item.get("message") or "")
            if not text:
                continue
            messages.append(
                ChannelMessage(
                    handle=sender_id,
                    text=text,
                    direction="inbound",
                    sent_at=item.get("created_time"),
                    message_id=str(item.get("id") or ""),
                    channel=channel_name,
                    raw=item,
                )
            )
    return messages


def _fetch_x_messages(store: RolodexStore) -> list[ChannelMessage]:
    channel = get_channel("x")
    if not channel.is_configured():
        raise NotConfigured("x not configured")
    since = _parse_iso(store.inbound_poll_state.get("x")) or (_now() - timedelta(days=2))
    next_token: str | None = None
    messages: list[ChannelMessage] = []
    for _ in range(5):
        params = {
            "event_types": "MessageCreate",
            "max_results": "100",
        }
        if next_token:
            params["pagination_token"] = next_token
        url = "https://api.x.com/2/dm_events?" + urllib.parse.urlencode(params)
        data = x_request_json("GET", url)
        stop = False
        for item in data.get("data", []):
            sent_at = item.get("created_at")
            sent_dt = _parse_iso(sent_at)
            if sent_dt is not None and sent_dt <= since:
                stop = True
                continue
            text = str(item.get("text") or "")
            sender_id = str(item.get("sender_id") or item.get("dm_conversation_id") or "")
            if not text or not sender_id:
                continue
            messages.append(
                ChannelMessage(
                    handle=sender_id,
                    text=text,
                    direction="inbound",
                    sent_at=sent_at,
                    message_id=str(item.get("id") or ""),
                    channel="x",
                    raw=item,
                )
            )
        next_token = data.get("meta", {}).get("next_token")
        if stop or not next_token:
            break
    return messages


def _filter_new_messages(messages: list[ChannelMessage], since: datetime | None) -> list[ChannelMessage]:
    if since is None:
        return messages
    filtered: list[ChannelMessage] = []
    for message in messages:
        sent_at = _message_time(message)
        if sent_at is None or sent_at > since:
            filtered.append(message)
    return filtered


async def _poll_channel(
    channel_name: str,
    store: RolodexStore,
    *,
    dry_run: bool = False,
) -> dict[str, int | list[str]]:
    result: dict[str, int | list[str]] = {"messages_pulled": 0, "errors": []}
    last_polled_at = store.inbound_poll_state.get(channel_name)
    since = _parse_iso(last_polled_at)
    try:
        messages: list[ChannelMessage]
        next_offset: str | None = None
        if channel_name == "telegram":
            messages, next_offset = await _fetch_telegram_messages(store)
        elif channel_name == "whatsapp":
            messages = _fetch_whatsapp_messages(store)
        elif channel_name == "instagram":
            messages = _fetch_meta_messages("instagram", "instagram", "META_IG_BUSINESS_ID")
        elif channel_name == "facebook":
            messages = _fetch_meta_messages("facebook", "messenger", "META_FB_PAGE_ID")
        elif channel_name == "x":
            messages = _fetch_x_messages(store)
        else:
            messages = []
        if channel_name != "telegram":
            messages = _filter_new_messages(messages, since)
        pulled = 0
        for message in sorted(messages, key=lambda item: item.sent_at or ""):
            if (message.direction or "").lower() != "inbound":
                continue
            if _apply_inbound_message(store, channel_name, message):
                pulled += 1
        result["messages_pulled"] = pulled
        now_iso = _now().isoformat()
        store.inbound_poll_state[channel_name] = now_iso
        if next_offset:
            store.inbound_poll_offsets[channel_name] = next_offset
        store.inbound_poll_status[channel_name] = {
            "last_polled_at": now_iso,
            "messages_last_pull": pulled,
            "last_error": None,
        }
    except NotConfigured:
        return result
    except Exception as exc:
        log.warning("inbound poll failed for %s: %s", channel_name, exc)
        result["errors"] = [str(exc)]
        existing = dict(store.inbound_poll_status.get(channel_name, {}))
        existing.update(
            {
                "last_polled_at": store.inbound_poll_state.get(channel_name),
                "messages_last_pull": int(existing.get("messages_last_pull") or 0),
                "last_error": str(exc),
            }
        )
        store.inbound_poll_status[channel_name] = existing
    if dry_run:
        return result
    return result


async def poll_all_channels(
    *,
    settings=None,
    store: RolodexStore | None = None,
    dry_run: bool = False,
) -> PollReport:
    settings = settings or get_settings()
    ConnectionStore().apply_to_env()
    report = PollReport()
    if store is not None:
        for channel_name in _POLL_CHANNELS:
            report.channel_results[channel_name] = await _poll_channel(channel_name, store, dry_run=dry_run)
        return report

    path = store_path(settings)
    if dry_run:
        loaded = load_store(path)
        for channel_name in _POLL_CHANNELS:
            report.channel_results[channel_name] = await _poll_channel(channel_name, loaded, dry_run=True)
        return report

    with store_transaction(path) as persisted:
        for channel_name in _POLL_CHANNELS:
            report.channel_results[channel_name] = await _poll_channel(channel_name, persisted)
    return report


def format_report(report: PollReport) -> str:
    lines = ["Rolodex inbound poll"]
    for channel_name, result in report.channel_results.items():
        errors = list(result.get("errors", [])) if isinstance(result.get("errors"), list) else []
        pulled = int(result.get("messages_pulled", 0) or 0)
        suffix = f" | errors={'; '.join(errors)}" if errors else ""
        lines.append(f"- {channel_name}: messages_pulled={pulled}{suffix}")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agent poll-inbound")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = asyncio.run(poll_all_channels(dry_run=bool(args.dry_run)))
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
