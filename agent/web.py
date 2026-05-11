from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import webbrowser
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
import uvicorn

from agent.config import get_settings
from agent.channels import available_channels, channel_health, get_channel
from agent.channels.base import NotConfigured
from agent.channels.dispatcher import handle_for_channel, infer_channels_from_handles, route_message
from agent.channels.meta_common import instructions_url, is_meta_capability_error
from agent.connections import CHANNEL_SCHEMA, TEST_MESSAGE_TEXT, ConnectionStore, channel_keys, channel_schema
from agent.daemon import daily_run
from agent.inbound_poller import poll_all_channels
from agent.llm_client import draft as llm_draft
from agent.draft import generate_draft
from agent.ingest import _configured_user_last_name, _deterministic_relationship_hash
from agent.models import DraftBundle, MessageSample, PersonRecord, RolodexStore
from agent.ops import append_audit_entry, audit_log_path, collect_health, tail_audit_entries
from agent.person_utils import display_name, effective_relationship_class, format_handle_label, is_manually_overridden
from agent.relationship_signals import infer_relationship
from agent.scoring import active_tier, compute_priority
from agent.store import _encrypted_path, decrypt_store_to_text, load_store, store_path, store_transaction
from agent.scheduler import RolodexScheduler, get_active_scheduler, next_digest_fire_at

APP_HTML_PATH = Path(__file__).resolve().parent.parent / "app.html"
README_PATH = Path(__file__).resolve().parent.parent / "README.md"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
ACTION_LLM_TIMEOUT_SECONDS = 12.0
ACTION_DEFAULT_MAX_TARGETS = 25
ACTION_SEND_BATCH_SIZE = 5
SPAM_RELATIONSHIP_CLASS = "spam_or_verification"
ROI_CACHE_TTL_SECONDS = 300
BUSINESS_PIPELINE_CLASSES = {"business", "professional", "client", "mentor", "mentee", "service_provider"}
ROI_METHOD_NOTE = "Pipeline estimates assume each business reconnect represents a $5K-$25K potential deal."
_ROI_CACHE_LOCK = threading.Lock()
_ROI_CACHE: dict[str, Any] = {
    "expires_at": datetime.fromtimestamp(0, tz=UTC),
    "audit_path": None,
    "audit_mtime_ns": None,
    "value": None,
}


class AskActionPayload(BaseModel):
    instruction: str
    dry_run: bool = True
    max_targets: int = ACTION_DEFAULT_MAX_TARGETS


class AnnotationPayload(BaseModel):
    user_note: str | None = None
    user_override_class: str | None = None
    user_override_tier: str | None = None
    user_priority_boost: int | None = None
    do_not_contact: bool = False
    instagram_username: str | None = None
    facebook_handle: str | None = None
    twitter_handle: str | None = None
    linkedin_url: str | None = None
    snapchat_username: str | None = None
    tiktok_handle: str | None = None
    how_we_met: str | None = None
    onboarding_reviewed: bool | None = None


class AskPayload(BaseModel):
    query: str


class SendPayload(BaseModel):
    text: str
    channel: str | None = None
    # Optional — when present we compare to the actual sent text and record
    # an `edited` rating in the tone feedback log. This is the lifeblood of
    # voice learning: every time the user tweaks a draft before sending,
    # the next draft for that person tightens to their real voice.
    original_draft: str | None = None


class FeedbackPayload(BaseModel):
    """Operator feedback on a draft — Skip / Off / Sounds-like-me."""
    rating: str  # "off" | "sounds_like_me" | "edited"
    draft: str
    edit_diff: str | None = None


class RegenerateDraftPayload(BaseModel):
    reason: str = "post-feedback"


class ConnectionSavePayload(BaseModel):
    credentials: dict[str, str] = {}
    test: bool = True


class MetaReplyPayload(BaseModel):
    participant_id: str
    text: str


class MetaSendTestPayload(BaseModel):
    handle: str
    text: str | None = None


META_DM_FIRST_HINT = (
    "Meta requires you to send the Page a DM first from your personal IG before this endpoint returns results. "
    "If you've done that, wait 30 seconds and refresh."
)


def _today() -> date:
    return datetime.now(UTC).date()


def _safe_last_contact(person: PersonRecord) -> str | None:
    value = person.last_contacted or person.last_message_at
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        return value


def _latest_run_id(store: RolodexStore) -> str | None:
    if not store.digests:
        return None
    return max(store.digests)


def _drafts_by_person(store: RolodexStore) -> dict[str, dict]:
    run_id = _latest_run_id(store)
    out: dict[str, dict] = {}
    if not run_id:
        return out
    for candidate in store.digests.get(run_id, []):
        bundle_key = f"{run_id}:{candidate.person_id}"
        bundle: DraftBundle | None = store.drafts.get(bundle_key)
        out[candidate.person_id] = {
            "run_id": run_id,
            "reason": candidate.reason,
            "relationship_class": candidate.relationship_class,
            "draft_preview": candidate.draft_preview or (bundle.top_draft if bundle else None),
            "alternates": list(bundle.alternates if bundle else []),
        }
    return out


def _serialize_person_summary(person: PersonRecord, settings, draft_map: dict[str, dict]) -> dict:
    score = compute_priority(person, settings, _today())
    draft = draft_map.get(person.person_id, {})
    now = datetime.now(UTC)
    snooze_until = person.cadence.snooze_until
    is_snoozed = False
    if snooze_until:
        try:
            is_snoozed = datetime.fromisoformat(snooze_until) > now
        except ValueError:
            is_snoozed = False
    return {
        "person_id": person.person_id,
        "name": display_name(person),
        "phone_or_handle": format_handle_label(person.handles[0]) if person.handles else person.person_id,
        "relationship_class": effective_relationship_class(person) or "unknown",
        "tier": active_tier(person),
        "last_contacted": _safe_last_contact(person),
        "priority_score": score or 0.0,
        "is_overdue": bool(person.cadence.is_overdue),
        "snooze_until": snooze_until,
        "is_snoozed": is_snoozed,
        "contact_organization": person.contact_organization,
        "contact_tags": list(person.contact_tags),
        "source": person.source,
        "connected_channels": list(person.connected_channels or infer_channels_from_handles(person.handles)),
        "draft_available": bool(draft.get("draft_preview")),
        "context_summary": person.context_summary,
        "topics": list(person.topics),
        "user_note": person.user_note,
        "how_we_met": person.how_we_met,
        "user_override_class": person.user_override_class,
        "user_override_tier": person.user_override_tier,
        "user_priority_boost": person.user_priority_boost,
        "do_not_contact": person.do_not_contact,
        "instagram_username": person.instagram_username,
        "facebook_handle": person.facebook_handle,
        "twitter_handle": person.twitter_handle,
        "linkedin_url": person.linkedin_url,
        "snapchat_username": person.snapchat_username,
        "tiktok_handle": person.tiktok_handle,
        "onboarding_reviewed": bool(person.onboarding_reviewed),
        "onboarding_reviewed_at": person.onboarding_reviewed_at.isoformat() if person.onboarding_reviewed_at else None,
        "manually_set": is_manually_overridden(person),
        "message_count": int(person.inbound_message_count or 0) + int(person.outbound_message_count or 0),
        "outbound_message_count": int(person.outbound_message_count or 0),
        "inbound_message_count": int(person.inbound_message_count or 0),
    }


def _serialize_people() -> dict:
    settings = get_settings()
    store = load_store(store_path(settings))
    draft_map = _drafts_by_person(store)
    # Exclude spam_or_verification (OTC codes from Apple/Google/banks etc.) and
    # any DNC'd person from the default UI list. They clutter the dashboard
    # and the user almost never wants to see them. Ask PILK can still find
    # them if explicitly requested.
    excluded_classes = {"spam_or_verification"}
    visible_people_records = [
        person for person in store.people
        if not (person.user_override_class or person.relationship_class or "").lower() in excluded_classes
    ]
    people = [_serialize_person_summary(person, settings, draft_map) for person in visible_people_records]
    people.sort(key=lambda item: item["priority_score"], reverse=True)
    buckets: dict[str, list[dict]] = {}
    for item in people:
        buckets.setdefault(item["relationship_class"], []).append(item)
    counts = {
        "due_now": sum(1 for item in people if item["is_overdue"] and item["draft_available"]),
        "all_people": len(people),
        "snoozed": sum(1 for item in people if item["is_snoozed"]),
        "tiers": {f"T{i}": sum(1 for item in people if item["tier"] == f"T{i}") for i in range(1, 6)},
        "hidden_spam": len(store.people) - len(people),
    }
    return {
        "total_people": len(people),
        "latest_sync": store.last_sync_at,
        "people": people,
        "buckets": [{"bucket": name, "count": len(items), "people": items} for name, items in buckets.items()],
        "counts": counts,
        "inbound_activity": _serialize_inbound_activity(store),
    }


def _serialize_digest() -> dict:
    settings = get_settings()
    store = load_store(store_path(settings))
    run_id = _latest_run_id(store)
    draft_map = _drafts_by_person(store)
    candidates = []
    if run_id:
        for candidate in store.digests.get(run_id, []):
            payload = draft_map.get(candidate.person_id, {})
            candidates.append(
                {
                    "person_id": candidate.person_id,
                    "display_name": candidate.display_name,
                    "relationship_class": candidate.relationship_class,
                    "reason": candidate.reason,
                    "priority": candidate.priority,
                    "due_days": candidate.due_days,
                    "draft_preview": payload.get("draft_preview") or candidate.draft_preview,
                    "alternates": payload.get("alternates", []),
                }
            )
    return {"run_id": run_id, "candidates": candidates, "drafts_by_person_id": draft_map}


def _serialize_person_detail(person_id: str) -> dict:
    settings = get_settings()
    store = load_store(store_path(settings))
    draft_map = _drafts_by_person(store)
    for person in store.people:
        if person.person_id != person_id:
            continue
        summary = _serialize_person_summary(person, settings, draft_map)
        # recent_messages is stored newest-first; take the first 250 → newest 250.
        # Bumped from 50 — the smaller window made LLM context summaries miss
        # the full picture (e.g. classified mom-who-also-does-business as
        # purely "client / business partner" because only recent business
        # messages were visible).
        summary["recent_messages"] = [
            {
                "direction": message.direction,
                "text": message.text,
                "at": message.at,
                "channel": message.channel,
            }
            for message in person.recent_messages[:250]
        ]
        summary["draft"] = draft_map.get(person.person_id)
        return summary
    raise HTTPException(status_code=404, detail="Person not found")


def _top_recent_messages(person: PersonRecord, limit: int = 15) -> list[dict[str, str | None]]:
    recent = list(person.recent_messages[:limit])
    def _message_sort_key(message: MessageSample) -> tuple[int, str]:
        if not message.at:
            return (1, "")
        try:
            return (0, datetime.fromisoformat(message.at).isoformat())
        except ValueError:
            return (0, message.at)

    recent.sort(key=_message_sort_key)
    return [
        {
            "direction": message.direction,
            "text": message.text,
            "at": message.at,
            "channel": message.channel,
        }
        for message in recent
    ]


def _is_implicitly_onboarded(person: PersonRecord) -> bool:
    return bool(person.first_name and person.last_name and (person.user_note or "").strip())


def _onboarding_candidate_people(store: RolodexStore) -> list[PersonRecord]:
    candidates: list[PersonRecord] = []
    for person in store.people:
        active_class = (effective_relationship_class(person) or "").strip().lower()
        if active_class == SPAM_RELATIONSHIP_CLASS:
            continue
        if person.do_not_contact:
            continue
        if _is_implicitly_onboarded(person):
            continue
        candidates.append(person)
    return candidates


def _onboarding_priority_segment(person: PersonRecord) -> str:
    active_class = (effective_relationship_class(person) or "").strip().lower()
    tier = active_tier(person)
    message_count = int(person.inbound_message_count or 0) + int(person.outbound_message_count or 0)
    if active_class == "family":
        return "family"
    if tier == "T1":
        return "tier1"
    if tier == "T2":
        return "tier2"
    if message_count >= 50:
        return "high_message_volume"
    return "other"


def _onboarding_priority_rank(person: PersonRecord, settings) -> tuple[int, float]:
    segment_order = {
        "family": 0,
        "tier1": 1,
        "tier2": 1,
        "high_message_volume": 2,
        "other": 3,
    }
    segment = _onboarding_priority_segment(person)
    priority = compute_priority(person, settings, _today()) or 0.0
    return (segment_order.get(segment, 3), -priority)


def _serialize_onboarding_queue(limit: int = 20) -> dict:
    settings = get_settings()
    store = load_store(store_path(settings))
    draft_map = _drafts_by_person(store)
    queued = [person for person in _onboarding_candidate_people(store) if not person.onboarding_reviewed]
    queued.sort(key=lambda person: _onboarding_priority_rank(person, settings))
    items: list[dict[str, object]] = []
    for person in queued[: max(1, min(int(limit or 20), 200))]:
        summary = _serialize_person_summary(person, settings, draft_map)
        inference = infer_relationship(person, store, _configured_user_last_name(settings))
        summary["recent_messages"] = _top_recent_messages(person, limit=15)
        summary["suggested_class"] = inference.label
        summary["suggested_class_reasoning"] = inference.reasoning
        items.append(summary)
    return {"people": items, "items": items, "count": len(items)}


def _serialize_onboarding_progress() -> dict:
    payload = onboarding_progress_snapshot()
    payload.pop("breakdown_by_class", None)
    return payload


def onboarding_progress_snapshot(settings=None) -> dict:
    settings = settings or get_settings()
    store = load_store(store_path(settings))
    candidates = _onboarding_candidate_people(store)
    reviewed = [person for person in candidates if person.onboarding_reviewed]
    remaining_priority_segments = {
        "family": 0,
        "tier1": 0,
        "tier2": 0,
        "high_message_volume": 0,
        "other": 0,
    }
    for person in candidates:
        if person.onboarding_reviewed:
            continue
        remaining_priority_segments[_onboarding_priority_segment(person)] += 1
    breakdown_by_class: dict[str, int] = {}
    for person in reviewed:
        label = effective_relationship_class(person) or "unknown"
        breakdown_by_class[label] = breakdown_by_class.get(label, 0) + 1
    total = len(candidates)
    reviewed_count = len(reviewed)
    return {
        "reviewed": reviewed_count,
        "total": total,
        "percent": round((reviewed_count / total) * 100, 2) if total else 100.0,
        "remaining_priority_segments": remaining_priority_segments,
        "breakdown_by_class": dict(sorted(breakdown_by_class.items())),
    }


def _serialize_audit() -> dict:
    entries = tail_audit_entries(get_settings(), count=200)
    sent = [entry for entry in entries if entry.get("action") == "send_succeeded"]
    return {"entries": entries, "sent_entries": sent}


def _load_audit_entries(settings) -> list[dict[str, object]]:
    path = audit_log_path(settings)
    if not path.exists():
        return []
    out: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _serialize_roi() -> dict[str, object]:
    settings = get_settings()
    audit_path = audit_log_path(settings)
    audit_mtime_ns = audit_path.stat().st_mtime_ns if audit_path.exists() else -1
    now = datetime.now(UTC)
    with _ROI_CACHE_LOCK:
        cached = _ROI_CACHE.get("value")
        if (
            cached is not None
            and _ROI_CACHE.get("audit_path") == str(audit_path)
            and _ROI_CACHE.get("audit_mtime_ns") == audit_mtime_ns
            and isinstance(_ROI_CACHE.get("expires_at"), datetime)
            and _ROI_CACHE["expires_at"] > now
        ):
            return cached

    store = load_store(store_path(settings))
    people_by_id = {person.person_id: person for person in store.people}
    entries = [
        entry for entry in _load_audit_entries(settings)
        if entry.get("action") == "send_succeeded" and entry.get("person_id")
    ]
    send_events: list[tuple[datetime, dict[str, object], PersonRecord]] = []
    for entry in entries:
        ts = _parse_timestamp(str(entry.get("ts") or ""))
        person = people_by_id.get(str(entry.get("person_id")))
        if ts is None or person is None:
            continue
        send_events.append((ts, entry, person))
    send_events.sort(key=lambda item: item[0])

    cutoff_30 = now - timedelta(days=30)
    cutoff_90 = now - timedelta(days=90)
    cutoff_180 = now - timedelta(days=180)
    person_history = {
        person.person_id: sorted(
            [
                message_ts
                for message in person.recent_messages
                if (message_ts := _parse_timestamp(message.at)) is not None
            ]
        )
        for person in store.people
    }

    reconnects_30d_people: set[str] = set()
    reconnects_90d_people: set[str] = set()
    dormant_warmed_people: set[str] = set()
    pipeline_people: set[str] = set()
    reconnects_30d_series = [0] * 30
    last_seen_before_send: dict[str, datetime | None] = {}

    for ts, _entry, person in send_events:
        known_history = person_history.get(person.person_id, [])
        previous_contact = last_seen_before_send.get(person.person_id)
        if previous_contact is None:
            earlier = [seen for seen in known_history if seen < ts]
            previous_contact = earlier[-1] if earlier else None

        gap_days = (ts - previous_contact).days if previous_contact is not None else None
        if gap_days is not None and gap_days > 90:
            if ts >= cutoff_90:
                reconnects_90d_people.add(person.person_id)
                if (effective_relationship_class(person) or "unknown").lower() in BUSINESS_PIPELINE_CLASSES:
                    pipeline_people.add(person.person_id)
            if ts >= cutoff_30:
                reconnects_30d_people.add(person.person_id)
                day_index = max(0, min(29, (ts.date() - cutoff_30.date()).days))
                reconnects_30d_series[day_index] += 1
        if gap_days is not None and gap_days > 180 and ts >= cutoff_30:
            current_last_contact = _parse_timestamp(person.last_contacted or person.last_message_at)
            if current_last_contact is not None and current_last_contact >= cutoff_30:
                dormant_warmed_people.add(person.person_id)

        last_seen_before_send[person.person_id] = ts

    active_relationships = 0
    tier_breakdown: dict[str, int] = {}
    for person in store.people:
        last_contact = _parse_timestamp(person.last_contacted or person.last_message_at)
        if last_contact is None or last_contact < cutoff_30:
            continue
        active_relationships += 1
        tier_breakdown[f"{active_tier(person)}_active"] = tier_breakdown.get(f"{active_tier(person)}_active", 0) + 1

    total_sends_30d = sum(1 for ts, _entry, _person in send_events if ts >= cutoff_30)
    total_sends_90d = sum(1 for ts, _entry, _person in send_events if ts >= cutoff_90)
    payload = {
        "reconnects_30d": len(reconnects_30d_people),
        "reconnects_90d": len(reconnects_90d_people),
        "active_relationships": active_relationships,
        "dormant_warmed": len(dormant_warmed_people),
        "total_sends_30d": total_sends_30d,
        "total_sends_90d": total_sends_90d,
        "tier_breakdown": dict(sorted(tier_breakdown.items())),
        "estimated_pipeline_value_low": len(pipeline_people) * 5_000,
        "estimated_pipeline_value_high": len(pipeline_people) * 25_000,
        "method_note": ROI_METHOD_NOTE,
        "reconnects_30d_series": reconnects_30d_series,
    }
    with _ROI_CACHE_LOCK:
        _ROI_CACHE.update(
            {
                "expires_at": now + timedelta(seconds=ROI_CACHE_TTL_SECONDS),
                "audit_path": str(audit_path),
                "audit_mtime_ns": audit_mtime_ns,
                "value": payload,
            }
        )
    return payload


def _format_relative_duration(target: datetime, now: datetime) -> str:
    delta = target - now
    total_minutes = max(0, int(delta.total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"in {hours}h {minutes}m"
    if hours:
        return f"in {hours}h"
    return f"in {minutes}m"


def _telegram_target_label() -> str:
    try:
        detail = get_channel("telegram").health_check().detail or ""
    except Exception:
        detail = ""
    if "@" in detail:
        return f"Telegram ({detail.split()[-1]})"
    return "Telegram"


def _serialize_inbound_poll_status(store: RolodexStore | None = None) -> dict[str, dict[str, object]]:
    settings = get_settings()
    store = store or load_store(store_path(settings))
    payload: dict[str, dict[str, object]] = {}
    for channel_name in ("telegram", "whatsapp", "instagram", "facebook", "x"):
        status = dict(store.inbound_poll_status.get(channel_name, {}))
        payload[channel_name] = {
            "last_polled_at": status.get("last_polled_at") or store.inbound_poll_state.get(channel_name),
            "messages_last_pull": int(status.get("messages_last_pull") or 0),
            "last_error": status.get("last_error"),
        }
    return payload


def _serialize_inbound_activity(store: RolodexStore | None = None, *, limit: int = 10) -> list[dict[str, object]]:
    settings = get_settings()
    store = store or load_store(store_path(settings))
    items: list[dict[str, object]] = []
    for person in store.people:
        for message in person.recent_messages:
            if message.direction != "inbound":
                continue
            if not (message.text or "").strip():
                continue
            items.append(
                {
                    "person_id": person.person_id,
                    "sender_name": display_name(person),
                    "channel": message.channel or "unknown",
                    "preview": (message.text or "").strip()[:80],
                    "at": message.at,
                }
            )
    items.sort(key=lambda item: item.get("at") or "", reverse=True)
    return items[:limit]


def _serialize_settings() -> dict:
    settings = get_settings()
    health = collect_health(settings)
    channels = _serialize_channels()
    timezone = os.environ.get("ROLODEX_TIMEZONE", "America/New_York")
    now = datetime.now(ZoneInfo(timezone))
    next_fire = next_digest_fire_at(now=now, timezone=timezone)
    return {
        "send_cap": health.cap,
        "schedule": {
            "cron": os.environ.get("ROLODEX_DIGEST_CRON", "0 9 * * *"),
            "timezone": timezone,
            "label": "Daily at 9:00 AM local",
            "next_fire_at": next_fire.isoformat(),
            "next_fire_label": next_fire.strftime("%-I:%M %p"),
            "next_fire_relative": _format_relative_duration(next_fire, now),
        },
        "daily_push": {
            "send_to": _telegram_target_label(),
        },
        "env_status": {
            "anthropic_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "twilio_account_sid": bool(os.environ.get("TWILIO_ACCOUNT_SID")),
            "twilio_auth_token": bool(os.environ.get("TWILIO_AUTH_TOKEN")),
            "twilio_from_number": bool(os.environ.get("TWILIO_FROM_NUMBER")),
        },
        "channels": channels,
        "inbound_poll_status": _serialize_inbound_poll_status(),
        "encryption": {
            "encrypted_store_present": health.encrypted_store_present,
            "keychain_accessible": health.keychain_accessible,
            "store_path": str(store_path(settings)),
            "encrypted_path": str(_encrypted_path(store_path(settings))),
        },
        "readme_url": "/README.md",
        "decrypt_url": "/api/decrypt",
    }


def _serialize_channels() -> dict[str, dict[str, object]]:
    payload: dict[str, dict[str, object]] = {}
    health_map = channel_health()
    for name in available_channels():
        item = health_map[name]
        url = item.instructions_url
        if not url:
            url = instructions_url(get_channel(name).connect_instructions())
        payload[name] = {
            "configured": item.configured,
            "healthy": item.healthy,
            "instructions_url": url,
        }
    return payload


def _get_connection_store() -> ConnectionStore:
    return ConnectionStore()


def _require_connection_channel(channel: str) -> str:
    key = channel.strip().lower()
    if key not in CHANNEL_SCHEMA:
        raise HTTPException(status_code=404, detail=f"Unknown channel: {channel}")
    return key


def _require_meta_channel(channel: str) -> str:
    key = _require_connection_channel(channel)
    if key not in {"instagram", "facebook"}:
        raise HTTPException(status_code=404, detail=f"Unknown Meta channel: {channel}")
    return key


def _meta_channel_or_error(channel: str):
    _get_connection_store().apply_to_env()
    adapter = get_channel(channel)
    if not adapter.is_configured():
        return None, {"ok": False, "channel": channel, "error": "channel not configured"}
    return adapter, None


def _serialize_connections() -> dict[str, dict[str, object]]:
    store = _get_connection_store()
    store.apply_to_env()
    payload: dict[str, dict[str, object]] = {}
    for name in available_channels():
        schema = channel_schema(name)
        health = get_channel(name).health_check()
        payload[name] = {
            "configured": health.configured,
            "healthy": health.healthy,
            "detail": health.detail,
            "credentials": store.list_credentials(name),
            "required_keys": list(schema["required_keys"]),
            "optional_keys": list(schema["optional_keys"]),
            "instructions_md": schema["instructions_md"],
            "human_name": schema["human_name"],
            "color": schema["color"],
        }
    return payload


def _clear_connection_env(channel: str) -> None:
    for key in channel_keys(channel):
        os.environ.pop(key, None)


def _channel_health_payload(channel: str) -> dict[str, object]:
    _get_connection_store().apply_to_env()
    health = get_channel(channel).health_check()
    payload: dict[str, object] = {
        "ok": health.healthy,
        "configured": health.configured,
        "healthy": health.healthy,
    }
    if health.healthy:
        payload["message"] = health.detail or f"{channel_schema(channel)['human_name']} connected"
    else:
        payload["error"] = health.detail or f"{channel_schema(channel)['human_name']} health check failed"
    return payload


def _run_connection_test(channel: str) -> dict[str, object]:
    store = _get_connection_store()
    store.apply_to_env()
    health_payload = _channel_health_payload(channel)
    if not health_payload["configured"] or not health_payload["healthy"]:
        return health_payload

    if channel == "telegram":
        chat_id = store.get_credential(channel, "TELEGRAM_CHAT_ID")
        if not chat_id:
            return {
                "ok": False,
                "configured": True,
                "healthy": True,
                "error": "Saved, but TELEGRAM_CHAT_ID is required to send a Telegram test message",
            }
        result = get_channel(channel).send(chat_id, TEST_MESSAGE_TEXT)
        return {
            "ok": result.ok,
            "configured": True,
            "healthy": True,
            "message": f"Sent test message to {chat_id}",
        }

    if channel == "whatsapp":
        handle = store.get_credential(channel, "TWILIO_TEST_TO_NUMBER")
        if handle:
            result = get_channel(channel).send(handle, TEST_MESSAGE_TEXT)
            return {
                "ok": result.ok,
                "configured": True,
                "healthy": True,
                "message": f"Sent test WhatsApp message to {handle}",
            }
        return {
            "ok": True,
            "configured": True,
            "healthy": True,
            "message": "Twilio credentials validated. Add TWILIO_TEST_TO_NUMBER to enable one-click test sends.",
        }

    return {
        "ok": True,
        "configured": True,
        "healthy": True,
        "message": str(health_payload.get("message") or f"{channel_schema(channel)['human_name']} connection verified"),
    }


def _maybe_apply_note_classification(person: PersonRecord, store: RolodexStore, settings) -> dict[str, object] | None:
    if person.user_override_class:
        return None
    inference = infer_relationship(person, store, _configured_user_last_name(settings))
    if not inference.label or inference.confidence < 0.8:
        return None
    changed = False
    if person.relationship_class != inference.label:
        person.relationship_class = inference.label
        changed = True
    deterministic_hash = _deterministic_relationship_hash(person, inference.rule_id)
    if person.relationship_classification_hash != deterministic_hash:
        person.relationship_classification_hash = deterministic_hash
        changed = True
    if inference.rule_id == "operator_note_business_partner" and "business_partner" not in person.contact_tags:
        person.contact_tags = [*person.contact_tags, "business_partner"]
        changed = True
    if changed:
        now = datetime.now(UTC)
        person.relationship_classified_at = now
        person.updated_at = now.isoformat()
    return {
        "label": inference.label,
        "confidence": inference.confidence,
        "rule_id": inference.rule_id,
        "applied": changed,
    }


def _message_word_count(text: str) -> int:
    return len([token for token in re.split(r"\s+", text.strip()) if token])


def _is_name_like_text(text: str) -> bool:
    cleaned = re.sub(r"[^A-Za-z\s']", " ", text).strip()
    if not cleaned:
        return False
    if re.fullmatch(r"(?:[A-Z][a-z]+|[a-z]+)(?:\s+(?:[A-Z][a-z]+|[a-z]+)){0,2}", cleaned):
        return True
    if re.search(r"\b(?:i am|i'm|this is|it's|its)\s+[A-Za-z][A-Za-z']+(?:\s+[A-Za-z][A-Za-z']+)?\b", text, re.IGNORECASE):
        return True
    return False


def _looks_like_name_exchange(person: PersonRecord) -> bool:
    total_messages = int(person.inbound_message_count or 0) + int(person.outbound_message_count or 0)
    if total_messages < 2 or total_messages > 8:
        return False
    recent = [message for message in person.recent_messages[:8] if (message.text or "").strip()]
    if len(recent) < 2:
        return False
    qualifying = 0
    for message in recent:
        text = (message.text or "").strip()
        if _message_word_count(text) <= 8 and _is_name_like_text(text):
            qualifying += 1
    return qualifying >= 2 and qualifying >= max(2, len(recent) // 2)


def _parse_days_ago(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return (_today() - datetime.fromisoformat(value).date()).days
    except ValueError:
        return None


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _coerce_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]


def _coerce_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    return None


def _normalized_source_tokens(person: PersonRecord) -> set[str]:
    src = (person.source or "").strip().lower()
    tokens = {src} if src else set()
    if src == "contacts_only":
        tokens.add("contacts")
    if "+" in src:
        tokens.update(part.strip() for part in src.split("+") if part.strip())
    return tokens


def _has_self_intro_pattern(person: PersonRecord) -> bool:
    recent = [message for message in person.recent_messages[:12] if (message.text or "").strip()]
    for message in recent:
        text = (message.text or "").strip()
        if _message_word_count(text) <= 12 and _is_name_like_text(text):
            return True
    return False


def _filter_recent_messages(person: PersonRecord) -> list[MessageSample]:
    return [message for message in person.recent_messages[:50] if (message.text or "").strip()]


async def _inbound_poll_loop() -> None:
    interval_minutes = max(1, int(os.environ.get("ROLODEX_INBOUND_POLL_MINUTES", "5") or "5"))
    while True:
        try:
            await poll_all_channels()
        except Exception:
            # Per-channel failures are already isolated in the poller. This
            # outer guard prevents the background loop from taking down serve.
            pass
        await asyncio.sleep(interval_minutes * 60)


def _normalize_filters(filters: dict[str, object]) -> dict[str, object]:
    normalized = dict(filters or {})
    relationship_values = (
        _coerce_list(normalized.get("relationship_classes"))
        or _coerce_list(normalized.get("relationship_class"))
    )
    excluded_classes = _coerce_list(normalized.get("excluded_classes"))
    tiers = _coerce_list(normalized.get("tiers")) or _coerce_list(normalized.get("tier"))
    sources = _coerce_list(normalized.get("source_in")) or _coerce_list(normalized.get("source"))
    topic_contains = _coerce_list(normalized.get("topic_contains"))

    normalized["relationship_classes"] = [item.lower() for item in relationship_values if item]
    normalized["excluded_classes"] = [item.lower() for item in excluded_classes if item]
    normalized["tiers"] = [item.upper() for item in tiers if item]
    normalized["source_in"] = [item.lower() for item in sources if item]
    normalized["topic_contains"] = [item.lower() for item in topic_contains if item]

    for field in (
        "min_messages",
        "max_messages",
        "min_total_messages",
        "max_total_messages",
        "min_outbound_messages",
        "min_inbound_messages",
        "last_contacted_before_days",
        "last_contacted_after_days",
    ):
        normalized[field] = _coerce_int(normalized.get(field))

    if normalized["min_total_messages"] is not None and normalized["min_messages"] is None:
        normalized["min_messages"] = normalized["min_total_messages"]
    if normalized["max_total_messages"] is not None and normalized["max_messages"] is None:
        normalized["max_messages"] = normalized["max_total_messages"]

    for field in ("has_user_note", "onboarding_reviewed", "do_not_contact", "no_followup_after_intro"):
        normalized[field] = _coerce_bool(normalized.get(field))

    keyword = normalized.get("keyword_in_messages")
    normalized["keyword_in_messages"] = str(keyword).strip() if keyword not in (None, "") else None
    normalized["heuristic"] = str(normalized.get("heuristic") or "").strip().lower() or None
    return normalized


def _normalize_action_plan(plan: dict[str, object], instruction: str) -> dict[str, object]:
    normalized = dict(plan or {})
    action = str(normalized.get("action") or _guess_action(instruction)).strip() or "draft_outreach"
    selector = normalized.get("selector")
    if not isinstance(selector, dict):
        selector = {"kind": "search", "query": instruction}
    selector = dict(selector)
    filters = selector.get("filters")
    if isinstance(filters, dict):
        selector["kind"] = "filter"
        selector["filters"] = _normalize_filters(filters)
    elif selector.get("kind") == "filter":
        selector["filters"] = {}
    elif selector.get("kind") == "ids":
        selector["person_ids"] = [str(item) for item in selector.get("person_ids", []) if item]
    else:
        selector["kind"] = str(selector.get("kind") or "search")
        selector["query"] = str(selector.get("query") or instruction).strip()
    normalized["action"] = action
    normalized["selector"] = selector
    if normalized.get("target_class"):
        normalized["target_class"] = str(normalized["target_class"]).strip().lower()
    if normalized.get("note_text") is not None:
        normalized["note_text"] = str(normalized.get("note_text") or "").strip() or None
    if normalized.get("message_template") is not None:
        normalized["message_template"] = str(normalized.get("message_template") or "").strip() or None
    snooze_days = _coerce_int(normalized.get("snooze_days"))
    if snooze_days is not None:
        normalized["snooze_days"] = snooze_days
    normalized["explanation"] = str(normalized.get("explanation") or f"Plan: {action} for {instruction!r}.")
    return normalized


def _keyword_matches_query(person: PersonRecord, query: str) -> bool:
    return _query_score(person, query) > 0


def _query_mentions_spam(query: str) -> bool:
    lowered = query.lower()
    return any(token in lowered for token in ("spam", "verification", "otc", "code", "2fa"))


def _heuristic_filters_for_query(query: str) -> dict[str, object]:
    lowered = query.lower()
    filters: dict[str, object] = {}
    if (
        ("exchanged names" in lowered)
        or ("name exchange" in lowered)
        or ("just exchanged names" in lowered)
        or ("just met" in lowered and "name" in lowered)
    ):
        filters["max_total_messages"] = 6
        filters["no_followup_after_intro"] = True
        filters["excluded_classes"] = ["family", "partner", SPAM_RELATIONSHIP_CLASS]
    if "over a year" in lowered or "more than a year" in lowered or "haven't talked to in a year" in lowered:
        filters["last_contacted_before_days"] = 365
    if "business contacts" in lowered or ("business" in lowered and "contacts" in lowered):
        filters["relationship_classes"] = ["business", "professional", "client"]
    if "old friends" in lowered or "old friend" in lowered:
        filters["relationship_classes"] = ["old_friend"]
        filters["include_old_friend_fallback"] = True
        filters.setdefault("last_contacted_before_days", 365)
    if "never followed up" in lowered or "no follow up" in lowered or "no follow-up" in lowered:
        filters["max_total_messages"] = min(
            6,
            int(filters.get("max_total_messages") or 6),
        )
        filters["no_followup_after_intro"] = True
        filters.setdefault("excluded_classes", ["family", "partner", SPAM_RELATIONSHIP_CLASS])
    tier_matches = re.findall(r"\bT([1-5])\b", query, flags=re.IGNORECASE)
    if tier_matches:
        filters["tiers"] = [f"T{match}" for match in tier_matches]
    range_match = re.search(r"\bbetween\s+(\d+)\s+and\s+(\d+)\s+messages?\b", lowered)
    if range_match:
        filters["min_messages"] = int(range_match.group(1))
        filters["max_messages"] = int(range_match.group(2))
    return _normalize_filters(filters)


def _extract_message_template(instruction: str) -> str | None:
    quoted_match = re.search(r"(['\"])(.+)\1\s*$", instruction)
    if quoted_match and len(quoted_match.group(2).strip()) >= 3:
        return quoted_match.group(2).strip()
    colon_match = re.search(r":\s*(.+)$", instruction)
    if colon_match:
        tail = colon_match.group(1).strip()
        if len(tail) >= 3:
            return tail.strip("'\"")
    return None


def _guess_action(instruction: str) -> str:
    lowered = instruction.lower()
    if any(token in lowered for token in ("snooze", "pause until", "hide until")):
        return "snooze"
    if any(token in lowered for token in ("set note", "note that", "annotate")):
        return "set_note"
    if any(token in lowered for token in ("set class", "classify as", "mark as")):
        return "set_class"
    return "draft_outreach"


def _fallback_action_plan(instruction: str) -> dict[str, object]:
    action = _guess_action(instruction)
    filters = _heuristic_filters_for_query(instruction)
    selector: dict[str, object] = {"kind": "search", "query": instruction}
    if filters:
        selector = {"kind": "filter", "filters": filters}
    class_match = re.search(
        r"\b(?:set class|classify as|mark as)\s+(family|partner|close_friend|casual_friend|old_friend|met_briefly|met_at_event|business|client|professional|mentor|mentee|service_provider|spam_or_verification|group_chat_member|unknown)\b",
        instruction,
        re.IGNORECASE,
    )
    note_text = _extract_message_template(instruction) if action == "set_note" else None
    snooze_days_match = re.search(r"\bfor\s+(\d+)\s+days?\b", instruction, re.IGNORECASE)
    return _normalize_action_plan(
        {
            "action": action,
            "selector": selector,
            "message_template": _extract_message_template(instruction),
            "target_class": class_match.group(1).lower() if class_match else None,
            "note_text": note_text,
            "snooze_days": int(snooze_days_match.group(1)) if snooze_days_match else 30,
            "explanation": f"Plan: {action} for contacts matching {instruction!r}.",
        },
        instruction,
    )


def _parse_action_plan_with_llm(instruction: str) -> dict[str, object] | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    prompt = json.dumps(
        {
            "instruction": instruction,
            "supported_actions": ["draft_outreach", "snooze", "set_note", "set_class"],
            "supported_relationship_classes": [
                "family", "partner", "close_friend", "casual_friend", "old_friend",
                "met_briefly", "met_at_event", "business", "client", "professional",
                "mentor", "mentee", "service_provider", "spam_or_verification",
                "group_chat_member", "unknown",
            ],
        },
        indent=2,
    )
    try:
        result = llm_draft(
            system=(
                "You parse Rolodex action instructions into strict JSON. "
                "Return exactly one JSON object with keys action, selector, message_template, explanation, "
                "and optionally target_class, note_text, snooze_days. "
                "If selector.filters is present then selector.kind MUST be \"filter\". "
                "Allowed filter keys: relationship_classes, excluded_classes, tiers, min_messages, max_messages, "
                "min_total_messages, max_total_messages, min_outbound_messages, min_inbound_messages, "
                "last_contacted_before_days, last_contacted_after_days, has_user_note, onboarding_reviewed, "
                "do_not_contact, no_followup_after_intro, source_in, topic_contains, keyword_in_messages, heuristic. "
                "Do not invent freeform filter keys. "
                "Worked example: for 'find people I just exchanged names with' emit "
                "{\"action\":\"draft_outreach\",\"selector\":{\"kind\":\"filter\",\"filters\":{\"max_total_messages\":6,"
                "\"no_followup_after_intro\":true,\"excluded_classes\":[\"family\",\"partner\",\"spam_or_verification\"]}},"
                "\"message_template\":null,\"explanation\":\"Find recent name exchanges.\"} "
                "not loose keys like {\"source\":\"texts\",\"max_total_messages\":4}. "
                "Use selector.kind=\"search\" only when you cannot express the target with the allowed filter keys."
            ),
            user=prompt,
            max_tokens=500,
            temperature=0,
            timeout=ACTION_LLM_TIMEOUT_SECONDS,
        ).text
        start = result.find("{")
        end = result.rfind("}")
        if start == -1 or end == -1:
            return None
        parsed = json.loads(result[start:end + 1])
        if not isinstance(parsed, dict):
            return None
        return _normalize_action_plan(parsed, instruction)
    except Exception:
        return None


def _action_plan_from_instruction(instruction: str) -> dict[str, object]:
    return _parse_action_plan_with_llm(instruction) or _fallback_action_plan(instruction)


def _person_matches_filters(person: PersonRecord, filters: dict[str, object]) -> bool:
    # Filter contract for Ask PILK action mode:
    # - The LLM emits only structured keys from `_parse_action_plan_with_llm`.
    # - `_normalize_filters` resolves legacy aliases (`relationship_class`, `tier`,
    #   `min_total_messages`, `max_total_messages`, etc.) into one runtime shape.
    # - This matcher is intentionally strict: every populated filter key must be
    #   honored here so action-mode counts reflect the requested selector.
    filters = _normalize_filters(filters)
    relationship_classes = [str(item).lower() for item in filters.get("relationship_classes", []) if item]
    if relationship_classes:
        active_class = (effective_relationship_class(person) or "unknown").lower()
        if active_class not in relationship_classes:
            if not (
                filters.get("include_old_friend_fallback")
                and "old_friend" in relationship_classes
                and active_class == "close_friend"
                and (_parse_days_ago(person.last_contacted or person.last_message_at) or 0) > 365
            ):
                return False
    excluded_classes = [str(item).lower() for item in filters.get("excluded_classes", []) if item]
    active_class = (effective_relationship_class(person) or "unknown").lower()
    if excluded_classes and active_class in excluded_classes:
        return False
    tiers = [str(item).upper() for item in filters.get("tiers", []) if item]
    if tiers and active_tier(person) not in tiers:
        return False
    inbound_count = int(person.inbound_message_count or 0)
    outbound_count = int(person.outbound_message_count or 0)
    message_count = inbound_count + outbound_count
    min_messages = filters.get("min_messages")
    max_messages = filters.get("max_messages")
    if min_messages is not None and message_count < int(min_messages):
        return False
    if max_messages is not None and message_count > int(max_messages):
        return False
    min_outbound_messages = filters.get("min_outbound_messages")
    if min_outbound_messages is not None and outbound_count < int(min_outbound_messages):
        return False
    min_inbound_messages = filters.get("min_inbound_messages")
    if min_inbound_messages is not None and inbound_count < int(min_inbound_messages):
        return False
    before_days = filters.get("last_contacted_before_days")
    days_ago = _parse_days_ago(person.last_contacted or person.last_message_at)
    if before_days is not None:
        if days_ago is None or days_ago <= int(before_days):
            return False
    after_days = filters.get("last_contacted_after_days")
    if after_days is not None and (days_ago is None or days_ago >= int(after_days)):
        return False
    has_user_note = filters.get("has_user_note")
    if has_user_note is not None and bool((person.user_note or "").strip()) is not has_user_note:
        return False
    onboarding_reviewed = filters.get("onboarding_reviewed")
    if onboarding_reviewed is not None and bool(person.onboarding_reviewed) is not onboarding_reviewed:
        return False
    do_not_contact = filters.get("do_not_contact")
    if do_not_contact is not None and bool(person.do_not_contact) is not do_not_contact:
        return False
    no_followup_after_intro = filters.get("no_followup_after_intro")
    if no_followup_after_intro:
        if message_count > 6:
            return False
        if not (_looks_like_name_exchange(person) or _has_self_intro_pattern(person)):
            return False
    source_in = [str(item).lower() for item in filters.get("source_in", []) if item]
    if source_in and not (_normalized_source_tokens(person) & set(source_in)):
        return False
    topic_contains = [str(item).lower() for item in filters.get("topic_contains", []) if item]
    if topic_contains:
        hay_topics = [str(topic).lower() for topic in person.topics]
        if not any(any(needle in topic for needle in topic_contains) for topic in hay_topics):
            return False
    keyword_in_messages = str(filters.get("keyword_in_messages") or "").strip().lower()
    if keyword_in_messages:
        recent = _filter_recent_messages(person)
        if not any(keyword_in_messages in (message.text or "").lower() for message in recent):
            return False
    heuristic = str(filters.get("heuristic") or "").strip().lower()
    if heuristic == "name_exchange" and not _looks_like_name_exchange(person):
        return False
    return True


def _select_people_for_action(store: RolodexStore, selector: dict[str, object], instruction: str) -> list[PersonRecord]:
    kind = str(selector.get("kind") or "search")
    if kind == "ids":
        wanted = {str(person_id) for person_id in selector.get("person_ids", []) if person_id}
        return [person for person in store.people if person.person_id in wanted]
    if kind == "filter":
        filters = selector.get("filters") or {}
        if not isinstance(filters, dict):
            filters = {}
        filters = _normalize_filters(filters)
        return [person for person in store.people if _person_matches_filters(person, filters)]

    query = str(selector.get("query") or instruction).strip()
    filters = _heuristic_filters_for_query(query)
    candidates = store.people
    if not _query_mentions_spam(query):
        candidates = [person for person in candidates if (effective_relationship_class(person) or "unknown") != SPAM_RELATIONSHIP_CLASS]
    if filters:
        filtered = [person for person in candidates if _person_matches_filters(person, filters)]
        if filtered:
            return filtered
    ranked = sorted(
        candidates,
        key=lambda person: (_query_score(person, query), int(_looks_like_name_exchange(person))),
        reverse=True,
    )
    return [person for person in ranked if _keyword_matches_query(person, query)]


def _personalize_template(template: str, person: PersonRecord) -> str:
    first_name = (person.first_name or display_name(person).split(" ", 1)[0]).strip()
    draft = template
    for placeholder in ("{name}", "[name]", "<name>"):
        draft = draft.replace(placeholder, first_name)
    return draft.strip()


async def _draft_for_action(person: PersonRecord, plan: dict[str, object]) -> str:
    template = str(plan.get("message_template") or "").strip()
    if template:
        return _personalize_template(template, person)

    async def _llm(prompt: str, system: str) -> str:
        return llm_draft(
            system=system,
            user=prompt,
            max_tokens=220,
            temperature=0.6,
            timeout=ACTION_LLM_TIMEOUT_SECONDS,
        ).text

    bundle = await generate_draft(person, "ask-action", _llm)
    return bundle.top_draft


def _apply_non_draft_action(person: PersonRecord, plan: dict[str, object], *, dry_run: bool) -> dict[str, object]:
    action = str(plan.get("action") or "draft_outreach")
    if dry_run or os.environ.get("ROLODEX_DRY_RUN") == "1":
        return {"applied": False, "dry_run": True}
    now = datetime.now(UTC)
    if action == "set_class":
        target_class = str(plan.get("target_class") or "").strip() or None
        if target_class:
            person.user_override_class = target_class
            person.user_marked_at = now
            return {"applied": True, "target_class": target_class}
    if action == "set_note":
        note_text = str(plan.get("note_text") or "").strip() or None
        if note_text:
            person.user_note = note_text
            person.user_marked_at = now
            return {"applied": True, "note_text": note_text}
    if action == "snooze":
        days = max(1, int(plan.get("snooze_days") or 30))
        person.cadence = person.cadence.model_copy(
            update={"snooze_until": (datetime.now(UTC) + timedelta(days=days)).isoformat()}
        )
        person.updated_at = now.isoformat()
        return {"applied": True, "snooze_days": days}
    return {"applied": False}


def ask_rolodex_action(
    instruction: str,
    *,
    dry_run: bool = True,
    max_targets: int = ACTION_DEFAULT_MAX_TARGETS,
    settings=None,
) -> dict:
    settings = settings or get_settings()
    store = load_store(store_path(settings))
    plan = _action_plan_from_instruction(instruction)
    selector = plan.get("selector") if isinstance(plan.get("selector"), dict) else {"kind": "search", "query": instruction}
    matched = _select_people_for_action(store, selector, instruction)
    ranked = sorted(
        matched,
        key=lambda person: compute_priority(person, settings, _today()) or 0.0,
        reverse=True,
    )
    selected = ranked[: max(1, min(int(max_targets or ACTION_DEFAULT_MAX_TARGETS), ACTION_DEFAULT_MAX_TARGETS))]
    action = str(plan.get("action") or "draft_outreach")
    drafts: list[dict[str, object]] = []
    mutations: list[dict[str, object]] = []
    if action == "draft_outreach":
        for person in selected:
            try:
                draft_text = asyncio.run(_draft_for_action(person, plan))
            except Exception as exc:
                draft_text = f"[draft failed: {exc}]"
            drafts.append(
                {
                    "person_id": person.person_id,
                    "name": display_name(person),
                    "draft": draft_text,
                    "channel": (person.connected_channels[0] if person.connected_channels else "imessage"),
                }
            )
    else:
        with store_transaction(store_path(settings)) as txn_store:
            for person in selected:
                live = next((item for item in txn_store.people if item.person_id == person.person_id), None)
                if live is None:
                    continue
                mutations.append(
                    {
                        "person_id": live.person_id,
                        "name": display_name(live),
                        **_apply_non_draft_action(live, plan, dry_run=dry_run),
                    }
                )
    return {
        "action": action,
        "explanation": str(plan.get("explanation") or f"Prepared {action} for {len(selected)} people."),
        "matched_count": len(matched),
        "selected_count": len(selected),
        "drafts": drafts,
        "mutations": mutations,
        "would_send": bool(dry_run),
        "plan": plan,
    }


# ─── Voice learning loop ────────────────────────────────────────────────────
#
# Every time the operator sends a draft, edits before sending, or skips a
# proposed draft, we capture that as a tone-feedback row. The next draft
# generated for that person reads the most recent feedback rows via
# `_feedback_anchor` in agent/draft.py — so each interaction tightens the
# next draft toward the user's actual voice.


def _compact_edit_diff(original: str, sent: str) -> str:
    """One-line summary of how the operator edited a draft.

    Subsequent draft generation reads this in `_feedback_anchor` to learn the
    user's real voice. Keep it short, concrete, and machine-friendly.
    """
    if not original:
        return sent[:160]
    if not sent:
        return f"sent empty (deleted draft of: {original[:80]!r})"
    return f"original={original[:80]!r} → sent={sent[:80]!r}"


def _append_tone_feedback(
    person: PersonRecord,
    *,
    rating: str,
    draft_sent: str,
    edit_diff: str | None,
    timestamp: str,
) -> None:
    """Append to person.tone_profile.feedback_log (capped at 25 most recent).

    The LLM only reads the last 3 rows during draft generation, but we keep
    a longer history so future product features (analytics, voice fine-tune)
    have data to work with.
    """
    from agent.models import ToneFeedback as _ToneFeedback
    if rating not in {"edited", "off", "sounds_like_me"}:
        return
    sample = _ToneFeedback(
        timestamp=timestamp,
        draft_sent=(draft_sent or "")[:240],
        rating=rating,  # type: ignore[arg-type]
        edit_diff=edit_diff,
    )
    log = list(person.tone_profile.feedback_log or [])
    log.append(sample)
    person.tone_profile.feedback_log = log[-25:]


def _record_feedback(person_id: str, payload: "FeedbackPayload") -> dict:
    """Operator-explicit feedback (Skip → off, etc.) without a send.

    Used by the Skip button: "this draft was off, don't write like this for
    me again". The Send path auto-records via _send_to_person.
    """
    settings = get_settings()
    rating = (payload.rating or "").lower().strip()
    if rating not in {"edited", "off", "sounds_like_me"}:
        return {"ok": False, "error": f"invalid rating: {rating!r}"}
    with store_transaction(store_path(settings)) as store:
        for person in store.people:
            if person.person_id != person_id:
                continue
            _append_tone_feedback(
                person,
                rating=rating,
                draft_sent=payload.draft or "",
                edit_diff=payload.edit_diff,
                timestamp=datetime.now(UTC).isoformat(),
            )
            return {"ok": True, "person_id": person_id, "rating": rating}
    raise HTTPException(status_code=404, detail="Person not found")


async def _generate_post_feedback_draft(person: PersonRecord, reason: str = "post-feedback") -> DraftBundle:
    async def _llm(prompt: str, system: str) -> str:
        return llm_draft(
            system=system,
            user=prompt,
            max_tokens=220,
            temperature=0.6,
            timeout=ACTION_LLM_TIMEOUT_SECONDS,
        ).text

    return await generate_draft(person, reason, _llm)


def _regenerate_draft(person_id: str, payload: "RegenerateDraftPayload") -> dict:
    settings = get_settings()
    store = load_store(store_path(settings))
    person = next((item for item in store.people if item.person_id == person_id), None)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    bundle = asyncio.run(_generate_post_feedback_draft(person, payload.reason or "post-feedback"))

    latest_run_id = _latest_run_id(store) or bundle.run_id
    try:
        with store_transaction(store_path(settings)) as txn_store:
            target = next((item for item in txn_store.people if item.person_id == person_id), None)
            if target is None:
                raise HTTPException(status_code=404, detail="Person not found")
            persisted = bundle.model_copy(update={"run_id": latest_run_id})
            txn_store.drafts[f"{latest_run_id}:{person_id}"] = persisted
            for candidate in txn_store.digests.get(latest_run_id, []):
                if candidate.person_id == person_id:
                    candidate.draft_preview = persisted.top_draft
    except HTTPException:
        raise
    except Exception:
        persisted = bundle.model_copy(update={"run_id": latest_run_id})

    return {
        "ok": True,
        "person_id": person_id,
        "draft": {
            "run_id": persisted.run_id,
            "reason": persisted.reason,
            "relationship_class": effective_relationship_class(person) or "unknown",
            "draft_preview": persisted.top_draft,
            "alternates": list(persisted.alternates),
        },
    }


def _annotate_person(person_id: str, payload: AnnotationPayload) -> dict:
    settings = get_settings()
    path = store_path(settings)
    with store_transaction(path) as store:
        for person in store.people:
            if person.person_id != person_id:
                continue
            person.user_note = payload.user_note or None
            person.user_override_class = payload.user_override_class or None
            person.user_override_tier = payload.user_override_tier or None
            person.user_priority_boost = payload.user_priority_boost
            person.do_not_contact = bool(payload.do_not_contact)
            person.instagram_username = payload.instagram_username or None
            person.facebook_handle = payload.facebook_handle or None
            person.twitter_handle = payload.twitter_handle or None
            person.linkedin_url = payload.linkedin_url or None
            person.snapchat_username = payload.snapchat_username or None
            person.tiktok_handle = payload.tiktok_handle or None
            person.how_we_met = payload.how_we_met or None
            if payload.onboarding_reviewed is not None:
                person.onboarding_reviewed = bool(payload.onboarding_reviewed)
                person.onboarding_reviewed_at = datetime.now(UTC) if person.onboarding_reviewed else None
            person.user_marked_at = datetime.now(UTC)
            only_marking_reviewed = (
                payload.onboarding_reviewed is True
                and not any(
                    (
                        payload.user_note,
                        payload.user_override_class,
                        payload.user_override_tier,
                        payload.user_priority_boost not in (None, 0),
                        payload.do_not_contact,
                        payload.instagram_username,
                        payload.facebook_handle,
                        payload.twitter_handle,
                        payload.linkedin_url,
                        payload.snapchat_username,
                        payload.tiktok_handle,
                        payload.how_we_met,
                    )
                )
            )
            classification = None if only_marking_reviewed else _maybe_apply_note_classification(person, store, settings)
            return {
                "ok": True,
                "person_id": person.person_id,
                "name": display_name(person),
                "classification": classification,
            }
    raise HTTPException(status_code=404, detail="Person not found")


def _send_to_person(person_id: str, payload: SendPayload) -> dict:
    """
    Send a message to a person and persist the state change.

    On both real send and dry-run we update the store so the cadence engine
    knows we just contacted this person — otherwise they'd stay marked
    "due now" forever after you click Send.
    """
    settings = get_settings()
    is_dry_run = os.environ.get("ROLODEX_DRY_RUN") == "1"
    channel_name = payload.channel
    now = datetime.now(UTC)

    # Resolve person + dispatch first (so we know the channel).
    store = load_store(store_path(settings))
    person = next((item for item in store.people if item.person_id == person_id), None)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")

    if is_dry_run:
        result_channel = (
            channel_name
            or (person.connected_channels[0] if person.connected_channels else "imessage")
        )
        result_ok = True
        result_error: str | None = None
    else:
        try:
            if channel_name:
                handle = handle_for_channel(person, channel_name)
                if not handle:
                    return {"ok": False, "channel": channel_name, "error": f"No {channel_name} handle for person"}
                send_result = get_channel(channel_name).send(handle, payload.text)
            else:
                send_result = route_message(person, payload.text)
            result_ok = send_result.ok
            result_channel = send_result.channel
            result_error = send_result.error
        except Exception as exc:
            return {"ok": False, "channel": channel_name or "auto", "error": str(exc)}
        if not result_ok:
            # Don't mutate cadence on failure.
            response: dict = {"ok": False, "channel": result_channel}
            if result_error:
                response["error"] = result_error
            return response

    # Persist the send to the store: mark cadence, append a synthetic
    # outbound message sample, bump the outbound counter, AND record voice
    # feedback if the operator edited the original draft before sending.
    try:
        with store_transaction(store_path(settings)) as txn_store:
            target = next((item for item in txn_store.people if item.person_id == person_id), None)
            if target is not None:
                target.cadence = target.cadence.model_copy(
                    update={"last_sent_at": now.isoformat()}
                )
                target.last_contacted = now.isoformat()
                target.last_message_at = now.isoformat()
                target.last_message_direction = "outbound"
                target.outbound_message_count = int(target.outbound_message_count or 0) + 1
                sample = MessageSample(
                    rowid=None,
                    at=now.isoformat(),
                    direction="outbound",
                    text=payload.text,
                    handle=target.handles[0] if target.handles else None,
                    channel=result_channel,
                )
                target.recent_messages = [sample] + list(target.recent_messages)[:199]
                target.updated_at = now.isoformat()

                # Voice learning: if we have the original draft and it differs
                # from what was actually sent, record an "edited" rating with
                # a diff. Subsequent drafts for this person will see it via
                # _feedback_anchor and tighten toward the user's real voice.
                original = (payload.original_draft or "").strip()
                sent = (payload.text or "").strip()
                if original and sent and original != sent:
                    _append_tone_feedback(
                        target,
                        rating="edited",
                        draft_sent=sent,
                        edit_diff=_compact_edit_diff(original, sent),
                        timestamp=now.isoformat(),
                    )
                elif original and sent:
                    # Sent unchanged → confirms the draft sounded right.
                    _append_tone_feedback(
                        target,
                        rating="sounds_like_me",
                        draft_sent=sent,
                        edit_diff=None,
                        timestamp=now.isoformat(),
                    )
    except Exception:
        # The send succeeded; we just couldn't write the cadence update.
        # Don't fail the request — log and continue.
        pass

    # Audit log entry so the Sent KPI counts go up.
    try:
        append_audit_entry(
            settings,
            {
                "ts": now.isoformat(),
                "action": "send_succeeded" if result_ok else "send_failed",
                "person_id": person_id,
                "channel": result_channel,
                "preview": payload.text[:140],
                "dry_run": is_dry_run,
            },
        )
    except Exception:
        pass

    response = {
        "ok": result_ok,
        "channel": result_channel,
    }
    if is_dry_run:
        response["dry_run"] = True
        response["would_send"] = payload.text
    if not result_ok and result_error:
        response["error"] = result_error
    return response


def _query_score(person: PersonRecord, query: str) -> int:
    # Build the searchable haystack from EVERY field that could plausibly
    # contain a topic, organization, or context word — including `company`
    # which is set on contacts imported from macOS Contacts but was being
    # ignored by the previous search. Without it, queries like "CBD" only
    # matched 12 of 293 actual matches.
    text = " ".join(
        [
            display_name(person),
            " ".join(person.handles),
            effective_relationship_class(person) or "",
            active_tier(person),
            person.contact_organization or "",
            person.company or "",
            " ".join(person.contact_tags),
            person.context_summary or "",
            " ".join(person.topics),
            person.user_note or "",
            person.first_name or "",
            person.last_name or "",
        ]
    ).lower()
    score = 0
    for token in [part for part in query.lower().split() if part]:
        if token in text:
            score += 3
    if "over a year" in query.lower() and person.last_contacted:
        try:
            days = (_today() - datetime.fromisoformat(person.last_contacted).date()).days
        except ValueError:
            days = 0
        if days > 365:
            score += 8
    if "close" in query.lower() and (effective_relationship_class(person) in {"close_friend", "family", "partner"} or active_tier(person) in {"T1", "T2"}):
        score += 5
    if "st pete" in query.lower() and ("st.pete" in person.contact_tags or "st pete" in (person.contact_organization or "").lower()):
        score += 8
    return score


def ask_rolodex_query(query: str) -> dict:
    settings = get_settings()
    store = load_store(store_path(settings))
    draft_map = _drafts_by_person(store)
    ranked_people = sorted(
        store.people,
        key=lambda person: (_query_score(person, query), compute_priority(person, settings, _today()) or 0.0),
        reverse=True,
    )
    # Find ALL people whose haystack matched (was hard-capped at 12 — caused
    # the user's "I have 287 CBD contacts but Ask PILK shows 12" complaint).
    full_matches = [person for person in ranked_people if _query_score(person, query) > 0]
    total_matches = len(full_matches)
    # Cap the slice we send to the LLM so the prompt stays manageable, but
    # return up to LLM_RESULT_LIMIT to the UI.
    LLM_LIMIT = 30
    UI_LIMIT = 200
    llm_subset = full_matches[:LLM_LIMIT]
    ui_matches = full_matches[:UI_LIMIT]
    if not full_matches:
        ui_matches = ranked_people[:8]
        llm_subset = ui_matches
    subset = [
        {
            "person_id": person.person_id,
            "name": display_name(person),
            "relationship_class": effective_relationship_class(person) or "unknown",
            "tier": active_tier(person),
            "last_contacted": _safe_last_contact(person),
            "message_count": int(person.inbound_message_count or 0) + int(person.outbound_message_count or 0),
            "company": person.company or person.contact_organization or "",
            "contact_tags": list(person.contact_tags),
            "context_summary": person.context_summary,
            "recent_messages": [
                {"direction": message.direction, "text": message.text, "at": message.at}
                for message in person.recent_messages[:8]
            ],
        }
        for person in llm_subset
    ]
    answer = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        prompt_payload = {
            "query": query,
            "total_matches_in_rolodex": total_matches,
            "showing_top_n": len(subset),
            "people": subset,
        }
        prompt = json.dumps(prompt_payload, indent=2)
        result = llm_draft(
            system=(
                "You are Ask Rolodex, a relationship-memory assistant. "
                "Answer the user's query using the provided rolodex subset. "
                "IMPORTANT: `total_matches_in_rolodex` is the TRUE total count "
                "across the user's entire rolodex; `people` is just the top "
                "`showing_top_n` matches by score. When summarizing counts, "
                "use total_matches_in_rolodex as the authoritative count and "
                "explain that you're describing the highest-priority slice. "
                "Return strict JSON with keys answer and person_ids."
            ),
            user=prompt,
            max_tokens=600,
            temperature=0.2,
        ).text
        try:
            parsed = json.loads(result[result.find("{"): result.rfind("}") + 1])
            answer = str(parsed.get("answer") or "").strip() or None
            chosen_ids = [person_id for person_id in parsed.get("person_ids", []) if isinstance(person_id, str)]
            if chosen_ids:
                # If the LLM picked specific people, prioritize them but keep
                # the rest of the matched set so the UI can still surface
                # everyone if the user wants the full list.
                chosen_set = set(chosen_ids)
                ui_matches = (
                    [p for p in ui_matches if p.person_id in chosen_set]
                    + [p for p in ui_matches if p.person_id not in chosen_set]
                )
        except Exception:
            answer = result.strip()
    if not answer:
        if total_matches > len(ui_matches):
            answer = f"Found {total_matches} matches for {query!r}. Showing top {len(ui_matches)} by priority."
        else:
            answer = f"Found {total_matches} matches for {query!r}."

    # Reassign matches to the UI list — the caller serializes this.
    matches = ui_matches
    return {
        "answer": answer,
        "people": [_serialize_person_summary(person, settings, draft_map) for person in matches],
        "person_ids": [person.person_id for person in matches],
    }


def create_app() -> FastAPI:
    app = FastAPI(title="Rolodex UI")

    @app.on_event("startup")
    async def on_startup():
        _get_connection_store().apply_to_env()
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return
        scheduler = RolodexScheduler(run_callback=daily_run)
        scheduler.start()
        app.state.scheduler = scheduler
        app.state.inbound_poll_task = asyncio.create_task(_inbound_poll_loop())

    @app.on_event("shutdown")
    async def on_shutdown():
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            scheduler.shutdown()
        poll_task = getattr(app.state, "inbound_poll_task", None)
        if poll_task is not None:
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass

    @app.get("/")
    def index():
        return FileResponse(APP_HTML_PATH)

    @app.get("/api/people")
    def api_people():
        return _serialize_people()

    @app.get("/api/people/{person_id}")
    def api_person(person_id: str):
        return _serialize_person_detail(person_id)

    @app.get("/api/digest")
    def api_digest():
        return _serialize_digest()

    @app.get("/api/audit")
    def api_audit():
        return _serialize_audit()

    @app.get("/api/roi")
    def api_roi():
        return _serialize_roi()

    @app.get("/api/settings")
    def api_settings():
        return _serialize_settings()

    @app.get("/api/inbound-poll/status")
    def api_inbound_poll_status():
        return _serialize_inbound_poll_status()

    @app.get("/api/channels")
    def api_channels():
        return _serialize_channels()

    @app.get("/api/connections")
    def api_connections():
        return _serialize_connections()

    @app.post("/api/connections/{channel}")
    def api_save_connection(channel: str, payload: ConnectionSavePayload):
        name = _require_connection_channel(channel)
        allowed_keys = set(channel_keys(name))
        unknown = sorted(set(payload.credentials) - allowed_keys)
        if unknown:
            return {"ok": False, "error": f"Unknown credential keys: {', '.join(unknown)}"}

        store = _get_connection_store()
        for key, raw_value in payload.credentials.items():
            value = str(raw_value or "").strip()
            if value:
                store.set_credential(name, key, value)
            else:
                store.delete_credential(name, key)
                os.environ.pop(key, None)
        store.apply_to_env()

        try:
            if payload.test:
                return _run_connection_test(name)
            return _channel_health_payload(name)
        except Exception as exc:
            health = get_channel(name).health_check()
            return {
                "ok": False,
                "configured": health.configured,
                "healthy": False,
                "error": str(exc),
            }

    @app.delete("/api/connections/{channel}")
    def api_delete_connection(channel: str):
        name = _require_connection_channel(channel)
        store = _get_connection_store()
        for key in channel_keys(name):
            store.delete_credential(name, key)
        _clear_connection_env(name)
        health = get_channel(name).health_check()
        return {
            "ok": True,
            "configured": health.configured,
            "healthy": health.healthy,
            "message": f"{channel_schema(name)['human_name']} disconnected",
        }

    @app.post("/api/connections/{channel}/test")
    def api_test_connection(channel: str):
        name = _require_connection_channel(channel)
        try:
            return _run_connection_test(name)
        except Exception as exc:
            health = get_channel(name).health_check()
            return {
                "ok": False,
                "configured": health.configured,
                "healthy": False,
                "error": str(exc),
            }

    @app.get("/api/connections/{channel}/inbox")
    def api_connection_inbox(channel: str, limit: int = 20):
        name = _require_meta_channel(channel)
        adapter, error_payload = _meta_channel_or_error(name)
        if error_payload is not None:
            return {"channel": name, "conversations": [], "error": str(error_payload["error"])}
        try:
            conversations = adapter.list_conversations(limit=max(1, limit))
            return {"channel": name, "conversations": conversations, "error": None}
        except NotConfigured:
            return {"channel": name, "conversations": [], "error": "channel not configured"}
        except Exception as exc:
            if is_meta_capability_error(exc):
                return {"channel": name, "conversations": [], "error": META_DM_FIRST_HINT}
            return {"channel": name, "conversations": [], "error": str(exc)}

    @app.post("/api/connections/{channel}/reply")
    def api_connection_reply(channel: str, payload: MetaReplyPayload):
        name = _require_meta_channel(channel)
        adapter, error_payload = _meta_channel_or_error(name)
        if error_payload is not None:
            return error_payload
        try:
            result = adapter.send(payload.participant_id, payload.text)
            return {
                "ok": result.ok,
                "channel": name,
                "message_id": result.message_id,
                "error": result.error,
            }
        except NotConfigured:
            return {"ok": False, "channel": name, "message_id": None, "error": "channel not configured"}
        except Exception as exc:
            return {"ok": False, "channel": name, "message_id": None, "error": str(exc)}

    @app.post("/api/connections/{channel}/send_test")
    def api_connection_send_test(channel: str, payload: MetaSendTestPayload):
        name = _require_meta_channel(channel)
        adapter, error_payload = _meta_channel_or_error(name)
        if error_payload is not None:
            return {"ok": False, "message_id": None, "error": str(error_payload["error"])}
        try:
            result = adapter.send(
                payload.handle,
                payload.text or "Hey, this is a Rolodex AI test message — feel free to ignore.",
            )
            return {
                "ok": result.ok,
                "message_id": result.message_id,
                "error": result.error,
            }
        except NotConfigured:
            return {"ok": False, "message_id": None, "error": "channel not configured"}
        except Exception as exc:
            return {"ok": False, "message_id": None, "error": str(exc)}

    @app.get("/api/onboarding/queue")
    def api_onboarding_queue(limit: int = 20):
        return _serialize_onboarding_queue(limit=limit)

    @app.get("/api/onboarding/progress")
    def api_onboarding_progress():
        return _serialize_onboarding_progress()

    @app.post("/api/digest/trigger-now")
    async def api_digest_trigger_now():
        scheduler = get_active_scheduler() or getattr(app.state, "scheduler", None)
        if scheduler is not None:
            await scheduler.run_now()
        else:
            await daily_run()
        return {"ok": True, "triggered_at": datetime.now(UTC).isoformat()}

    @app.post("/api/ask")
    def api_ask(payload: AskPayload):
        return ask_rolodex_query(payload.query)

    @app.post("/api/ask/action")
    def api_ask_action(payload: AskActionPayload):
        try:
            return ask_rolodex_action(
                payload.instruction,
                dry_run=bool(payload.dry_run),
                max_targets=int(payload.max_targets or ACTION_DEFAULT_MAX_TARGETS),
            )
        except Exception as exc:
            return {
                "action": "draft_outreach",
                "explanation": f"Ask action failed gracefully: {exc}",
                "matched_count": 0,
                "selected_count": 0,
                "drafts": [],
                "would_send": True,
            }

    @app.get("/api/decrypt")
    def api_decrypt():
        return PlainTextResponse(decrypt_store_to_text(store_path(get_settings())))

    @app.get("/README.md")
    def readme():
        return FileResponse(README_PATH)

    @app.post("/api/people/{person_id}/annotate")
    def annotate_person(person_id: str, payload: AnnotationPayload):
        return _annotate_person(person_id, payload)

    @app.post("/api/people/{person_id}/send")
    def send_person(person_id: str, payload: SendPayload):
        return _send_to_person(person_id, payload)

    @app.post("/api/people/{person_id}/feedback")
    def feedback_person(person_id: str, payload: FeedbackPayload):
        return _record_feedback(person_id, payload)

    @app.post("/api/people/{person_id}/regenerate-draft")
    def regenerate_person_draft(person_id: str, payload: RegenerateDraftPayload | None = None):
        return _regenerate_draft(person_id, payload or RegenerateDraftPayload())

    return app


def _browser_url(host: str, port: int) -> str:
    visible_host = "localhost" if host in {"127.0.0.1", "0.0.0.0"} else host
    return f"http://{visible_host}:{port}"


def serve(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    _get_connection_store().apply_to_env()
    app = create_app()
    url = _browser_url(host, port)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open_new_tab(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="info")
