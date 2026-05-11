from __future__ import annotations

from agent.ingest import _handle_key, _relink_duplicate_handles, upsert_person_from_thread
from agent.models import Channel, MessageSample, PersonRecord, RolodexStore, ThreadSnapshot


def _thread(chat_id: int, handle: str, when: str, texts: list[tuple[int, str, str]]) -> ThreadSnapshot:
    return ThreadSnapshot(
        chat_id=chat_id,
        title=handle,
        handle=handle,
        handles=[handle],
        last_at=when,
        message_count=len(texts),
        last_message_direction=texts[0][1],  # newest first
        messages=[
            MessageSample(rowid=rowid, direction=direction, text=text, at=at, handle=handle, channel="imessage")
            for rowid, direction, text, at in texts
        ],
    )


def test_upsert_person_from_thread_merges_threads_newest_first(monkeypatch) -> None:
    monkeypatch.setattr("agent.ingest.resolve_contact_metadata", lambda _handle: None)
    store = RolodexStore()

    newer = ThreadSnapshot(
        chat_id=100,
        title="Jane",
        handle="+1 (727) 555-1234",
        handles=["+1 (727) 555-1234"],
        last_at="2026-05-09T12:00:00+00:00",
        message_count=2,
        last_message_direction="inbound",
        messages=[
            MessageSample(rowid=9002, direction="inbound", text="see you soon", at="2026-05-09T12:00:00+00:00", handle="+17275551234", channel="imessage"),
            MessageSample(rowid=9001, direction="outbound", text="lunch this week?", at="2026-05-08T12:00:00+00:00", handle="+17275551234", channel="imessage"),
        ],
    )
    older = ThreadSnapshot(
        chat_id=101,
        title="Jane",
        handle="7275551234",
        handles=["7275551234"],
        last_at="2024-02-03T10:00:00+00:00",
        message_count=2,
        last_message_direction="outbound",
        messages=[
            MessageSample(rowid=8002, direction="outbound", text="old follow-up", at="2024-02-03T10:00:00+00:00", handle="7275551234", channel="imessage"),
            MessageSample(rowid=8001, direction="inbound", text="old ping", at="2024-02-01T10:00:00+00:00", handle="7275551234", channel="imessage"),
        ],
    )

    upsert_person_from_thread(store, newer)
    person = upsert_person_from_thread(store, older)

    assert [message.rowid for message in person.recent_messages] == [9002, 9001, 8002, 8001]
    assert person.last_message_at == "2026-05-09T12:00:00+00:00"
    assert person.last_contacted == "2026-05-08T12:00:00+00:00"
    assert person.inbound_message_count == 2
    assert person.outbound_message_count == 2
    assert {channel.chat_id for channel in person.channels} == {100, 101}


def test_handle_key_collapses_us_phone_variants() -> None:
    variants = [
        "+1 727 555 1234",
        "1 727 555 1234",
        "7275551234",
        "(727) 555-1234",
        "727-555-1234",
        "727 555 1234",
    ]
    assert {_handle_key(value) for value in variants} == {"7275551234"}


def test_relink_duplicate_handles_merges_records_preserving_overrides() -> None:
    store = RolodexStore(
        people=[
            PersonRecord(
                person_id="p1",
                display_name="Jane Contact",
                handles=["+1 727 555 1234"],
                channels=[Channel(type="imessage", handle="+17275551234", chat_id=1, message_count=2)],
                recent_messages=[
                    MessageSample(rowid=10, direction="inbound", text="new message", at="2026-05-09T10:00:00+00:00"),
                ],
                inbound_message_count=1,
                user_note="keep in touch",
                do_not_contact=True,
                source="imessage+contacts",
            ),
            PersonRecord(
                person_id="p2",
                display_name="Jane Phone",
                handles=["(727) 555-1234"],
                channels=[Channel(type="imessage", handle="7275551234", chat_id=2, message_count=3)],
                recent_messages=[
                    MessageSample(rowid=11, direction="outbound", text="older message", at="2024-04-01T10:00:00+00:00"),
                ],
                outbound_message_count=1,
                user_override_class="close_friend",
                user_priority_boost=9,
                source="imessage",
            ),
        ]
    )

    merged = _relink_duplicate_handles(store)

    assert merged == 1
    assert len(store.people) == 1
    person = store.people[0]
    assert person.handles == ["+1 727 555 1234", "(727) 555-1234"]
    assert {channel.chat_id for channel in person.channels} == {1, 2}
    assert [message.rowid for message in person.recent_messages] == [10, 11]
    assert person.inbound_message_count == 1
    assert person.outbound_message_count == 1
    assert person.user_note == "keep in touch"
    assert person.user_override_class == "close_friend"
    assert person.user_priority_boost == 9
    assert person.do_not_contact is True
