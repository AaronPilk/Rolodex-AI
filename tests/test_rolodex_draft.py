from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.digest import archive_digest_to_brain, render_brain_note, select_daily_candidates
from agent.draft import build_draft_prompt, generate_draft
from agent.models import MessageSample, PersonRecord, RolodexStore, ToneFeedback
from agent.scoring import compute_cadence


class _Settings:
    rolodex_tier_days = {"T1": 14, "T2": 45, "T3": 90, "T4": 180, "T5": 365}


class _BrainSettings(_Settings):
    def __init__(self, root: Path) -> None:
        self.brain_vault_path = root


@pytest.mark.asyncio
async def test_generate_draft_selects_best_candidate() -> None:
    person = PersonRecord(
        person_id="p1",
        display_name="Jane",
        recent_messages=[
            MessageSample(direction="outbound", text="hey sounds good"),
            MessageSample(direction="outbound", text="i can do tuesday"),
            MessageSample(direction="inbound", text="cool"),
        ],
    )

    async def _llm(**_kwargs):
        return "\n".join(
            [
                "1. Hey Jane, been meaning to check in.",
                "2. hey jane just checking in",
                "3. Hello Jane. I hope you're doing well.",
            ]
        )

    bundle = await generate_draft(person, "cadence-due", _llm)
    assert bundle.top_draft
    assert len(bundle.alternates) == 2
    assert "Reason: cadence-due" in bundle.prompt


@pytest.mark.asyncio
async def test_generate_draft_rejects_do_not_contact() -> None:
    person = PersonRecord(person_id="p1", do_not_contact=True)

    async def _llm(**_kwargs):
        return "1. hi"

    with pytest.raises(ValueError, match="do_not_contact"):
        await generate_draft(person, "cadence-due", _llm)


def test_build_draft_prompt_includes_style_examples() -> None:
    person = PersonRecord(
        person_id="p1",
        display_name="Sam",
        recent_messages=[
            MessageSample(direction="outbound", text="see you soon"),
            MessageSample(direction="outbound", text="sounds good"),
            MessageSample(direction="inbound", text="great"),
        ],
    )
    prompt = build_draft_prompt(person, "cadence-due")
    assert "Verbatim style examples" in prompt
    assert "Most-recent thread snippet" in prompt


@pytest.mark.asyncio
async def test_generate_draft_includes_feedback_anchor_in_system_prompt() -> None:
    person = PersonRecord(
        person_id="p1",
        display_name="Sam",
        recent_messages=[MessageSample(direction="outbound", text="sounds good")],
    )
    person.tone_profile.feedback_log = [
        ToneFeedback(
            timestamp="2026-05-07T10:00:00+00:00",
            draft_sent="yep sounds good",
            rating="off",
        ),
        ToneFeedback(
            timestamp="2026-05-07T11:00:00+00:00",
            draft_sent="yep let's do it",
            rating="edited",
            edit_diff="ORIGINAL: yes\nEDITED: yep let's do it",
        ),
    ]
    captured: dict[str, str] = {}

    async def _llm(**kwargs):
        captured.update(kwargs)
        return "1. sounds good"

    await generate_draft(person, "cadence-due", _llm)
    assert "Calibration anchor from recent operator feedback:" in captured["system"]
    assert "Avoid 'yep sounds good' pattern." in captured["system"]
    assert "they sent 'yep let's do it'" in captured["system"]
    assert "Match the change." in captured["system"]


@pytest.mark.asyncio
async def test_generate_draft_includes_operator_note_in_system_prompt() -> None:
    person = PersonRecord(
        person_id="p1",
        display_name="Sam",
        user_note="This is my cousin, not a client. Keep it family-casual.",
        recent_messages=[MessageSample(direction="outbound", text="sounds good")],
    )
    captured: dict[str, str] = {}

    async def _llm(**kwargs):
        captured.update(kwargs)
        return "1. sounds good"

    await generate_draft(person, "cadence-due", _llm)
    assert "Operator's note about this person: This is my cousin, not a client. Keep it family-casual." in captured["system"]


@pytest.mark.asyncio
async def test_digest_selection_and_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    person = PersonRecord(
        person_id="p1",
        display_name="Jane",
        inferred_name="Jane",
        relationship_class="close_friend",
        tier="T1",
        user_priority=0.8,
        last_contacted="2026-04-01T00:00:00+00:00",
        inbound_message_count=2,
        outbound_message_count=2,
        recent_messages=[
            MessageSample(direction="outbound", text="hey jane how was the trip?"),
            MessageSample(direction="inbound", text="it was great"),
            MessageSample(direction="inbound", text="Talk soon"),
            MessageSample(direction="outbound", text="Want to grab coffee next week?"),
        ],
    )
    compute_cadence(person, _Settings(), datetime(2026, 5, 8, tzinfo=UTC).date())
    store = RolodexStore(people=[person])
    async def _llm(**_kwargs):
        return '{"score": 0.2, "reason": "Reply still owed."}'

    candidates = await select_daily_candidates(store, _Settings(), llm=_llm, limit=5)
    assert len(candidates) == 1
    note = render_brain_note(candidates, datetime(2026, 5, 8, 9, 0, tzinfo=UTC))
    assert "# Rolodex Digest - 2026-05-08" in note

    monkeypatch.setattr("agent.digest.get_settings", lambda: _BrainSettings(tmp_path))
    path = archive_digest_to_brain(candidates, datetime(2026, 5, 8, 9, 0, tzinfo=UTC))
    assert path.read_text(encoding="utf-8") == note


@pytest.mark.asyncio
async def test_digest_can_surface_priority_top_even_when_not_overdue() -> None:
    person = PersonRecord(
        person_id="p1",
        display_name="Jane",
        inferred_name="Jane",
        relationship_class="close_friend",
        tier="T1",
        user_priority=0.8,
        last_contacted="2026-05-01T00:00:00+00:00",
        inbound_message_count=2,
        outbound_message_count=2,
        recent_messages=[
            MessageSample(direction="outbound", text="hey jane"),
            MessageSample(direction="inbound", text="hi"),
            MessageSample(direction="outbound", text="coffee soon?"),
            MessageSample(direction="inbound", text="yes"),
        ],
    )
    compute_cadence(person, _Settings(), datetime(2026, 5, 8, tzinfo=UTC).date())

    async def _llm(**_kwargs):
        return '{"score": 0.2, "reason": "Reply still owed."}'

    candidates = await select_daily_candidates(RolodexStore(people=[person]), _Settings(), llm=_llm, limit=5)
    assert len(candidates) == 1
    assert candidates[0].reason == "priority-top"


@pytest.mark.asyncio
async def test_digest_excludes_sensitive_threads_from_autodraft() -> None:
    person = PersonRecord(
        person_id="p1",
        display_name="Jane",
        inferred_name="Jane",
        relationship_class="close_friend",
        tier="T1",
        user_priority=0.8,
        last_contacted="2026-04-01T00:00:00+00:00",
        inbound_message_count=2,
        outbound_message_count=2,
        sensitivity_flags=["LEGAL"],
    )
    compute_cadence(person, _Settings(), datetime(2026, 5, 8, tzinfo=UTC).date())
    async def _llm(**_kwargs):
        return '{"score": 0.2, "reason": "Reply still owed."}'

    candidates = await select_daily_candidates(
        RolodexStore(people=[person]),
        _Settings(),
        llm=_llm,
        limit=5,
    )
    assert candidates == []


@pytest.mark.asyncio
async def test_digest_excludes_do_not_contact() -> None:
    person = PersonRecord(
        person_id="p1",
        display_name="Jane",
        inferred_name="Jane",
        relationship_class="close_friend",
        tier="T1",
        user_priority=0.8,
        last_contacted="2026-04-01T00:00:00+00:00",
        inbound_message_count=2,
        outbound_message_count=2,
        do_not_contact=True,
    )
    compute_cadence(person, _Settings(), datetime(2026, 5, 8, tzinfo=UTC).date())
    async def _llm(**_kwargs):
        return '{"score": 0.2, "reason": "Reply still owed."}'

    candidates = await select_daily_candidates(
        RolodexStore(people=[person]),
        _Settings(),
        llm=_llm,
        limit=5,
    )
    assert candidates == []


@pytest.mark.asyncio
async def test_digest_excludes_non_relationship_threads() -> None:
    people = [
        PersonRecord(
            person_id="spam",
            display_name="Spam",
            relationship_class="spam_or_verification",
            tier="T1",
            user_priority=0.8,
            last_contacted="2026-04-01T00:00:00+00:00",
            inbound_message_count=3,
            outbound_message_count=1,
        ),
        PersonRecord(
            person_id="unknown",
            display_name="Unknown",
            relationship_class="unknown",
            tier="T1",
            user_priority=0.8,
            last_contacted="2026-04-01T00:00:00+00:00",
            inbound_message_count=3,
            outbound_message_count=1,
        ),
        PersonRecord(
            person_id="cold",
            display_name="Cold",
            relationship_class="business",
            tier="T1",
            user_priority=0.8,
            last_contacted="2026-04-01T00:00:00+00:00",
            inbound_message_count=4,
            outbound_message_count=0,
        ),
        PersonRecord(
            person_id="thin",
            display_name="Thin",
            relationship_class="close_friend",
            tier="T1",
            user_priority=0.8,
            last_contacted="2026-04-01T00:00:00+00:00",
            inbound_message_count=2,
            outbound_message_count=1,
        ),
        PersonRecord(
            person_id="good",
            display_name="Raw Name",
            inferred_name="Sam",
            relationship_class="close_friend",
            tier="T1",
            user_priority=0.8,
            last_contacted="2026-04-01T00:00:00+00:00",
            inbound_message_count=3,
            outbound_message_count=2,
            recent_messages=[
                MessageSample(direction="outbound", text="hey sam"),
                MessageSample(direction="inbound", text="hey"),
                MessageSample(direction="outbound", text="coffee soon?"),
                MessageSample(direction="inbound", text="yes"),
                MessageSample(direction="outbound", text="great"),
            ],
        ),
    ]
    for person in people:
        compute_cadence(person, _Settings(), datetime(2026, 5, 8, tzinfo=UTC).date())

    async def _llm(**_kwargs):
        return '{"score": 0.2, "reason": "Reply still owed."}'

    candidates = await select_daily_candidates(RolodexStore(people=people), _Settings(), llm=_llm, limit=5)
    assert [candidate.person_id for candidate in candidates] == ["good"]
    assert candidates[0].inferred_name == "Sam"
    assert candidates[0].relationship_class == "close_friend"


@pytest.mark.asyncio
async def test_digest_uses_override_class_for_filtering() -> None:
    person = PersonRecord(
        person_id="p1",
        display_name="Unknown",
        relationship_class="unknown",
        user_override_class="casual_friend",
        tier="T1",
        user_priority=0.8,
        last_contacted="2026-04-01T00:00:00+00:00",
        inbound_message_count=3,
        outbound_message_count=2,
        recent_messages=[
            MessageSample(direction="outbound", text="hey"),
            MessageSample(direction="inbound", text="hi"),
            MessageSample(direction="outbound", text="coffee?"),
            MessageSample(direction="inbound", text="sure"),
            MessageSample(direction="outbound", text="great"),
        ],
    )
    compute_cadence(person, _Settings(), datetime(2026, 5, 8, tzinfo=UTC).date())

    async def _llm(**_kwargs):
        return '{"score": 0.2, "reason": "Reply still owed."}'

    candidates = await select_daily_candidates(RolodexStore(people=[person]), _Settings(), llm=_llm, limit=5)
    assert len(candidates) == 1
    assert candidates[0].relationship_class == "casual_friend"
