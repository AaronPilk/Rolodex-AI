from __future__ import annotations

from agent.models import MessageSample, PersonRecord
from agent.web import _person_matches_filters


def _person(**overrides) -> PersonRecord:
    base = PersonRecord(
        person_id="p1",
        display_name="Taylor",
        relationship_class="met_briefly",
        tier="T3",
        last_contacted="2026-01-01T00:00:00+00:00",
        inbound_message_count=2,
        outbound_message_count=2,
        recent_messages=[
            MessageSample(direction="inbound", text="this is Taylor", at="2026-01-01T00:00:00+00:00"),
            MessageSample(direction="outbound", text="i'm Aaron", at="2026-01-01T00:01:00+00:00"),
        ],
    )
    return base.model_copy(update=overrides)


def test_person_matches_max_total_messages_alias() -> None:
    person = _person(inbound_message_count=3, outbound_message_count=3)
    assert _person_matches_filters(person, {"max_total_messages": 6}) is True
    assert _person_matches_filters(person, {"max_total_messages": 5}) is False


def test_person_matches_last_contacted_before_days() -> None:
    person = _person(last_contacted="2025-10-01T00:00:00+00:00")
    assert _person_matches_filters(person, {"last_contacted_before_days": 90}) is True
    assert _person_matches_filters(person, {"last_contacted_before_days": 300}) is False


def test_person_matches_no_followup_after_intro() -> None:
    person = _person(
        inbound_message_count=2,
        outbound_message_count=2,
        recent_messages=[
            MessageSample(direction="inbound", text="this is Taylor", at="2026-05-01T00:00:00+00:00"),
            MessageSample(direction="outbound", text="i'm Aaron", at="2026-05-01T00:01:00+00:00"),
            MessageSample(direction="inbound", text="nice to meet you", at="2026-05-01T00:02:00+00:00"),
            MessageSample(direction="outbound", text="same here", at="2026-05-01T00:03:00+00:00"),
        ],
    )
    assert _person_matches_filters(person, {"no_followup_after_intro": True}) is True
    assert _person_matches_filters(person.model_copy(update={"outbound_message_count": 5}), {"no_followup_after_intro": True}) is False


def test_person_matches_relationship_class_inclusion_and_exclusion() -> None:
    person = _person(relationship_class="business")
    assert _person_matches_filters(person, {"relationship_classes": ["business", "professional"]}) is True
    assert _person_matches_filters(person, {"relationship_classes": ["family"]}) is False
    assert _person_matches_filters(person, {"excluded_classes": ["business"]}) is False
