from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from agent.models import Channel, PersonRecord
from agent.store import (
    get_person_by_handle,
    load_store,
    save_store,
    upsert_person,
)


def test_load_missing_store_returns_empty(tmp_path: Path) -> None:
    store = load_store(tmp_path / "rolodex.json")
    assert store.people == []
    assert store.version == 1


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "rolodex.json"
    person = PersonRecord(
        person_id="imessage:14155551234",
        display_name="Jane Doe",
        handles=["+1 (415) 555-1234"],
        channels=[Channel(type="imessage", handle="+14155551234", message_count=3)],
    )
    store = load_store(path)
    upsert_person(store, person)
    save_store(path, store)

    assert not path.exists()
    assert path.with_suffix(".json.enc").exists()
    assert path.with_suffix(".json.salt").exists()
    reloaded = load_store(path)
    assert len(reloaded.people) == 1
    assert reloaded.people[0].display_name == "Jane Doe"
    assert reloaded.updated_at is not None


def test_get_person_by_handle_normalizes_phone_digits() -> None:
    store = load_store(Path("/tmp/does-not-exist.json"))
    upsert_person(
        store,
        PersonRecord(
            person_id="imessage:14155551234",
            handles=["+1 (415) 555-1234"],
            channels=[Channel(type="imessage", handle="+14155551234")],
        ),
    )
    person = get_person_by_handle(store, "14155551234")
    assert person is not None
    assert person.person_id == "imessage:14155551234"


def test_upsert_replaces_existing_person_by_person_id() -> None:
    store = load_store(Path("/tmp/does-not-exist.json"))
    upsert_person(
        store,
        PersonRecord(person_id="imessage:abc", display_name="Old", handles=["a@x.com"]),
    )
    upsert_person(
        store,
        PersonRecord(person_id="imessage:abc", display_name="New", handles=["a@x.com"]),
    )
    assert len(store.people) == 1
    assert store.people[0].display_name == "New"


def test_load_plaintext_store_migrates_to_encrypted_file(tmp_path: Path) -> None:
    path = tmp_path / "rolodex.json"
    path.write_text(
        json.dumps(
            {
                "people": [
                    {
                        "person_id": "p1",
                        "display_name": "Jane",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    store = load_store(path)

    assert store.people[0].display_name == "Jane"
    assert not path.exists()
    assert path.with_suffix(".json.enc").exists()
    assert path.with_suffix(".json.salt").exists()
    assert (tmp_path / ".key").exists()
    assert os.stat(tmp_path / ".key").st_mode & 0o777 == 0o600


def test_load_partial_store_fills_missing_defaults(tmp_path: Path) -> None:
    path = tmp_path / "rolodex.json"
    path.write_text(
        json.dumps(
            {
                "people": [
                    {
                        "person_id": "p1",
                        "display_name": "Jane",
                        "handles": ["+14155551234"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    store = load_store(path)
    person = store.people[0]

    assert person.group_threads == []
    assert person.life_events == []
    assert person.do_not_contact is False
    assert person.notes is None
    assert person.scoring.natural_end_score == 0.0
    assert person.last_message_direction is None
    assert store.daily_sends == {}


def test_concurrent_saves_do_not_corrupt_encrypted_store(tmp_path: Path) -> None:
    path = tmp_path / "rolodex.json"
    first = load_store(path)
    second = load_store(path)
    upsert_person(first, PersonRecord(person_id="p1", display_name="One"))
    upsert_person(second, PersonRecord(person_id="p2", display_name="Two"))

    barrier = threading.Barrier(2)

    def _save(store) -> None:
        barrier.wait()
        save_store(path, store)

    t1 = threading.Thread(target=_save, args=(first,))
    t2 = threading.Thread(target=_save, args=(second,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    reloaded = load_store(path)
    assert len(reloaded.people) == 1
    assert reloaded.people[0].display_name in {"One", "Two"}
