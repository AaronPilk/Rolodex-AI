from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.imessage_reader import COCOA_EPOCH as MAC_EPOCH
from agent.ingest import (
    ContactMatch,
    _thread_classification_hash,
    sync_imessage_threads,
    upsert_person_from_thread,
    verify_contact_names,
)
from agent.models import Channel, MessageSample, PersonRecord, RolodexStore, ThreadSnapshot
from agent.store import get_person_by_handle, load_store, save_store


def _build_fake_chat_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE handle (
              ROWID INTEGER PRIMARY KEY,
              id TEXT
            );
            CREATE TABLE chat (
              ROWID INTEGER PRIMARY KEY,
              display_name TEXT,
              chat_identifier TEXT,
              style INTEGER
            );
            CREATE TABLE chat_handle_join (
              chat_id INTEGER,
              handle_id INTEGER
            );
            CREATE TABLE chat_message_join (
              chat_id INTEGER,
              message_id INTEGER
            );
            CREATE TABLE message (
              ROWID INTEGER PRIMARY KEY,
              text TEXT,
              attributedBody BLOB,
              date INTEGER,
              is_from_me INTEGER,
              handle_id INTEGER,
              cache_has_attachments INTEGER DEFAULT 0,
              is_audio_message INTEGER DEFAULT 0,
              item_type INTEGER DEFAULT 0
            );
            """
        )
        handles = [
            (1, "+14155551234"),
            (2, "sam@example.com"),
            (3, "+14155550000"),
            (4, "+14155559999"),
        ]
        chats = [
            (10, "Jane", "+14155551234", 45),
            (11, "Sam", "sam@example.com", 45),
            (12, "Group Chat", "chat0001", 43),
        ]
        conn.executemany("INSERT INTO handle(ROWID, id) VALUES (?, ?)", handles)
        conn.executemany(
            "INSERT INTO chat(ROWID, display_name, chat_identifier, style) VALUES (?, ?, ?, ?)",
            chats,
        )
        conn.executemany(
            "INSERT INTO chat_handle_join(chat_id, handle_id) VALUES (?, ?)",
            [
                (10, 1),
                (11, 2),
                (12, 1),
                (12, 3),
                (12, 4),
            ],
        )
        now = datetime.now(UTC)
        mac_now = int((now - MAC_EPOCH).total_seconds() * 1e9)

        def _insert(message_id: int, chat_id: int, text: str, is_from_me: int, handle_id: int | None, ago_s: int) -> None:
            ts = mac_now - int(ago_s * 1e9)
            conn.execute(
                "INSERT INTO message(ROWID, text, date, is_from_me, handle_id) VALUES (?, ?, ?, ?, ?)",
                (message_id, text, ts, is_from_me, handle_id),
            )
            conn.execute(
                "INSERT INTO chat_message_join(chat_id, message_id) VALUES (?, ?)",
                (chat_id, message_id),
            )

        _insert(100, 10, "want to grab coffee?", 0, 1, 90)
        _insert(101, 10, "yeah next week works", 1, None, 30)
        _insert(200, 11, "send me the deck", 0, 2, 300)
        _insert(201, 11, "will do", 1, None, 120)
        _insert(300, 12, "family dinner sunday", 0, 3, 600)
        _insert(301, 12, "sounds good", 1, None, 480)
        conn.commit()
    finally:
        conn.close()


class _FakeSettings:
    def __init__(self, home: Path) -> None:
        self._home = home

    def resolve_home(self) -> Path:
        return self._home


@pytest.fixture
def fake_messages_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "chat.db"
    _build_fake_chat_db(db)
    monkeypatch.setenv("PILK_APPLE_MESSAGES_DB", str(db))
    return db


def test_upsert_person_from_thread_enriches_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    store = load_store(Path("/tmp/does-not-exist.json"))
    monkeypatch.setattr(
        "agent.ingest.resolve_contact_metadata",
        lambda handle: ContactMatch(
            query=handle,
            matched_name="Jane Doe",
            first_name="Jane",
            last_name="Doe",
            phones=[handle],
        ),
    )
    thread = ThreadSnapshot(
        chat_id=10,
        title="Jane",
        handle="+14155551234",
        handles=["+14155551234"],
        last_at="2026-05-08T09:00:00+00:00",
        message_count=2,
        last_message_direction="outbound",
        messages=[
            MessageSample(direction="inbound", text="want to grab coffee?"),
            MessageSample(direction="outbound", text="yeah next week works"),
        ],
    )
    person = upsert_person_from_thread(store, thread)
    assert person.display_name == "Jane Doe"
    assert person.first_name == "Jane"
    assert person.channels[0].message_count == 2


def test_sync_imessage_threads_writes_store_and_tags_groups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_messages_db: Path,
) -> None:
    monkeypatch.setattr("agent.ingest.get_settings", lambda: _FakeSettings(tmp_path))
    monkeypatch.setattr(
        "agent.ingest.resolve_contact_metadata",
        lambda handle: ContactMatch(
            query=handle,
            matched_name="Jane Doe" if handle == "+14155551234" else "Sam Roe",
            first_name="Jane" if handle == "+14155551234" else "Sam",
            last_name="Doe" if handle == "+14155551234" else "Roe",
            phones=[handle] if handle.startswith("+") else [],
            emails=[handle] if "@" in handle else [],
        ) if handle in {"+14155551234", "sam@example.com"} else None,
    )

    report = sync_imessage_threads(max_threads=10, max_messages_per_thread=10)
    assert report.scanned_threads == 3
    assert report.created_people == 2
    assert report.skipped_group_threads == 1
    assert report.tagged_group_threads == 3

    path = tmp_path / "state" / "rolodex" / "rolodex.json"
    store = load_store(path)
    assert len(store.people) == 4
    jane = get_person_by_handle(store, "+14155551234")
    assert jane is not None
    assert jane.display_name == "Jane Doe"
    assert jane.last_message_direction == "outbound"
    assert jane.group_threads[0].title == "Group Chat"

    sam = get_person_by_handle(store, "sam@example.com")
    assert sam is not None
    assert sam.display_name == "Sam Roe"
    assert sam.channels[0].type == "imessage"


def test_sync_imessage_threads_sets_sensitivity_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_messages_db: Path,
) -> None:
    monkeypatch.setattr("agent.ingest.get_settings", lambda: _FakeSettings(tmp_path))
    monkeypatch.setattr("agent.ingest.resolve_contact_metadata", lambda _handle: None)

    report = sync_imessage_threads(
        max_threads=10,
        max_messages_per_thread=10,
        enrich=True,
        sensitive_classifier=lambda prompt: "LEGAL" if "send me the deck" in prompt else "NONE",
    )

    assert report.created_people == 2
    store = load_store(tmp_path / "state" / "rolodex" / "rolodex.json")
    sam = get_person_by_handle(store, "sam@example.com")
    jane = get_person_by_handle(store, "+14155551234")
    assert sam is not None
    assert jane is not None
    assert sam.sensitivity_flags == ["LEGAL"]
    assert jane.sensitivity_flags == []


def test_sync_imessage_threads_skips_reclassification_when_hash_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_messages_db: Path,
) -> None:
    monkeypatch.setattr("agent.ingest.get_settings", lambda: _FakeSettings(tmp_path))
    monkeypatch.setattr("agent.ingest.resolve_contact_metadata", lambda _handle: None)
    calls: list[str] = []

    def _classifier(prompt: str) -> str:
        calls.append(prompt)
        return "NONE"

    sync_imessage_threads(
        max_threads=10,
        max_messages_per_thread=10,
        enrich=True,
        sensitive_classifier=_classifier,
    )
    assert len(calls) == 2

    calls.clear()
    sync_imessage_threads(
        max_threads=10,
        max_messages_per_thread=10,
        enrich=True,
        sensitive_classifier=_classifier,
    )
    assert calls == []


def test_sync_imessage_threads_enriches_relationship_profile_and_caches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_messages_db: Path,
) -> None:
    monkeypatch.setattr("agent.ingest.get_settings", lambda: _FakeSettings(tmp_path))
    monkeypatch.setattr("agent.ingest.resolve_contact_metadata", lambda _handle: None)
    relationship_calls: list[str] = []
    profile_calls: list[str] = []

    def _relationship(prompt: str) -> str:
        relationship_calls.append(prompt)
        return "close_friend" if "coffee" in prompt else "business"

    def _profile(prompt: str) -> str:
        profile_calls.append(prompt)
        if "coffee" in prompt:
            return (
                '{"inferred_name": "Jane", '
                '"context_summary": "They are friends who check in casually and sometimes make plans.", '
                '"topics": ["coffee", "plans"]}'
            )
        return (
            '{"inferred_name": "Sam", '
            '"context_summary": "They have a professional relationship with occasional document requests.", '
            '"topics": ["work", "deck"]}'
        )

    sync_imessage_threads(
        max_threads=10,
        max_messages_per_thread=10,
        enrich=True,
        relationship_classifier=_relationship,
        profile_enricher=_profile,
    )

    path = tmp_path / "state" / "rolodex" / "rolodex.json"
    store = load_store(path)
    jane = get_person_by_handle(store, "+14155551234")
    sam = get_person_by_handle(store, "sam@example.com")
    assert jane is not None
    assert sam is not None
    assert jane.relationship_class == "close_friend"
    assert jane.inferred_name == "Jane"
    assert jane.context_summary is not None
    assert jane.topics == ["coffee", "plans"]
    assert sam.relationship_class == "business"
    assert sam.inferred_name == "Sam"
    assert len(relationship_calls) == 2
    assert len(profile_calls) == 2

    relationship_calls.clear()
    profile_calls.clear()
    sync_imessage_threads(
        max_threads=10,
        max_messages_per_thread=10,
        enrich=True,
        relationship_classifier=_relationship,
        profile_enricher=_profile,
    )
    assert relationship_calls == []
    assert profile_calls == []


def test_sync_imessage_threads_falls_back_to_contacts_for_inferred_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_messages_db: Path,
) -> None:
    monkeypatch.setattr("agent.ingest.get_settings", lambda: _FakeSettings(tmp_path))
    monkeypatch.setattr("agent.ingest.resolve_contact_metadata", lambda _handle: None)
    monkeypatch.setattr("agent.ingest.lookup_by_phone", lambda handle: type("Contact", (), {"first_name": "Jane", "name": "Jane Doe"})() if handle == "+14155551234" else None)

    sync_imessage_threads(
        max_threads=10,
        max_messages_per_thread=10,
        enrich=True,
        relationship_classifier=lambda _prompt: "close_friend",
        profile_enricher=lambda _prompt: '{"inferred_name": null, "context_summary": "Friends who keep up casually.", "topics": ["coffee"]}',
    )

    store = load_store(tmp_path / "state" / "rolodex" / "rolodex.json")
    jane = get_person_by_handle(store, "+14155551234")
    assert jane is not None
    assert jane.inferred_name == "Jane"


def test_sync_imessage_threads_reclassifies_when_hash_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_messages_db: Path,
) -> None:
    monkeypatch.setattr("agent.ingest.get_settings", lambda: _FakeSettings(tmp_path))
    monkeypatch.setattr("agent.ingest.resolve_contact_metadata", lambda _handle: None)
    sync_imessage_threads(
        max_threads=10,
        max_messages_per_thread=10,
        enrich=True,
        sensitive_classifier=lambda _prompt: "NONE",
    )

    path = tmp_path / "state" / "rolodex" / "rolodex.json"
    store = load_store(path)
    sam = get_person_by_handle(store, "sam@example.com")
    assert sam is not None
    sam.recent_messages.append(MessageSample(direction="inbound", text="one more update"))
    sam.sensitivity_classification_hash = _thread_classification_hash(sam.recent_messages)
    save_store(path, store)

    calls: list[str] = []
    sync_imessage_threads(
        max_threads=10,
        max_messages_per_thread=10,
        enrich=True,
        sensitive_classifier=lambda prompt: calls.append(prompt) or "NONE",
    )
    assert len(calls) == 1
    assert "send me the deck" in calls[0]


def test_sync_imessage_threads_reclassifies_when_cache_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_messages_db: Path,
) -> None:
    monkeypatch.setattr("agent.ingest.get_settings", lambda: _FakeSettings(tmp_path))
    monkeypatch.setattr("agent.ingest.resolve_contact_metadata", lambda _handle: None)
    sync_imessage_threads(
        max_threads=10,
        max_messages_per_thread=10,
        enrich=True,
        sensitive_classifier=lambda _prompt: "NONE",
    )

    path = tmp_path / "state" / "rolodex" / "rolodex.json"
    store = load_store(path)
    stale_at = datetime(2026, 4, 1, tzinfo=UTC)
    for person in store.people:
        if person.channels:
            person.sensitivity_classified_at = stale_at
    save_store(path, store)

    calls: list[str] = []
    sync_imessage_threads(
        max_threads=10,
        max_messages_per_thread=10,
        enrich=True,
        sensitive_classifier=lambda prompt: calls.append(prompt) or "NONE",
    )
    assert len(calls) == 2


def test_verify_contact_names_fills_missing_display_name_from_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agent.ingest.get_settings", lambda: _FakeSettings(tmp_path))
    save_store(
        tmp_path / "state" / "rolodex" / "rolodex.json",
        RolodexStore(
            people=[
                PersonRecord(
                    person_id="imessage:14155551234",
                    display_name="+14155551234",
                    handles=["+14155551234"],
                    channels=[Channel(type="imessage", handle="+14155551234")],
                )
            ]
        ),
    )
    monkeypatch.setattr("agent.ingest.lookup_by_phone", lambda _handle: None)
    monkeypatch.setattr("agent.ingest.load_contacts_snapshot", lambda: ["snapshot"])
    monkeypatch.setattr(
        "agent.ingest.lookup_in_snapshot",
        lambda _handle, _snapshot=None: type(
            "Contact",
            (),
            {"full_name": "Jane Doe", "first_name": "Jane", "last_name": "Doe", "name": "Jane Doe"},
        )(),
    )

    result = verify_contact_names(settings=_FakeSettings(tmp_path))
    reloaded = load_store(tmp_path / "state" / "rolodex" / "rolodex.json").people[0]

    assert result["updated"] == 1
    assert reloaded.display_name == "Jane Doe"
    assert reloaded.first_name == "Jane"


def test_sync_imessage_threads_repairs_stale_known_channel_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_messages_db: Path,
) -> None:
    monkeypatch.setattr("agent.ingest.get_settings", lambda: _FakeSettings(tmp_path))
    monkeypatch.setattr("agent.ingest.resolve_contact_metadata", lambda _handle: None)
    monkeypatch.setattr("agent.ingest.list_threads", lambda **_kwargs: [])

    path = tmp_path / "state" / "rolodex" / "rolodex.json"
    save_store(
        path,
        RolodexStore(
            people=[
                PersonRecord(
                    person_id="imessage:14155551234",
                    display_name="+14155551234",
                    handles=["+14155551234"],
                    channels=[
                        Channel(
                            type="imessage",
                            handle="+14155551234",
                            chat_id=10,
                            message_count=2,
                            last_message_at="2026-05-08T09:00:00+00:00",
                            last_message_direction="outbound",
                            active=True,
                        )
                    ],
                    recent_messages=[
                        MessageSample(
                            rowid=1,
                            direction="inbound",
                            text="stale message",
                            at="2024-01-01T00:00:00+00:00",
                            handle="+14155551234",
                            channel="imessage",
                        )
                    ],
                    last_message_at="2024-01-01T00:00:00+00:00",
                    last_contacted="2024-01-01T00:00:00+00:00",
                    inbound_message_count=1,
                )
            ]
        ),
    )

    report = sync_imessage_threads(max_threads=0, max_messages_per_thread=0)

    assert "refreshed 1 stale iMessage channel records" in report.warnings
    reloaded = load_store(path)
    person = get_person_by_handle(reloaded, "+14155551234")
    assert person is not None
    assert person.last_message_at != "2024-01-01T00:00:00+00:00"
    assert person.last_contacted != "2024-01-01T00:00:00+00:00"
    assert person.recent_messages[0].text == "yeah next week works"


def test_sync_imessage_threads_applies_group_self_introduction_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agent.ingest.get_settings", lambda: _FakeSettings(tmp_path))
    monkeypatch.setattr("agent.ingest._messages_db_path", lambda: tmp_path / "chat.db")
    monkeypatch.setattr(
        "agent.ingest._fetch_thread_snapshots",
        lambda **_kwargs: [
            ThreadSnapshot(
                chat_id=77,
                title="Family Group",
                is_group=True,
                handles=["+14155550001", "+14155550002", "+14155550003"],
                last_at="2026-05-10T09:00:00+00:00",
                message_count=2,
                last_message_direction="inbound",
                messages=[
                    MessageSample(
                        rowid=1,
                        at="2026-05-10T09:00:00+00:00",
                        direction="inbound",
                        text="Hi this is Mike",
                        handle="+14155550003",
                        channel="imessage",
                    ),
                    MessageSample(
                        rowid=2,
                        at="2026-05-10T08:00:00+00:00",
                        direction="inbound",
                        text="Dinner at 7",
                        handle="+14155550001",
                        channel="imessage",
                    ),
                ],
            )
        ],
    )
    monkeypatch.setattr("agent.ingest.resolve_contact_metadata", lambda _handle: None)

    report = sync_imessage_threads(max_threads=10, max_messages_per_thread=10)
    assert report.skipped_group_threads == 1

    store = load_store(tmp_path / "state" / "rolodex" / "rolodex.json")
    mike = get_person_by_handle(store, "+14155550003")
    assert mike is not None
    assert mike.display_name == "Mike"
    assert mike.first_name == "Mike"
    assert mike.recent_messages == []
    assert mike.group_threads[0].title == "Family Group"
