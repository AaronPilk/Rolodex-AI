from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import asyncio
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from agent.config import get_settings
from agent.contacts_reader import load_contacts_snapshot, lookup_by_phone, lookup_in_snapshot, search_by_name
from agent.imessage_reader import CHAT_DB_PATH, list_threads, read_thread
from agent.llm_client import classify as llm_classify, draft as llm_draft
from agent.channels.dispatcher import infer_channels_from_handles
from agent.models import (
    CadenceState,
    Channel,
    ContactMatch,
    GroupThread,
    MessageSample,
    PersonRecord,
    ScoringFields,
    SyncReport,
    ThreadSnapshot,
    ToneProfile,
)
from agent.relationship_signals import infer_relationship
from agent.scoring import classify_natural_end
from agent.scoring import auto_assign_tiers
from agent.store import (
    get_person_by_handle,
    load_store,
    save_store,
    store_path,
    store_transaction,
    upsert_person,
)
from agent.person_utils import looks_like_phone_label

_SENSITIVE_THREAD_PROMPT = (
    "Classify this message thread for sensitive content. Return one of: "
    "NONE, MEDICAL, LEGAL, MENTAL_HEALTH, SEXUAL, GRIEF, CONFLICT. "
    "Reply with only the label."
)
_SENSITIVE_LABELS = {"NONE", "MEDICAL", "LEGAL", "MENTAL_HEALTH", "SEXUAL", "GRIEF", "CONFLICT"}
_RELATIONSHIP_LABELS = (
    "family",
    "partner",
    "close_friend",
    "casual_friend",
    "old_friend",
    "met_briefly",
    "met_at_event",
    "business",
    "client",
    "professional",
    "mentor",
    "mentee",
    "service_provider",
    "spam_or_verification",
    "group_chat_member",
    "unknown",
)
_THREAD_METADATA_TTL = timedelta(days=30)
_DETERMINISTIC_RELATIONSHIP_HASH_VERSION = "deterministic-v1"
_NAME_SUFFIX_PATTERN = re.compile(r"\s+(home|work|cell|mobile)\s*$", re.IGNORECASE)
_SELF_INTRO_PATTERNS = (
    re.compile(r"\b(?:hi|hello|hey)[, ]+(?:this is|it's|it is)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"),
    re.compile(r"\b(?:this is|it's|it is)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"),
    re.compile(r"\b([A-Z][a-z]+)\s+here\b"),
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _handle_key(handle: str) -> str:
    text = handle.strip().lower()
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits:
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        return digits
    return text


def _parse_message_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _message_identity(message: MessageSample) -> tuple[object, ...]:
    if message.rowid is not None:
        return ("rowid", int(message.rowid))
    return ("fallback", message.at or "", message.text or "")


def _message_sort_key(message: MessageSample) -> tuple[float, int]:
    parsed = _parse_message_at(message.at)
    return (
        parsed.timestamp() if parsed else float("-inf"),
        int(message.rowid or -1),
    )


def _merge_recent_messages(*message_sets: list[MessageSample], limit: int = 500) -> list[MessageSample]:
    merged: dict[tuple[object, ...], MessageSample] = {}
    for messages in message_sets:
        for message in messages:
            key = _message_identity(message)
            current = merged.get(key)
            if current is None or _message_sort_key(message) > _message_sort_key(current):
                merged[key] = message
    ordered = sorted(merged.values(), key=_message_sort_key, reverse=True)
    return ordered[:limit]


def _message_rollup(messages: list[MessageSample]) -> dict[str, object]:
    latest = messages[0] if messages else None
    outbound = next((msg for msg in messages if msg.direction == "outbound"), None)
    return {
        "last_message_at": latest.at if latest else None,
        "last_message_direction": latest.direction if latest else None,
        "last_contacted": outbound.at if outbound else (latest.at if latest else None),
        "inbound_message_count": sum(1 for msg in messages if msg.direction == "inbound"),
        "outbound_message_count": sum(1 for msg in messages if msg.direction == "outbound"),
    }


def _merge_group_threads(*group_sets: list[GroupThread]) -> list[GroupThread]:
    merged: dict[int, GroupThread] = {}
    for groups in group_sets:
        for group in groups:
            current = merged.get(group.chat_id)
            if current is None:
                merged[group.chat_id] = group
                continue
            current_handles = list(dict.fromkeys([*current.handles, *group.handles]))
            merged[group.chat_id] = GroupThread(
                chat_id=group.chat_id,
                title=current.title or group.title,
                handles=current_handles,
                last_message_at=max(
                    [item for item in [current.last_message_at, group.last_message_at] if item],
                    default=current.last_message_at or group.last_message_at,
                ),
            )
    return sorted(
        merged.values(),
        key=lambda group: _parse_message_at(group.last_message_at).timestamp()
        if _parse_message_at(group.last_message_at)
        else float("-inf"),
        reverse=True,
    )


def _merge_channels(*channel_sets: list[Channel]) -> list[Channel]:
    merged: dict[tuple[object, ...], Channel] = {}
    for channels in channel_sets:
        for channel in channels:
            key = (
                channel.type,
                channel.chat_id if channel.chat_id is not None else _handle_key(channel.handle),
            )
            existing = merged.get(key)
            if existing is None:
                merged[key] = channel.model_copy(deep=True)
                continue
            existing.handle = existing.handle or channel.handle
            existing.message_count = max(existing.message_count, channel.message_count)
            existing.last_message_at = max(
                [item for item in [existing.last_message_at, channel.last_message_at] if item],
                default=existing.last_message_at or channel.last_message_at,
            )
            if (
                channel.last_message_at
                and existing.last_message_at == channel.last_message_at
                and channel.last_message_direction is not None
            ):
                existing.last_message_direction = channel.last_message_direction
            existing.active = existing.active or channel.active
    return sorted(
        merged.values(),
        key=lambda channel: _parse_message_at(channel.last_message_at).timestamp()
        if _parse_message_at(channel.last_message_at)
        else float("-inf"),
        reverse=True,
    )


def _choose_value(*values):
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return values[-1] if values else None


def _normalized_email_keys(person: PersonRecord) -> set[str]:
    keys: set[str] = set()
    for handle in [*person.handles, *[channel.handle for channel in person.channels if channel.handle]]:
        value = handle.strip().lower()
        if "@" in value:
            keys.add(value)
    return keys


def _display_name_base(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or re.fullmatch(r"[\+\d\-\(\)\s]+", text):
        return None
    return _NAME_SUFFIX_PATTERN.sub("", text).strip().lower() or None


def _is_suffix_variant_name(value: str | None) -> bool:
    return bool(value and _NAME_SUFFIX_PATTERN.search(value.strip()))


def _display_name_score(value: str | None) -> tuple[int, int, int]:
    text = (value or "").strip()
    if not text:
        return (-1, -1, -1)
    words = [part for part in re.split(r"\s+", text) if part]
    alpha_chars = sum(ch.isalpha() for ch in text)
    return (
        0 if looks_like_phone_label(text) else 1,
        0 if _is_suffix_variant_name(text) else 1,
        (2 if len(words) >= 2 else 1) + alpha_chars,
    )


def _preferred_display_name(*values: str | None) -> str | None:
    candidates = [value.strip() for value in values if value and value.strip()]
    if not candidates:
        return None
    return max(candidates, key=_display_name_score)


def _extract_intro_name(message: MessageSample) -> tuple[str | None, str | None]:
    text = (message.text or "").strip()
    if not text:
        return (None, None)
    for pattern in _SELF_INTRO_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        name = match.group(1).strip(" ,.!?")
        if not name:
            continue
        first_name, last_name = _split_name(name)
        return (first_name, last_name)
    return (None, None)


def _apply_group_intro_name(person: PersonRecord, message: MessageSample) -> bool:
    first_name, last_name = _extract_intro_name(message)
    if not first_name:
        return False
    changed = False
    if not person.first_name:
        person.first_name = first_name
        changed = True
    if not person.last_name and last_name:
        person.last_name = last_name
        changed = True
    full_name = " ".join(part for part in [person.first_name, person.last_name] if part).strip()
    if not person.inferred_name:
        person.inferred_name = full_name or first_name
        changed = True
    if (not person.display_name or looks_like_phone_label(person.display_name)) and full_name:
        person.display_name = full_name
        changed = True
    elif (not person.display_name or looks_like_phone_label(person.display_name)) and first_name:
        person.display_name = first_name
        changed = True
    return changed


def _deterministic_relationship_hash(person: PersonRecord, rule_id: str) -> str:
    thread_hash = _thread_classification_hash(person.recent_messages) if person.recent_messages else "no-messages"
    return f"{_DETERMINISTIC_RELATIONSHIP_HASH_VERSION}:{rule_id}:{thread_hash}"


def _configured_user_last_name(settings=None) -> str:
    candidate = getattr(settings or get_settings(), "rolodex_user_last_name", None)
    return str(candidate or "Pilkington").strip()


def _split_name(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return (None, None)
    parts = [part for part in value.strip().split() if part]
    if not parts:
        return (None, None)
    if len(parts) == 1:
        return (parts[0], None)
    return (parts[0], " ".join(parts[1:]))


def _messages_db_path() -> Path:
    override = os.environ.get("ROLODEX_MESSAGES_DB") or os.environ.get("PILK_APPLE_MESSAGES_DB")
    return Path(override).expanduser() if override else CHAT_DB_PATH


def _normalize_limit(value: int | None) -> int | None:
    if value is None:
        return None
    return None if int(value) <= 0 else int(value)


def _fetch_thread_snapshots(
    *, max_threads: int | None, max_messages_per_thread: int | None
) -> list[ThreadSnapshot]:
    path = _messages_db_path()
    if not path.exists():
        return []
    titles = _chat_titles(path)
    thread_limit = _normalize_limit(max_threads)
    message_limit = _normalize_limit(max_messages_per_thread)
    snapshots: list[ThreadSnapshot] = []
    for thread in list_threads(limit=thread_limit, chat_db=path):
        messages = list(read_thread(thread.chat_id, limit=message_limit, chat_db=path))
        snapshots.append(
            ThreadSnapshot(
                chat_id=thread.chat_id,
                title=titles.get(thread.chat_id, thread.handle),
                is_group=thread.is_group,
                handle=thread.handle if len(thread.handles) == 1 else None,
                handles=thread.handles,
                last_at=thread.last_message_at.isoformat(),
                message_count=thread.message_count,
                last_message_direction="outbound" if thread.last_message_from_me else "inbound",
                messages=[
                    MessageSample(
                        rowid=message.rowid,
                        at=message.sent_at.isoformat(),
                        direction="outbound" if message.is_from_me else "inbound",
                        text=message.text,
                        handle=message.handle,
                        channel="imessage",
                    )
                    for message in messages
                ],
            )
        )
    return snapshots


def _thread_snapshot_for_chat_id(
    chat_id: int,
    *,
    title: str | None,
    message_limit: int | None,
    chat_db: Path,
    handle: str | None = None,
    handles: list[str] | None = None,
) -> ThreadSnapshot | None:
    messages = list(read_thread(chat_id, limit=message_limit, chat_db=chat_db))
    if not messages:
        return None
    resolved_handles = list(dict.fromkeys(handles or [msg.handle for msg in messages if msg.handle and msg.handle != "unknown"]))
    primary_handle = handle or (resolved_handles[0] if resolved_handles else None)
    newest = messages[0]
    return ThreadSnapshot(
        chat_id=chat_id,
        title=title or primary_handle or f"chat-{chat_id}",
        is_group=bool(len(resolved_handles) > 1 or newest.is_group_chat),
        handle=primary_handle if len(resolved_handles) <= 1 else None,
        handles=resolved_handles,
        last_at=newest.sent_at.isoformat(),
        message_count=len(messages),
        last_message_direction="outbound" if newest.is_from_me else "inbound",
        messages=[
            MessageSample(
                rowid=message.rowid,
                at=message.sent_at.isoformat(),
                direction="outbound" if message.is_from_me else "inbound",
                text=message.text,
                handle=message.handle,
                channel="imessage",
            )
            for message in messages
        ],
    )


def _chat_titles(path: Path) -> dict[int, str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT ROWID, display_name, chat_identifier FROM chat").fetchall()
    finally:
        conn.close()
    return {
        int(row[0]): str(row[1] or row[2] or f"chat-{row[0]}")
        for row in rows
    }


def _refresh_people_from_known_imessage_channels(
    store,
    *,
    chat_db: Path,
    message_limit: int | None,
    processed_chat_ids: set[int],
) -> int:
    if not chat_db.exists():
        return 0
    titles = _chat_titles(chat_db)
    refreshed = 0
    for person in list(store.people):
        latest_message_at = _parse_message_at(person.recent_messages[0].at) if person.recent_messages else None
        for channel in person.channels:
            if channel.type != "imessage" or channel.chat_id is None or channel.chat_id in processed_chat_ids:
                continue
            channel_last = _parse_message_at(channel.last_message_at)
            if channel_last is None:
                continue
            if latest_message_at is not None and channel_last <= latest_message_at:
                continue
            snapshot = _thread_snapshot_for_chat_id(
                int(channel.chat_id),
                title=titles.get(int(channel.chat_id)),
                message_limit=message_limit,
                chat_db=chat_db,
                handle=channel.handle,
                handles=[channel.handle] if channel.handle else None,
            )
            processed_chat_ids.add(int(channel.chat_id))
            if snapshot is None or snapshot.is_group or not snapshot.handle:
                continue
            upsert_person_from_thread(store, snapshot)
            refreshed += 1
            break
    return refreshed


def _stratified_messages(
    messages: list[MessageSample],
    *,
    recent: int = 40,
    middle: int = 30,
    oldest: int = 20,
) -> list[MessageSample]:
    """
    Sample messages across the relationship's timeline so the LLM doesn't
    see only the most recent slice. Critical for people the user has had
    multiple "phases" of relationship with — e.g. someone who's both family
    AND a recent business collaborator. If we only show the LLM the last
    30 messages and those are all business, it'll classify them as a
    business contact and miss the family signal entirely.

    Returns at most `recent + middle + oldest` messages, ordered oldest→newest
    so the prompt reads chronologically.

    `messages` is expected to be ordered newest-first (the default in
    PersonRecord.recent_messages).
    """
    if not messages:
        return []
    n = len(messages)
    if n <= recent + middle + oldest:
        # Small thread — just return everything in oldest→newest order.
        return list(reversed(messages))
    head = messages[:recent]              # newest
    tail = messages[-oldest:]             # oldest
    middle_pool = messages[recent : n - oldest]
    if middle_pool and middle > 0:
        # Evenly-spaced sample from the middle pool.
        if len(middle_pool) <= middle:
            middle_slice = middle_pool
        else:
            step = len(middle_pool) // middle
            middle_slice = [middle_pool[i] for i in range(0, len(middle_pool), step)][:middle]
    else:
        middle_slice = []
    combined = list(head) + list(middle_slice) + list(tail)
    # Now order oldest→newest.
    return list(reversed(combined))


def _thread_classifier_prompt(messages: list[MessageSample]) -> str:
    lines = [_SENSITIVE_THREAD_PROMPT, "", "Thread:"]
    for msg in _stratified_messages(messages, recent=30, middle=15, oldest=10):
        lines.append(f"{msg.direction}: {msg.text}")
    return "\n".join(lines)


def _relationship_hint_line(hint: str | None) -> list[str]:
    if not hint:
        return []
    return [f"Hint: {hint}", "Use the hint as a prior from contact metadata, but you may contradict it if the thread clearly disagrees.", ""]


def _thread_relationship_prompt(messages: list[MessageSample], hint: str | None = None) -> str:
    lines = [
        "Classify the relationship represented by this one-to-one message thread.",
        f"Return exactly one label from: {', '.join(_RELATIONSHIP_LABELS)}.",
        "The thread sample below is stratified across the relationship's full",
        "history — recent messages, middle slice, and earliest — so you can",
        "see how the relationship has evolved. Don't be misled by topic shifts:",
        "someone can be both family AND a current business collaborator.",
        "",
        *_relationship_hint_line(hint),
        "Thread (oldest → newest, stratified sample):",
    ]
    for msg in _stratified_messages(messages, recent=40, middle=20, oldest=15):
        lines.append(f"{msg.direction}: {msg.text}")
    return "\n".join(lines)


def _thread_profile_prompt(messages: list[MessageSample]) -> str:
    lines = [
        "Infer lightweight relationship metadata from this one-to-one message thread.",
        "Return strict JSON with keys inferred_name, context_summary, topics.",
        "inferred_name should be the other person's likely first name or null.",
        "context_summary should be ONE sentence describing how they know each",
        "other, how often they talk, AND the full picture of the relationship —",
        "if someone is both family and a business collaborator, both must be",
        "reflected. Don't define a person by only their most recent topic.",
        "topics should be a short JSON array of 3-6 lowercase topic strings",
        "covering the FULL history, not just recent messages.",
        "",
        "Thread (oldest → newest, stratified sample of the full history):",
    ]
    for msg in _stratified_messages(messages, recent=50, middle=30, oldest=20):
        lines.append(f"{msg.direction}: {msg.text}")
    return "\n".join(lines)


def classify_sensitive_thread(
    messages: list[MessageSample],
    classifier: Callable[[str], str] | None,
) -> list[str]:
    if classifier is None or not messages:
        return []
    raw = str(classifier(_thread_classifier_prompt(messages))).strip().upper()
    label = raw.splitlines()[0].strip() if raw else "NONE"
    if label not in _SENSITIVE_LABELS:
        label = "NONE"
    return [] if label == "NONE" else [label]


def classify_relationship_thread(
    messages: list[MessageSample],
    classifier: Callable[[str], str] | None,
    hint: str | None = None,
) -> str | None:
    if classifier is None or not messages:
        return None
    raw = str(classifier(_thread_relationship_prompt(messages, hint))).strip().lower()
    label = raw.splitlines()[0].strip() if raw else "unknown"
    return label if label in _RELATIONSHIP_LABELS else "unknown"


def _contact_relationship_prompt(person: PersonRecord, contact: ContactMatch, hint: str | None = None) -> str:
    return "\n".join(
        [
            "Classify the relationship for this person using contact metadata only.",
            f"Return exactly one label from: {', '.join(_RELATIONSHIP_LABELS)}.",
            *_relationship_hint_line(hint),
            f"Display name: {person.display_name or '-'}",
            f"First name: {person.first_name or contact.first_name or '-'}",
            f"Last name: {person.last_name or contact.last_name or '-'}",
            f"Organization: {person.company or person.contact_organization or contact.company or '-'}",
            f"Tags: {', '.join(person.contact_tags) or '-'}",
            f"Handles: {', '.join(person.handles) or '-'}",
        ]
    )


def _group_context_for_person(person: PersonRecord, store) -> Counter[str]:
    labels: Counter[str] = Counter()
    for group in person.group_threads:
        for handle in group.handles:
            if any(_handle_key(existing) == _handle_key(handle) for existing in person.handles):
                continue
            other = get_person_by_handle(store, handle)
            if other is None:
                continue
            label = (other.user_override_class or other.relationship_class or "").strip()
            if label and label != "unknown":
                labels[label] += 1
    return labels


def _unknown_number_prompt(person: PersonRecord, store, hint: str | None = None) -> str:
    group_context = _group_context_for_person(person, store)
    message_lines = []
    for message in person.recent_messages[:20]:
        text = (message.text or "").strip() or "(empty)"
        message_lines.append(f"{message.direction} | {message.at or ''} | {text}")
    return "\n".join(
        [
            "Classify this unknown phone-number contact.",
            f"Return exactly one label from: {', '.join(_RELATIONSHIP_LABELS)}.",
            "Use these signals: self-introduction patterns, group chat co-membership, timing patterns, and message style.",
            *_relationship_hint_line(hint),
            f"Handles: {', '.join(person.handles) or '-'}",
            f"Known group context: {dict(group_context) if group_context else '{}'}",
            "Messages:",
            *message_lines,
        ]
    )


def _looks_like_business_hours(message: MessageSample) -> bool:
    parsed = _parse_message_at(message.at)
    if parsed is None:
        return False
    local = parsed.astimezone()
    return local.weekday() < 5 and 9 <= local.hour < 17


def _classify_unknown_number(
    person: PersonRecord,
    store,
    classifier: Callable[[str], str] | None = None,
    hint: str | None = None,
) -> str:
    messages = list(person.recent_messages)
    group_context = _group_context_for_person(person, store)
    joined_text = "\n".join((message.text or "").lower() for message in messages)
    if re.search(r"\b(verification code|passcode|otp|security code)\b", joined_text):
        return "spam_or_verification"
    if re.search(r"\b(hi|hello|hey)[, ]+this is\b|\bit'?s [a-z]+ from\b|\b[a-z]+ here\b", joined_text):
        if group_context:
            top_label, _count = group_context.most_common(1)[0]
            if top_label in {"family", "partner", "close_friend", "casual_friend", "old_friend"}:
                return top_label
        return "personal" if "personal" in _RELATIONSHIP_LABELS else "casual_friend"
    if group_context:
        top_label, count = group_context.most_common(1)[0]
        if count >= 1:
            return top_label
    if messages:
        weekday_hits = sum(1 for message in messages if _looks_like_business_hours(message))
        long_messages = sum(1 for message in messages if len((message.text or "").split()) >= 12)
        short_messages = sum(1 for message in messages if len((message.text or "").split()) <= 4)
        if weekday_hits == len(messages) and short_messages >= max(1, len(messages) - 1):
            return "business"
        if short_messages >= max(2, len(messages) - 1):
            return "spam_or_verification"
        if long_messages >= max(1, len(messages) // 2):
            return "casual_friend"
    if classifier is not None:
        raw = str(classifier(_unknown_number_prompt(person, store, hint))).strip().lower()
        label = raw.splitlines()[0].strip() if raw else "unknown"
        if label in _RELATIONSHIP_LABELS:
            return label
    return "unknown"


def _classify_relationship_for_person(
    person: PersonRecord,
    *,
    store,
    classifier: Callable[[str], str] | None,
    user_last_name: str,
) -> str | None:
    inference = infer_relationship(person, store, user_last_name)
    if inference.label and inference.should_skip_llm:
        return inference.label
    hint = None
    if inference.label and 0.5 <= inference.confidence < 0.9:
        contact_name = person.display_name or person.first_name or (person.handles[0] if person.handles else person.person_id)
        hint = f"contact metadata suggests {inference.label} because {inference.reasoning.rstrip('.')}. Contact: {contact_name!r}."
    contact = _contact_match_for_person(person)
    if person.recent_messages and (len(person.recent_messages) >= 3 or contact is None):
        label = classify_relationship_thread(person.recent_messages, classifier, hint)
        return label or inference.label
    if contact is not None and classifier is not None:
        raw = str(classifier(_contact_relationship_prompt(person, contact, hint))).strip().lower()
        label = raw.splitlines()[0].strip() if raw else "unknown"
        return label if label in _RELATIONSHIP_LABELS else "unknown"
    if contact is not None:
        if inference.label and inference.confidence >= 0.5:
            return inference.label
        if person.company or person.contact_organization or contact.company:
            return "professional"
        return "unknown"
    label = _classify_unknown_number(person, store, classifier, hint)
    if label == "unknown" and inference.label and inference.confidence >= 0.5:
        return inference.label
    return label


def _extract_json_object(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("{") and text.endswith("}"):
        return json.loads(text)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("LLM response did not contain JSON")
    return json.loads(match.group(0))


def enrich_profile_from_thread(
    messages: list[MessageSample],
    enricher: Callable[[str], str] | None,
) -> dict | None:
    if enricher is None or not messages:
        return None
    parsed = _extract_json_object(str(enricher(_thread_profile_prompt(messages))))
    inferred_name = parsed.get("inferred_name")
    context_summary = parsed.get("context_summary")
    raw_topics = parsed.get("topics") or []
    topics = []
    if isinstance(raw_topics, list):
        for item in raw_topics:
            text = str(item).strip().lower()
            if text and text not in topics:
                topics.append(text)
            if len(topics) >= 5:
                break
    return {
        "inferred_name": str(inferred_name).strip() if inferred_name not in {None, ""} else None,
        "context_summary": str(context_summary).strip() if context_summary not in {None, ""} else None,
        "topics": topics,
    }


def _thread_classification_hash(messages: list[MessageSample]) -> str:
    payload = "\n".join(
        f"{msg.at or ''}|{msg.direction}|{msg.handle or ''}|{msg.text}"
        for msg in messages[:30]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _should_refresh_thread_metadata(
    cached_hash: str | None,
    cached_at: datetime | None,
    thread_hash: str,
    now: datetime,
) -> bool:
    if cached_hash != thread_hash:
        return True
    if not cached_at:
        return True
    stamped = cached_at if cached_at.tzinfo is not None else cached_at.replace(tzinfo=UTC)
    return (now - stamped.astimezone(UTC)) >= _THREAD_METADATA_TTL


def _should_refresh_classification(person: PersonRecord, thread_hash: str, now: datetime) -> bool:
    return _should_refresh_thread_metadata(
        person.sensitivity_classification_hash,
        person.sensitivity_classified_at,
        thread_hash,
        now,
    )


def _default_relationship_classifier(prompt: str) -> str:
    return llm_classify(prompt, labels=_RELATIONSHIP_LABELS)


def _default_profile_enricher(prompt: str) -> str:
    return llm_draft(
        system=(
            "You extract concise relationship metadata from a text thread. "
            "Return strict JSON only."
        ),
        user=prompt,
        max_tokens=250,
        temperature=0.1,
    ).text


def _default_sensitive_classifier(prompt: str) -> str:
    return llm_classify(prompt, labels=_SENSITIVE_LABELS)


async def _default_natural_end_llm(*, prompt: str, task_type: str | None = None) -> str:
    del task_type
    label = llm_classify(prompt, labels=["WAITING", "ENDED"])
    score = 0.9 if label == "ENDED" else 0.1
    reason = (
        "LLM classified the conversation as naturally ended."
        if label == "ENDED"
        else "LLM classified the conversation as still waiting for a reply."
    )
    return json.dumps({"score": score, "reason": reason})


def _llm_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and "PYTEST_CURRENT_TEST" not in os.environ


def _resolve_name_contact(handles: list[str], snapshot: list | None = None):
    for handle in handles:
        try:
            contact = lookup_by_phone(handle)
        except Exception:
            contact = None
        if contact:
            return contact
    for handle in handles:
        contact = lookup_in_snapshot(handle, snapshot)
        if contact:
            return contact
    return None


def _apply_contact_name(person: PersonRecord, contact) -> bool:
    if contact is None:
        return False
    changed = False
    if not person.display_name or re.fullmatch(r"[\+\d\-\(\)\s]+", person.display_name or ""):
        if contact.full_name and person.display_name != contact.full_name:
            person.display_name = contact.full_name
            changed = True
    if not person.first_name and contact.first_name:
        person.first_name = contact.first_name
        changed = True
    if not person.last_name and contact.last_name:
        person.last_name = contact.last_name
        changed = True
    if not person.inferred_name and (contact.first_name or contact.full_name):
        person.inferred_name = contact.first_name or contact.full_name
        changed = True
    return changed


def resolve_contact_metadata(handle: str) -> ContactMatch | None:
    query = handle.strip()
    if not query:
        return None
    try:
        direct = lookup_by_phone(query)
        if direct:
            return ContactMatch(
                query=query,
                matched_name=direct.name,
                first_name=direct.first_name,
                last_name=direct.last_name,
                emails=list(direct.emails),
                phones=list(direct.phones),
                company=direct.organization,
            )
        matches = search_by_name(query)
    except Exception:
        return None
    if not matches:
        return None
    normalized_digits = re.sub(r"\D+", "", query)
    normalized_lower = query.lower()
    best = None
    for match in matches:
        phones = [str(v) for v in match.phones]
        emails = [str(v) for v in match.emails]
        if any(re.sub(r"\D+", "", phone) == normalized_digits for phone in phones if normalized_digits):
            best = match
            break
        if any(email.lower() == normalized_lower for email in emails):
            best = match
            break
        if not best:
            best = match
    if best is None:
        return None
    return ContactMatch(
        query=query,
        matched_name=str(best.name or query),
        first_name=best.first_name,
        last_name=best.last_name,
        emails=[str(v) for v in best.emails],
        phones=[str(v) for v in best.phones],
        company=str(best.organization or "") or None,
    )


def upsert_person_from_thread(store, thread: ThreadSnapshot) -> PersonRecord:
    handle = thread.handle or (thread.handles[0] if thread.handles else thread.title)
    existing = get_person_by_handle(store, handle)
    created_at = existing.created_at if existing else _now_iso()
    contact = resolve_contact_metadata(handle)
    display_name = (
        (contact.matched_name if contact else None)
        or (existing.display_name if existing else None)
    )
    if not display_name:
        display_name = thread.title
    merged_messages = _merge_recent_messages(
        thread.messages,
        [message for message in (existing.recent_messages if existing else []) if message.rowid is not None],
    )
    rollup = _message_rollup(merged_messages)
    channels = _merge_channels(list(existing.channels if existing else []))
    thread_channel = Channel(
        type="imessage",
        handle=handle,
        chat_id=thread.chat_id,
        message_count=thread.message_count,
        last_message_at=thread.last_at,
        last_message_direction=thread.last_message_direction,
        active=True,
    )
    channels = _merge_channels(channels, [thread_channel])
    person = PersonRecord(
        person_id=existing.person_id if existing else f"imessage:{_handle_key(handle)}",
        display_name=display_name,
        first_name=(
            contact.first_name if contact else (existing.first_name if existing else None)
        ),
        last_name=(
            contact.last_name if contact else (existing.last_name if existing else None)
        ),
        inferred_name=existing.inferred_name if existing else None,
        company=contact.company if contact else (existing.company if existing else None),
        contact_organization=existing.contact_organization if existing else None,
        contact_tags=list(existing.contact_tags if existing else []),
        birthday=existing.birthday if existing else None,
        source=(
            "imessage+contacts"
            if (contact is not None or (existing and existing.source in {"contacts_only", "imessage+contacts"}))
            else "imessage"
        ),
        context_summary=existing.context_summary if existing else None,
        topics=list(existing.topics if existing else []),
        handles=list(dict.fromkeys([*(existing.handles if existing else []), *thread.handles])),
        connected_channels=infer_channels_from_handles(
            list(dict.fromkeys([*(existing.handles if existing else []), *thread.handles]))
        ),
        channels=channels,
        tone_profile=existing.tone_profile if existing else ToneProfile(),
        group_threads=list(existing.group_threads if existing else []),
        life_events=list(existing.life_events if existing else []),
        scoring=existing.scoring if existing else ScoringFields(),
        cadence=existing.cadence if existing else CadenceState(),
        recent_messages=merged_messages,
        relationship_class=existing.relationship_class if existing else None,
        tier=existing.tier if existing else "T3",
        user_priority=existing.user_priority if existing else 0.0,
        notes=existing.notes if existing else None,
        user_note=existing.user_note if existing else None,
        user_override_class=existing.user_override_class if existing else None,
        user_override_tier=existing.user_override_tier if existing else None,
        user_priority_boost=existing.user_priority_boost if existing else None,
        user_marked_at=existing.user_marked_at if existing else None,
        do_not_contact=existing.do_not_contact if existing else False,
        relationship_classification_hash=(
            existing.relationship_classification_hash if existing else None
        ),
        relationship_classified_at=existing.relationship_classified_at if existing else None,
        profile_enrichment_hash=existing.profile_enrichment_hash if existing else None,
        profile_enriched_at=existing.profile_enriched_at if existing else None,
        sensitivity_flags=list(existing.sensitivity_flags if existing else []),
        sensitivity_classification_hash=(
            existing.sensitivity_classification_hash if existing else None
        ),
        sensitivity_classified_at=existing.sensitivity_classified_at if existing else None,
        natural_end_classification=existing.natural_end_classification if existing else None,
        last_contacted=rollup["last_contacted"],
        last_message_at=rollup["last_message_at"],
        last_message_direction=rollup["last_message_direction"],
        inbound_message_count=int(rollup["inbound_message_count"]),
        outbound_message_count=int(rollup["outbound_message_count"]),
        created_at=created_at,
        updated_at=_now_iso(),
    )
    return upsert_person(store, person)


def _find_person_by_id(store, person_id: str) -> PersonRecord | None:
    for person in store.people:
        if person.person_id == person_id:
            return person
    return None


def _contact_match_for_person(person: PersonRecord) -> ContactMatch | None:
    handles = [channel.handle for channel in person.channels if channel.handle] or list(person.handles)
    for handle in handles:
        contact = resolve_contact_metadata(handle)
        if contact:
            return contact
    return None


def _merge_people_records(primary: PersonRecord, duplicate: PersonRecord) -> PersonRecord:
    merged_messages = _merge_recent_messages(primary.recent_messages, duplicate.recent_messages)
    rollup = _message_rollup(merged_messages)
    handles = list(dict.fromkeys([*primary.handles, *duplicate.handles]))
    channels = _merge_channels(primary.channels, duplicate.channels)
    group_threads = _merge_group_threads(primary.group_threads, duplicate.group_threads)
    topics = list(dict.fromkeys([*primary.topics, *duplicate.topics]))
    sensitivity_flags = list(dict.fromkeys([*primary.sensitivity_flags, *duplicate.sensitivity_flags]))
    merged = primary.model_copy(deep=True)
    merged.display_name = _preferred_display_name(
        primary.display_name,
        duplicate.display_name,
        primary.inferred_name,
        duplicate.inferred_name,
    ) or _choose_value(
        primary.display_name,
        duplicate.display_name,
        primary.inferred_name,
        duplicate.inferred_name,
        handles[0] if handles else primary.person_id,
    )
    merged.first_name = _choose_value(primary.first_name, duplicate.first_name)
    merged.last_name = _choose_value(primary.last_name, duplicate.last_name)
    merged.inferred_name = _choose_value(primary.inferred_name, duplicate.inferred_name)
    merged.company = _choose_value(primary.company, duplicate.company)
    merged.contact_organization = _choose_value(primary.contact_organization, duplicate.contact_organization)
    merged.contact_tags = list(dict.fromkeys([*primary.contact_tags, *duplicate.contact_tags]))
    merged.birthday = _choose_value(primary.birthday, duplicate.birthday)
    merged.source = (
        "imessage+contacts"
        if (
            "imessage+contacts" in {primary.source, duplicate.source}
            or {primary.source, duplicate.source} == {"imessage", "contacts_only"}
        )
        else ("contacts_only" if primary.source == duplicate.source == "contacts_only" else "imessage")
    )
    merged.notes = _choose_value(primary.notes, duplicate.notes)
    merged.user_note = _choose_value(primary.user_note, duplicate.user_note)
    merged.user_override_class = _choose_value(primary.user_override_class, duplicate.user_override_class)
    merged.user_override_tier = _choose_value(primary.user_override_tier, duplicate.user_override_tier)
    merged.user_priority_boost = _choose_value(primary.user_priority_boost, duplicate.user_priority_boost)
    merged.user_marked_at = _choose_value(primary.user_marked_at, duplicate.user_marked_at)
    merged.context_summary = _choose_value(primary.context_summary, duplicate.context_summary)
    merged.topics = topics
    merged.handles = handles
    merged.connected_channels = infer_channels_from_handles(handles)
    merged.channels = channels
    merged.group_threads = group_threads
    merged.recent_messages = merged_messages
    merged.relationship_class = (
        primary.relationship_class
        if primary.relationship_class not in {None, "unknown"}
        else duplicate.relationship_class
    )
    merged.tier = _choose_value(primary.tier, duplicate.tier, "T3")
    merged.user_priority = max(primary.user_priority, duplicate.user_priority)
    merged.do_not_contact = primary.do_not_contact or duplicate.do_not_contact
    merged.sensitivity_flags = sensitivity_flags
    merged.last_message_at = rollup["last_message_at"]
    merged.last_message_direction = rollup["last_message_direction"]
    merged.last_contacted = max(
        [item for item in [primary.last_contacted, duplicate.last_contacted, rollup["last_contacted"]] if item],
        default=rollup["last_contacted"],
    )
    merged.inbound_message_count = primary.inbound_message_count + duplicate.inbound_message_count
    merged.outbound_message_count = primary.outbound_message_count + duplicate.outbound_message_count
    merged.relationship_classification_hash = None
    merged.relationship_classified_at = None
    merged.profile_enrichment_hash = None
    merged.profile_enriched_at = None
    merged.sensitivity_classification_hash = None
    merged.sensitivity_classified_at = None
    merged.natural_end_classification = None
    merged.created_at = _choose_value(primary.created_at, duplicate.created_at, _now_iso())
    merged.updated_at = _now_iso()
    return merged


def _relink_duplicate_handles(store) -> int:
    key_to_people: dict[str, set[str]] = defaultdict(set)
    for person in store.people:
        candidate_handles = [*person.handles, *[channel.handle for channel in person.channels if channel.handle]]
        for handle in candidate_handles:
            key = _handle_key(handle)
            if key.isdigit():
                key_to_people[key].add(person.person_id)

    person_map = {person.person_id: person for person in store.people}
    visited: set[str] = set()
    merged_count = 0

    for person in list(store.people):
        if person.person_id in visited or person.person_id not in person_map:
            continue
        queue = [person.person_id]
        component: set[str] = set()
        while queue:
            person_id = queue.pop()
            if person_id in component or person_id not in person_map:
                continue
            component.add(person_id)
            current = person_map[person_id]
            handles = [*current.handles, *[channel.handle for channel in current.channels if channel.handle]]
            for handle in handles:
                key = _handle_key(handle)
                if not key.isdigit():
                    continue
                for related_id in key_to_people.get(key, set()):
                    if related_id not in component:
                        queue.append(related_id)
        visited.update(component)
        if len(component) <= 1:
            continue
        ordered = sorted(
            (person_map[person_id] for person_id in component),
            key=lambda record: (
                -len(record.recent_messages),
                -(record.inbound_message_count + record.outbound_message_count),
                0 if record.source == "imessage+contacts" else 1,
                record.created_at or "",
                record.person_id,
            ),
        )
        merged = ordered[0]
        for duplicate in ordered[1:]:
            merged = _merge_people_records(merged, duplicate)
            person_map.pop(duplicate.person_id, None)
            merged_count += 1
        person_map[merged.person_id] = merged

    if merged_count:
        store.people = list(person_map.values())
    return merged_count


def _merge_people_by_ids(store, person_ids: set[str]) -> int:
    if len(person_ids) <= 1:
        return 0
    person_map = {person.person_id: person for person in store.people}
    ordered = sorted(
        (person_map[person_id] for person_id in person_ids if person_id in person_map),
        key=lambda record: (
            -len(record.recent_messages),
            -(record.inbound_message_count + record.outbound_message_count),
            -len(_normalized_email_keys(record)),
            -len([part for part in [record.first_name, record.last_name] if part]),
            0 if record.source == "imessage+contacts" else 1,
            record.created_at or "",
            record.person_id,
        ),
    )
    if len(ordered) <= 1:
        return 0
    merged = ordered[0]
    merged_count = 0
    for duplicate in ordered[1:]:
        merged = _merge_people_records(merged, duplicate)
        person_map.pop(duplicate.person_id, None)
        merged_count += 1
    person_map[merged.person_id] = merged
    store.people = list(person_map.values())
    return merged_count


def _merge_duplicate_people_by_name_or_email(store) -> int:
    email_groups: dict[str, set[str]] = defaultdict(set)
    name_groups: dict[str, set[str]] = defaultdict(set)

    for person in store.people:
        for email in _normalized_email_keys(person):
            email_groups[email].add(person.person_id)
        base_name = _display_name_base(person.display_name)
        if base_name:
            name_groups[base_name].add(person.person_id)

    total_merged = 0
    for group in email_groups.values():
        total_merged += _merge_people_by_ids(store, set(group))

    for base_name, group in name_groups.items():
        if len(group) <= 1:
            continue
        names = {
            (next((person.display_name for person in store.people if person.person_id == person_id), "") or "").strip().lower()
            for person_id in group
        }
        if base_name not in names:
            continue
        if not any(_is_suffix_variant_name(name) for name in names):
            continue
        total_merged += _merge_people_by_ids(store, set(group))

    return total_merged


def merge_duplicate_people(path: Path | None = None, *, settings=None) -> dict[str, int]:
    settings = settings or get_settings()
    path = path or store_path(settings)
    with store_transaction(path) as store:
        before = len(store.people)
        merged = 0
        while True:
            cycle = _relink_duplicate_handles(store)
            cycle += _merge_duplicate_people_by_name_or_email(store)
            if cycle <= 0:
                break
            merged += cycle
        after = len(store.people)
    return {"before": before, "after": after, "merged": merged}


def _propagate_group_relationship_context(store, thread: ThreadSnapshot) -> None:
    known_labels: Counter[str] = Counter()
    for handle in thread.handles:
        person = get_person_by_handle(store, handle)
        if person is None:
            continue
        label = (person.user_override_class or person.relationship_class or "").strip()
        if label in {"family", "partner", "close_friend", "casual_friend", "old_friend"}:
            known_labels[label] += 1
    if not known_labels:
        return
    inferred_label, _count = known_labels.most_common(1)[0]
    for handle in thread.handles:
        person = get_person_by_handle(store, handle)
        if person is None:
            continue
        if person.user_override_class:
            continue
        if person.relationship_class in {None, "unknown", "group_chat_member"}:
            person.relationship_class = inferred_label


def _needs_relationship_enrichment(person: PersonRecord, now: datetime) -> bool:
    if not person.recent_messages:
        return person.relationship_class in {None, "unknown"}
    thread_hash = _thread_classification_hash(person.recent_messages)
    return _should_refresh_thread_metadata(
        person.relationship_classification_hash,
        person.relationship_classified_at,
        thread_hash,
        now,
    )


def _needs_profile_enrichment(person: PersonRecord, now: datetime) -> bool:
    if not person.recent_messages:
        return False
    thread_hash = _thread_classification_hash(person.recent_messages)
    return _should_refresh_thread_metadata(
        person.profile_enrichment_hash,
        person.profile_enriched_at,
        thread_hash,
        now,
    )


def _needs_sensitivity_enrichment(person: PersonRecord, now: datetime) -> bool:
    if not person.recent_messages:
        return False
    thread_hash = _thread_classification_hash(person.recent_messages)
    return _should_refresh_classification(person, thread_hash, now)


def _should_enrich_person(person: PersonRecord, now: datetime, *, missing_only: bool = True) -> bool:
    if not person.channels and not person.handles:
        return False
    if not missing_only:
        return True
    if person.relationship_class in {None, "unknown"}:
        return True
    if person.recent_messages and (person.inferred_name is None or person.context_summary is None or not person.topics):
        return True
    if person.recent_messages and not person.sensitivity_classified_at:
        return True
    if person.recent_messages and person.natural_end_classification is None:
        return True
    return any(
        (
            _needs_relationship_enrichment(person, now),
            _needs_profile_enrichment(person, now) if person.recent_messages else False,
            _needs_sensitivity_enrichment(person, now) if person.recent_messages else False,
        )
    )


def enrich_person_record(
    person: PersonRecord,
    *,
    store=None,
    now: datetime | None = None,
    sensitive_classifier: Callable[[str], str] | None = None,
    relationship_classifier: Callable[[str], str] | None = None,
    profile_enricher: Callable[[str], str] | None = None,
    natural_end_llm: Callable[..., object] | None = None,
    force_reclassify: bool = False,
) -> tuple[PersonRecord, list[str]]:
    now = now or datetime.now(UTC)
    warnings: list[str] = []
    thread_hash = _thread_classification_hash(person.recent_messages)
    user_last_name = _configured_user_last_name()

    relationship_needs_refresh = force_reclassify or _needs_relationship_enrichment(person, now)
    if relationship_needs_refresh:
        deterministic = infer_relationship(person, store, user_last_name)
        try:
            relationship_class = _classify_relationship_for_person(
                person,
                store=store,
                classifier=relationship_classifier,
                user_last_name=user_last_name,
            )
        except Exception as exc:
            warnings.append(f"relationship classification failed for {person.person_id}: {exc}")
        else:
            if relationship_class:
                person.relationship_class = relationship_class
                if deterministic.label == relationship_class and deterministic.should_skip_llm:
                    person.relationship_classification_hash = _deterministic_relationship_hash(person, deterministic.rule_id)
                else:
                    person.relationship_classification_hash = thread_hash or "no-messages"
                person.relationship_classified_at = now

    if person.recent_messages and (
        force_reclassify
        or _should_refresh_thread_metadata(
        person.profile_enrichment_hash,
        person.profile_enriched_at,
        thread_hash,
        now,
        )
    ):
        try:
            profile = enrich_profile_from_thread(person.recent_messages, profile_enricher)
        except Exception as exc:
            warnings.append(f"profile enrichment failed for {person.person_id}: {exc}")
        else:
            if profile:
                person.inferred_name = profile["inferred_name"] or person.inferred_name
                person.context_summary = profile["context_summary"] or person.context_summary
                person.topics = profile["topics"] or person.topics
            if person.inferred_name is None and profile_enricher is not None:
                handles = [channel.handle for channel in person.channels if channel.handle] or list(person.handles)
                contact = _resolve_name_contact(handles)
                if contact:
                    person.inferred_name = contact.first_name or contact.name
            person.profile_enrichment_hash = thread_hash
            person.profile_enriched_at = now

    if person.recent_messages and (force_reclassify or _should_refresh_classification(person, thread_hash, now)):
        try:
            person.sensitivity_flags = classify_sensitive_thread(person.recent_messages, sensitive_classifier)
            person.sensitivity_classification_hash = thread_hash
            person.sensitivity_classified_at = now
        except Exception as exc:
            warnings.append(f"sensitivity classification failed for {person.person_id}: {exc}")

    if person.recent_messages and natural_end_llm is not None:
        try:
            if force_reclassify:
                person.natural_end_classification = None
            asyncio.run(classify_natural_end(person, natural_end_llm))
        except Exception as exc:
            warnings.append(f"natural-end classification failed for {person.person_id}: {exc}")

    return person, warnings


def enrich_people(
    *,
    settings=None,
    path: Path | None = None,
    missing_only: bool = True,
    sensitive_classifier: Callable[[str], str] | None = None,
    relationship_classifier: Callable[[str], str] | None = None,
    profile_enricher: Callable[[str], str] | None = None,
    natural_end_llm: Callable[..., object] | None = None,
    force_reclassify: bool = False,
) -> dict[str, int | list[str]]:
    settings = settings or get_settings()
    path = path or store_path(settings)
    if sensitive_classifier is None and _llm_enabled():
        sensitive_classifier = _default_sensitive_classifier
    if relationship_classifier is None and _llm_enabled():
        relationship_classifier = _default_relationship_classifier
    if profile_enricher is None and _llm_enabled():
        profile_enricher = _default_profile_enricher
    if natural_end_llm is None and _llm_enabled():
        natural_end_llm = _default_natural_end_llm

    snapshot = load_store(path)
    person_ids = [person.person_id for person in snapshot.people]
    warnings: list[str] = []
    processed = 0
    updated = 0
    now = datetime.now(UTC)

    for person_id in person_ids:
        current = load_store(path)
        person = _find_person_by_id(current, person_id)
        if person is None or not _should_enrich_person(person, now, missing_only=missing_only):
            continue
        processed += 1
        before = person.model_dump()
        person, person_warnings = enrich_person_record(
            person,
            store=current,
            now=datetime.now(UTC),
            sensitive_classifier=sensitive_classifier,
            relationship_classifier=relationship_classifier,
            profile_enricher=profile_enricher,
            natural_end_llm=natural_end_llm,
            force_reclassify=force_reclassify,
        )
        warnings.extend(person_warnings)
        if person.model_dump() != before:
            updated += 1
        with store_transaction(path) as store:
            live = _find_person_by_id(store, person_id)
            if live is None:
                continue
            upsert_person(store, person)

    return {"processed": processed, "updated": updated, "warnings": warnings}


def verify_contact_names(*, settings=None, path: Path | None = None) -> dict[str, int]:
    settings = settings or get_settings()
    path = path or store_path(settings)
    snapshot = load_contacts_snapshot()
    checked = 0
    updated = 0
    with store_transaction(path) as store:
        for person in store.people:
            checked += 1
            current_name = (person.display_name or "").strip()
            if current_name and not (re.fullmatch(r"[\+\d\-\(\)\s]+", current_name) or current_name.startswith("••••")):
                continue
            handles = [channel.handle for channel in person.channels if channel.handle] or list(person.handles)
            if not handles:
                continue
            contact = None
            if snapshot:
                for handle in handles:
                    contact = lookup_in_snapshot(handle, snapshot)
                    if contact:
                        break
            if contact is None and not snapshot:
                contact = _resolve_name_contact(handles, snapshot)
            if _apply_contact_name(person, contact):
                person.updated_at = _now_iso()
                updated += 1
    return {"checked": checked, "updated": updated}


def collect_store_audit_stats(store) -> dict[str, object]:
    year_counts: Counter[str] = Counter()
    zero_recent = 0
    with_recent = 0
    classified = 0
    unclassified = 0
    top_people: list[dict[str, object]] = []

    for person in store.people:
        if person.recent_messages:
            with_recent += 1
        else:
            zero_recent += 1
        relationship_class = (person.user_override_class or person.relationship_class or "unknown").strip() or "unknown"
        if relationship_class == "unknown":
            unclassified += 1
        else:
            classified += 1
        year = "none"
        parsed = _parse_message_at(person.last_message_at)
        if parsed is not None:
            year = str(parsed.year)
        year_counts[year] += 1
        top_people.append(
            {
                "person_id": person.person_id,
                "display_name": person.display_name or person.inferred_name or (person.handles[0] if person.handles else person.person_id),
                "message_count": int(person.inbound_message_count or 0) + int(person.outbound_message_count or 0),
                "last_message_year": year,
                "relationship_class": relationship_class,
            }
        )

    top_people.sort(
        key=lambda item: (int(item["message_count"]), str(item["last_message_year"]), str(item["display_name"])),
        reverse=True,
    )
    return {
        "year_histogram": dict(sorted(year_counts.items(), key=lambda item: item[0], reverse=True)),
        "zero_recent_messages": zero_recent,
        "with_recent_messages": with_recent,
        "classified_people": classified,
        "unclassified_people": unclassified,
        "top_active_people": top_people[:10],
    }


def _relationship_bucket_counts(store) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for person in store.people:
        label = (person.user_override_class or person.relationship_class or "unknown").strip() or "unknown"
        counts[label] += 1
    return dict(sorted(counts.items()))


def reclassify_people_deterministically(path: Path | None = None, *, settings=None) -> dict[str, object]:
    settings = settings or get_settings()
    path = path or store_path(settings)
    now = datetime.now(UTC)
    with store_transaction(path) as store:
        before_counts = _relationship_bucket_counts(store)
        applied_by_rule: Counter[str] = Counter()
        updated = 0
        preserved_overrides = 0
        # Rules whose evidence is contact-metadata based (first_name, last_name,
        # surname cluster, etc.) — these are MORE authoritative than the LLM's
        # content-based guesses, so we apply them at confidence >= 0.8 even
        # without should_skip_llm.
        contact_metadata_rules = {
            "operator_note_role",
            "operator_note_business_partner",
            "role_name_first_name",
            "family_prefix_display_name",
            "spouse_in_law_marker",
            "spouse_in_law_marker_explicit",
            "spouse_in_law_marker_standalone",
            "spouse_in_law_marker_inlaw",
            "romantic_partner_message_signal",
            "surname_match_with_user",
            "surname_cluster_family",
            "surname_cluster_family_self_seeded",
            "service_provider_keyword",
            "landlord_context_override",
            "outbound_family_pet_name",
        }
        for person in store.people:
            if person.user_override_class:
                preserved_overrides += 1
                continue
            inference = infer_relationship(person, store, _configured_user_last_name(settings))
            if not inference.label:
                continue
            # Apply when: skip_llm threshold (>=0.9), OR a contact-metadata-based
            # rule fired with confidence >= 0.8 (overrides LLM hallucinations
            # like "Stephenie Tocado" → business).
            should_apply = (
                inference.should_skip_llm
                or (inference.confidence >= 0.8 and inference.rule_id in contact_metadata_rules)
            )
            if not should_apply:
                continue
            if (
                person.relationship_class != inference.label
                or person.relationship_classification_hash != _deterministic_relationship_hash(person, inference.rule_id)
            ):
                person.relationship_class = inference.label
                person.relationship_classification_hash = _deterministic_relationship_hash(person, inference.rule_id)
                person.relationship_classified_at = now
                person.updated_at = now.isoformat()
                updated += 1
            applied_by_rule[inference.rule_id] += 1
        after_counts = _relationship_bucket_counts(store)
    return {
        "updated": updated,
        "preserved_overrides": preserved_overrides,
        "applied_by_rule": dict(sorted(applied_by_rule.items())),
        "before_counts": before_counts,
        "after_counts": after_counts,
    }


def sync_imessage_threads(
    *,
    store=None,
    settings=None,
    max_threads: int | None = None,
    max_messages_per_thread: int | None = None,
    enrich: bool = False,
    sensitive_classifier: Callable[[str], str] | None = None,
    relationship_classifier: Callable[[str], str] | None = None,
    profile_enricher: Callable[[str], str] | None = None,
) -> SyncReport:
    settings = settings or get_settings()
    path = store_path(settings)
    owns_store = store is None
    store = store or load_store(path)
    report = SyncReport(store_path=str(path))
    now = datetime.now(UTC)
    chat_db = _messages_db_path()
    processed_chat_ids: set[int] = set()
    for thread in _fetch_thread_snapshots(
        max_threads=max_threads,
        max_messages_per_thread=max_messages_per_thread,
    ):
        report.scanned_threads += 1
        processed_chat_ids.add(thread.chat_id)
        if thread.is_group:
            report.skipped_group_threads += 1
            for handle in thread.handles:
                person = get_person_by_handle(store, handle)
                if person is None:
                    contact = resolve_contact_metadata(handle)
                    person = PersonRecord(
                        person_id=f"imessage:{_handle_key(handle)}",
                        display_name=(contact.matched_name if contact else handle),
                        first_name=contact.first_name if contact else None,
                        last_name=contact.last_name if contact else None,
                        company=contact.company if contact else None,
                        relationship_class="group_chat_member",
                        handles=[handle],
                        connected_channels=infer_channels_from_handles([handle]),
                        source="imessage+contacts" if contact is not None else "imessage",
                        created_at=_now_iso(),
                    )
                elif not person.relationship_class and not person.recent_messages:
                    person.relationship_class = "group_chat_member"
                for message in thread.messages[:30]:
                    if message.direction != "inbound":
                        continue
                    if _handle_key(message.handle or "") != _handle_key(handle):
                        continue
                    _apply_group_intro_name(person, message)
                group = GroupThread(
                    chat_id=thread.chat_id,
                    title=thread.title,
                    handles=thread.handles,
                    last_message_at=thread.last_at,
                )
                if not any(existing.chat_id == group.chat_id for existing in person.group_threads):
                    person.group_threads.append(group)
                    report.tagged_group_threads += 1
                upsert_person(store, person)
            continue
        handle = thread.handle
        if not handle:
            report.warnings.append(f"thread {thread.chat_id} has no handle")
            continue
        before = get_person_by_handle(store, handle)
        person = upsert_person_from_thread(store, thread)
        upsert_person(store, person)
        if before is None:
            report.created_people += 1
        else:
            report.updated_people += 1
        report.people.append(person.person_id)
    repaired_threads = _refresh_people_from_known_imessage_channels(
        store,
        chat_db=chat_db,
        message_limit=_normalize_limit(max_messages_per_thread),
        processed_chat_ids=processed_chat_ids,
    )
    if repaired_threads:
        report.warnings.append(f"refreshed {repaired_threads} stale iMessage channel records")
    duplicate_merges = 0
    while True:
        cycle = _relink_duplicate_handles(store)
        cycle += _merge_duplicate_people_by_name_or_email(store)
        if cycle <= 0:
            break
        duplicate_merges += cycle
    if duplicate_merges:
        report.warnings.append(f"merged {duplicate_merges} duplicate people records")
    store.last_sync_at = now.isoformat()
    auto_assign_tiers(store, today=now.date())
    if owns_store:
        save_store(path, store)
        report.total_people = len(load_store(path).people)
        if enrich:
            enrichment = enrich_people(
                settings=settings,
                path=path,
                sensitive_classifier=sensitive_classifier,
                relationship_classifier=relationship_classifier,
                profile_enricher=profile_enricher,
                force_reclassify=False,
            )
            report.warnings.extend(list(enrichment["warnings"]))
    else:
        report.total_people = len(store.people)
        if enrich:
            if sensitive_classifier is None and _llm_enabled():
                sensitive_classifier = _default_sensitive_classifier
            if relationship_classifier is None and _llm_enabled():
                relationship_classifier = _default_relationship_classifier
            if profile_enricher is None and _llm_enabled():
                profile_enricher = _default_profile_enricher
            natural_end_llm = _default_natural_end_llm if _llm_enabled() else None
            for person in store.people:
                if not person.channels or not person.recent_messages:
                    continue
                person, warnings = enrich_person_record(
                    person,
                    store=store,
                    now=datetime.now(UTC),
                    sensitive_classifier=sensitive_classifier,
                    relationship_classifier=relationship_classifier,
                    profile_enricher=profile_enricher,
                    natural_end_llm=natural_end_llm,
                    force_reclassify=False,
                )
                report.warnings.extend(warnings)
    return report
