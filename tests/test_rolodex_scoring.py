from __future__ import annotations

from datetime import date

import pytest

from agent.models import MessageSample, PersonRecord
from agent.scoring import (
    active_tier,
    auto_assign_tiers,
    classify_natural_end,
    compute_cadence,
    compute_priority,
    natural_end_suppresses,
)


class _Settings:
    rolodex_tier_days = {"T1": 14, "T2": 45, "T3": 90, "T4": 180, "T5": 365}


def test_compute_cadence_marks_overdue() -> None:
    person = PersonRecord(
        person_id="p1",
        tier="T2",
        last_contacted="2026-01-01T00:00:00+00:00",
    )
    cadence = compute_cadence(person, _Settings(), date(2026, 3, 1))
    assert cadence.target_days == 45
    assert cadence.days_since_last == 59
    assert cadence.is_overdue is True
    assert cadence.days_overdue == 14


def test_compute_cadence_respects_snooze() -> None:
    person = PersonRecord(
        person_id="p1",
        tier="T1",
        last_contacted="2026-04-01T00:00:00+00:00",
    )
    person.cadence.snooze_until = "2026-05-15T00:00:00+00:00"
    cadence = compute_cadence(person, _Settings(), date(2026, 5, 8))
    assert cadence.is_overdue is False
    assert cadence.days_overdue == 0


def test_compute_priority_is_deterministic() -> None:
    person = PersonRecord(
        person_id="p1",
        tier="T3",
        user_priority=0.8,
        last_contacted="2026-01-01T00:00:00+00:00",
    )
    person.scoring.warmth = 0.9
    person.scoring.responsiveness = 0.4
    person.scoring.life_event_proximity = 0.3
    score = compute_priority(person, _Settings(), date(2026, 5, 8))
    assert score == compute_priority(person, _Settings(), date(2026, 5, 8))
    assert 0 <= score <= 100


def test_compute_priority_excludes_do_not_contact() -> None:
    person = PersonRecord(
        person_id="p1",
        do_not_contact=True,
        last_contacted="2026-01-01T00:00:00+00:00",
    )
    assert compute_priority(person, _Settings(), date(2026, 5, 8)) is None


def test_compute_priority_excludes_manual_deprioritized() -> None:
    person = PersonRecord(
        person_id="p1",
        user_priority_boost=-100,
        last_contacted="2026-01-01T00:00:00+00:00",
    )
    assert compute_priority(person, _Settings(), date(2026, 5, 8)) is None


def test_compute_priority_applies_manual_boost() -> None:
    person = PersonRecord(
        person_id="p1",
        tier="T3",
        user_priority=0.8,
        user_priority_boost=50,
        last_contacted="2026-01-01T00:00:00+00:00",
    )
    boosted = compute_priority(person, _Settings(), date(2026, 5, 8))
    person.user_priority_boost = None
    baseline = compute_priority(person, _Settings(), date(2026, 5, 8))
    assert boosted == pytest.approx(baseline + 50)


def test_active_tier_prefers_override() -> None:
    person = PersonRecord(person_id="p1", tier="T3", user_override_tier="T1")
    assert active_tier(person) == "T1"


def test_auto_assign_tiers_sends_contacts_only_to_t5() -> None:
    people = [
        PersonRecord(
            person_id=f"p{idx}",
            last_contacted="2026-05-01T00:00:00+00:00",
            outbound_message_count=10 if idx < 20 else 0,
            inbound_message_count=20 - idx if idx < 20 else 1,
            source="imessage" if idx < 20 else "contacts_only",
        )
        for idx in range(40)
    ]
    store = type("_Store", (), {"people": people})()
    counts = auto_assign_tiers(store, today=date(2026, 5, 9))
    assert counts["T5"] >= 20


def test_natural_end_suppresses_inbound_short_terminal() -> None:
    person = PersonRecord(
        person_id="p1",
        recent_messages=[MessageSample(direction="inbound", text="lol")],
    )
    assert natural_end_suppresses(person) is True


def test_natural_end_does_not_suppress_outbound_question() -> None:
    person = PersonRecord(
        person_id="p1",
        recent_messages=[
            MessageSample(
                direction="outbound",
                text="Are you free next week?",
                at="2026-05-01T00:00:00+00:00",
            )
        ],
    )
    assert natural_end_suppresses(person) is False


@pytest.mark.asyncio
async def test_classify_natural_end_clear_ending_uses_llm_cache() -> None:
    person = PersonRecord(
        person_id="p1",
        last_message_at="2026-05-08T10:00:00+00:00",
        recent_messages=[
            MessageSample(direction="outbound", text="great catching up"),
            MessageSample(direction="inbound", text="haha yes talk soon"),
        ],
    )
    calls = 0

    async def _llm(**_kwargs):
        nonlocal calls
        calls += 1
        return '{"score": 0.91, "reason": "Both sides closed the loop."}'

    result = await classify_natural_end(person, _llm)
    cached = await classify_natural_end(person, _llm)
    assert result.score == 0.91
    assert cached.reason == "Both sides closed the loop."
    assert calls == 1


@pytest.mark.asyncio
async def test_classify_natural_end_waiting_short_circuits_llm() -> None:
    person = PersonRecord(
        person_id="p1",
        last_message_at="2026-05-08T10:00:00+00:00",
        recent_messages=[MessageSample(direction="outbound", text="Are you free tomorrow?")],
    )

    async def _llm(**_kwargs):
        raise AssertionError("llm should not be called for clear waiting")

    result = await classify_natural_end(person, _llm)
    assert result.score == 0.0
    assert result.reason == "outbound question awaiting reply"


@pytest.mark.asyncio
async def test_classify_natural_end_ambiguous_parses_reason() -> None:
    person = PersonRecord(
        person_id="p1",
        last_message_at="2026-05-08T10:00:00+00:00",
        recent_messages=[
            MessageSample(direction="outbound", text="nice seeing you"),
            MessageSample(direction="inbound", text="yeah maybe later this week"),
        ],
    )

    async def _llm(**_kwargs):
        return '{"score": 0.55, "reason": "Could be wrapped up, but future follow-up is implied."}'

    result = await classify_natural_end(person, _llm)
    assert result.score == 0.55
    assert "future follow-up" in result.reason
