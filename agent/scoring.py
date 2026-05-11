from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, date, datetime, timedelta

from agent.models import CadenceState, NaturalEndClassification, NaturalEndResult, PersonRecord, RolodexStore
from agent.person_utils import effective_relationship_class, effective_tier

DEFAULT_TIER_DAYS = {
    "T1": 14,
    "T2": 45,
    "T3": 90,
    "T4": 180,
    "T5": 365,
}

_NATURAL_END_TERMINALS = {
    "lol",
    "k",
    "ok",
    "haha",
    "lmao",
    "thanks",
    "ty",
    "👍",
}
_CLASSIFICATION_TTL = timedelta(days=7)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.fromisoformat(value).date()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _total_messages(person: PersonRecord) -> int:
    counted = int(person.inbound_message_count or 0) + int(person.outbound_message_count or 0)
    channel_total = sum(int(channel.message_count or 0) for channel in person.channels)
    return max(counted, channel_total)


def _days_since_last_contact(person: PersonRecord, today: date) -> int | None:
    last_contacted = _parse_date(person.last_contacted or person.last_message_at)
    if last_contacted is None:
        return None
    return max(0, (today - last_contacted).days)


def _target_days_for(person: PersonRecord, settings) -> int:
    configured = getattr(settings, "rolodex_tier_days", None)
    tier = effective_tier(person)
    if isinstance(configured, dict):
        try:
            return int(configured.get(tier, DEFAULT_TIER_DAYS[tier]))
        except (TypeError, ValueError, KeyError):
            pass
    return DEFAULT_TIER_DAYS.get(tier, 90)


def compute_cadence(person: PersonRecord, settings, today: date) -> CadenceState:
    target_days = _target_days_for(person, settings)
    days_since_last = _days_since_last_contact(person, today)
    snooze_until = _parse_date(person.cadence.snooze_until)
    last_sent_at = _parse_date(person.cadence.last_sent_at)
    if days_since_last is None:
        is_overdue = False
        days_overdue = 0
    else:
        is_overdue = days_since_last > target_days
        days_overdue = max(0, days_since_last - target_days)
    if snooze_until and today <= snooze_until:
        is_overdue = False
        days_overdue = 0
    # Send debounce: if we (or the operator) sent something in the last
    # min(target_days/2, 7) days, treat the cadence as already fulfilled —
    # otherwise people stay "due now" forever after you message them.
    if last_sent_at is not None:
        debounce_days = max(7, target_days // 2)
        if (today - last_sent_at).days < debounce_days:
            is_overdue = False
            days_overdue = 0
    # Holiday override: if today is a holiday relevant to this person
    # (Mother's Day → mom, etc.), force them into Due Now — unless we just
    # sent them a message (debounce wins, you don't need to text mom twice).
    try:
        from agent.holidays import compute_holiday_boosts
        holidays_today = compute_holiday_boosts(person, today)
    except Exception:
        holidays_today = []
    debounced = (
        last_sent_at is not None
        and (today - last_sent_at).days < max(2, target_days // 2)
    )
    if holidays_today and not debounced and not (snooze_until and today <= snooze_until):
        is_overdue = True
        # Surface "days_overdue" as a synthetic positive value so they sort high
        # but don't blow out the existing scoring math.
        days_overdue = max(days_overdue, 1)
    cadence = person.cadence.model_copy(
        update={
            "tier": effective_tier(person),
            "target_days": target_days,
            "days_since_last": days_since_last,
            "is_overdue": is_overdue,
            "days_overdue": days_overdue,
        }
    )
    person.cadence = cadence
    return cadence


def compute_priority(person: PersonRecord, settings, today: date) -> float | None:
    manual_boost = int(person.user_priority_boost or 0)
    if person.do_not_contact or manual_boost <= -50:
        person.scoring.priority_score = 0.0
        return None

    cadence = compute_cadence(person, settings, today)
    target_days = cadence.target_days or _target_days_for(person, settings)
    days_since = cadence.days_since_last or 0
    total_messages = _total_messages(person)
    outbound = max(int(person.outbound_message_count or 0), 1 if person.last_message_direction == "outbound" else 0)
    inbound = max(int(person.inbound_message_count or 0), 1 if person.last_message_direction == "inbound" else 0)

    volume_score = _clamp(math.log1p(total_messages) / math.log1p(600))
    recency_score = _clamp(days_since / 365)
    overdue_score = _clamp(cadence.days_overdue / max(target_days, 1))
    if total_messages <= 0:
        reciprocity_score = 0.0
    else:
        outbound_share = outbound / total_messages
        reciprocity_score = 1.0 - _clamp(abs(outbound_share - 0.45) / 0.45)
    user_priority_score = _clamp(float(person.user_priority or 0.0))

    score = (
        volume_score * 28.0
        + recency_score * 22.0
        + reciprocity_score * 16.0
        + overdue_score * 24.0
        + user_priority_score * 10.0
        + manual_boost
    )

    # Holiday boost — Mother's Day, Father's Day, birthday, etc. Adds directly
    # to the 0–100 score. A 70-point boost on the day reliably surfaces mom
    # to the top of the queue regardless of cadence state.
    from agent.holidays import total_holiday_boost
    holiday_total, _ = total_holiday_boost(person, today)
    score += holiday_total

    final_score = round(_clamp(score / 100.0 if score > 100 else score / 100.0, 0.0, 1.0) * 100, 2)
    if score <= 100:
        final_score = round(_clamp(score, 0.0, 100.0), 2)
    else:
        final_score = 100.0

    person.scoring.priority_score = final_score
    person.scoring.warmth = round(volume_score, 4)
    person.scoring.responsiveness = round(reciprocity_score, 4)
    person.scoring.freshness_decay = round(recency_score, 4)
    person.scoring.user_priority_boost = round(user_priority_score, 4)
    person.scoring.life_event_proximity = round(overdue_score, 4)
    return final_score


def excluded_from_priority(person: PersonRecord) -> bool:
    return person.do_not_contact or int(person.user_priority_boost or 0) <= -50


def active_relationship_class(person: PersonRecord) -> str | None:
    return effective_relationship_class(person)


def active_tier(person: PersonRecord) -> str:
    return effective_tier(person)


def auto_assign_tiers(store: RolodexStore, *, today: date | None = None) -> dict[str, int]:
    today = today or datetime.now(UTC).date()
    ranked = sorted(
        store.people,
        key=lambda person: (
            _total_messages(person),
            int(person.outbound_message_count or 0),
            -(_days_since_last_contact(person, today) or 9_999),
        ),
        reverse=True,
    )
    total_people = max(1, len(ranked))
    targets = {
        "T1": max(1, math.ceil(total_people * 0.05)),
        "T2": max(1, math.ceil(total_people * 0.10)),
        "T3": max(1, math.ceil(total_people * 0.25)),
    }
    assigned: set[str] = set()

    for person in ranked:
        if person.user_override_tier:
            person.tier = person.user_override_tier
            assigned.add(person.person_id)

    def _assign(label: str, limit: int, score_fn) -> None:
        remaining = [
            person
            for person in ranked
            if person.person_id not in assigned
            and person.source != "contacts_only"
        ]
        scored = sorted(remaining, key=score_fn, reverse=True)
        for person in scored[:limit]:
            person.tier = label
            assigned.add(person.person_id)

    def _t1_score(person: PersonRecord) -> tuple[int, int, int, int]:
        days = _days_since_last_contact(person, today)
        return (
            1 if (int(person.outbound_message_count or 0) > 0 or person.last_message_direction == "outbound") else 0,
            1 if days is not None and days <= 14 else 0,
            _total_messages(person),
            -(days or 9_999),
        )

    def _t2_score(person: PersonRecord) -> tuple[int, int, int, int]:
        days = _days_since_last_contact(person, today)
        return (
            1 if (int(person.outbound_message_count or 0) > 5 or person.last_message_direction == "outbound") else 0,
            1 if days is not None and days <= 60 else 0,
            _total_messages(person),
            -(days or 9_999),
        )

    def _t3_score(person: PersonRecord) -> tuple[int, int, int]:
        days = _days_since_last_contact(person, today)
        return (
            1 if days is not None and days <= 180 else 0,
            _total_messages(person),
            -(days or 9_999),
        )

    _assign("T1", targets["T1"], _t1_score)
    _assign("T2", targets["T2"], _t2_score)
    _assign("T3", targets["T3"], _t3_score)

    for person in ranked:
        if person.person_id in assigned:
            continue
        if person.source == "contacts_only":
            person.tier = "T5"
        else:
            person.tier = "T4"

    counts = {f"T{i}": 0 for i in range(1, 6)}
    for person in store.people:
        counts[effective_tier(person)] = counts.get(effective_tier(person), 0) + 1
    return counts


def clearly_waiting_heuristic(person: PersonRecord) -> NaturalEndResult | None:
    if not person.recent_messages:
        return None
    last = person.recent_messages[0]
    text = (last.text or "").strip()
    if last.direction == "outbound" and "?" in text:
        person.scoring.natural_end_score = 0.0
        return NaturalEndResult(score=0.0, reason="outbound question awaiting reply")
    return None


def _thread_excerpt(person: PersonRecord, limit: int = 8) -> list[dict[str, str]]:
    return [
        {
            "direction": message.direction,
            "at": message.at or "",
            "text": (message.text or "").strip(),
        }
        for message in reversed(person.recent_messages[:limit])
    ]


def _thread_hash(person: PersonRecord) -> str:
    payload = {
        "last_message_at": person.last_message_at or "",
        "messages": _thread_excerpt(person),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _cached_classification_is_fresh(person: PersonRecord, now: datetime) -> bool:
    cached = person.natural_end_classification
    if cached is None or cached.hash != _thread_hash(person):
        return False
    classified_at = _parse_datetime(cached.classified_at)
    if classified_at is None:
        return False
    return now - classified_at < _CLASSIFICATION_TTL


def _extract_json_object(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("{") and text.endswith("}"):
        return json.loads(text)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("classifier response did not contain JSON")
    return json.loads(match.group(0))


async def classify_natural_end(person: PersonRecord, llm) -> NaturalEndResult:
    if not person.recent_messages:
        person.scoring.natural_end_score = 0.0
        person.natural_end_classification = NaturalEndClassification(
            hash=_thread_hash(person),
            score=0.0,
            reason="no recent messages; cannot treat as naturally ended",
            classified_at=datetime.now(UTC).isoformat(),
        )
        return NaturalEndResult(
            score=0.0,
            reason="no recent messages; cannot treat as naturally ended",
        )
    heuristic = clearly_waiting_heuristic(person)
    if heuristic is not None:
        person.natural_end_classification = NaturalEndClassification(
            hash=_thread_hash(person),
            score=heuristic.score,
            reason=heuristic.reason,
            classified_at=datetime.now(UTC).isoformat(),
        )
        return heuristic

    now = datetime.now(UTC)
    if _cached_classification_is_fresh(person, now):
        cached = person.natural_end_classification
        person.scoring.natural_end_score = round(cached.score, 2)
        return NaturalEndResult(score=cached.score, reason=cached.reason)

    prompt_lines = [
        "Did this conversation end naturally, or is one party waiting for a response?",
        "Score 0.0 (clearly waiting) to 1.0 (clearly ended). Brief reason.",
        "JSON: {\"score\": float, \"reason\": str}",
        "",
        "Recent messages:",
    ]
    for idx, message in enumerate(_thread_excerpt(person), start=1):
        text = message["text"] or "(empty)"
        prompt_lines.append(f"{idx}. {message['direction']} | {message['at']} | {text}")
    raw = await llm(
        prompt="\n".join(prompt_lines),
        task_type="classify",
    )
    parsed = _extract_json_object(raw)
    score = round(float(parsed.get("score", 0.0)), 2)
    score = _clamp(score)
    reason = str(parsed.get("reason", "")).strip() or "LLM did not provide a reason."
    person.scoring.natural_end_score = score
    person.natural_end_classification = NaturalEndClassification(
        hash=_thread_hash(person),
        score=score,
        reason=reason,
        classified_at=now.isoformat(),
    )
    return NaturalEndResult(score=score, reason=reason)


def natural_end_suppresses(person: PersonRecord) -> bool:
    if not person.recent_messages:
        return False
    last = person.recent_messages[0]
    text = (last.text or "").strip().lower()
    if last.direction == "outbound" and "?" in text:
        return False
    return last.direction == "inbound" and text in _NATURAL_END_TERMINALS
