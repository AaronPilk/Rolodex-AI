from __future__ import annotations

from datetime import date
from pathlib import Path

from agent.contacts_reader import Contact, _parse_tags, import_contacts, lookup_in_snapshot
from agent.models import Channel, PersonRecord, RolodexStore
from agent.store import load_store, save_store


class _Settings:
    def __init__(self, home: Path) -> None:
        self._home = home

    def resolve_home(self) -> Path:
        return self._home


def test_parse_tags_preserves_dot_tokens() -> None:
    assert _parse_tags("st.pete") == ["st.pete"]
    assert _parse_tags("client, vendor") == ["client", "vendor"]


def test_import_contacts_matches_and_creates_records(tmp_path: Path, monkeypatch) -> None:
    settings = _Settings(tmp_path)
    path = settings.resolve_home() / "state" / "rolodex" / "rolodex.json"
    save_store(
        path,
        RolodexStore(
            people=[
                PersonRecord(
                    person_id="imessage:14155551234",
                    display_name="Jane Doe",
                    inferred_name=None,
                    handles=["+14155551234"],
                    channels=[Channel(type="imessage", handle="+14155551234")],
                    source="imessage",
                )
            ]
        ),
    )
    monkeypatch.setattr(
        "agent.contacts_reader.list_all_contacts",
        lambda: [
            Contact(
                full_name="Jane Doe",
                first_name="Jane",
                last_name="Doe",
                phones=["+14155551234"],
                emails=["jane@example.com"],
                organization="client, st.pete",
                parsed_tags=["client", "st.pete"],
                birthday=date(1990, 1, 2),
                notes="met through work",
            ),
            Contact(
                full_name="Sam Roe",
                first_name="Sam",
                last_name="Roe",
                phones=["+14155550000"],
                emails=["sam@example.com"],
                organization="vendor",
                parsed_tags=["vendor"],
                birthday=None,
                notes=None,
            ),
        ],
    )

    result = import_contacts(settings=settings)
    store = load_store(path)
    jane = next(person for person in store.people if person.person_id == "imessage:14155551234")
    sam = next(person for person in store.people if person.person_id.startswith("contacts:"))

    assert result == {
        "contacts_found": 2,
        "matched_existing": 1,
        "new_records_created": 1,
        "total_people_now": 2,
    }
    assert jane.contact_organization == "client, st.pete"
    assert jane.contact_tags == ["client", "st.pete"]
    assert jane.birthday == date(1990, 1, 2)
    assert jane.inferred_name == "Jane"
    assert jane.source == "imessage+contacts"
    assert sam.source == "contacts_only"
    assert sam.tier == "T5"


def test_lookup_in_snapshot_matches_phone_and_email(tmp_path: Path, monkeypatch) -> None:
    settings = _Settings(tmp_path)
    snapshot_dir = settings.resolve_home() / "state" / "rolodex"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "agent.contacts_reader._snapshot_path",
        lambda: snapshot_dir / "contacts_snapshot.json",
    )
    (snapshot_dir / "contacts_snapshot.json").write_text(
        """
[
  {
    "full_name": "Jane Doe",
    "first_name": "Jane",
    "last_name": "Doe",
    "phones": ["+14155551234"],
    "emails": ["jane@example.com"],
    "organization": null,
    "parsed_tags": [],
    "birthday": null,
    "notes": null
  }
]
        """.strip(),
        encoding="utf-8",
    )

    assert lookup_in_snapshot("+1 (415) 555-1234").full_name == "Jane Doe"
    assert lookup_in_snapshot("jane@example.com").full_name == "Jane Doe"
