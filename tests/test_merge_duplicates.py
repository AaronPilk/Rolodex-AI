from __future__ import annotations

from pathlib import Path

from agent.ingest import merge_duplicate_people
from agent.models import Channel, MessageSample, PersonRecord, RolodexStore
from agent.store import load_store, save_store


class _Settings:
    def __init__(self, home: Path) -> None:
        self._home = home

    def resolve_home(self) -> Path:
        return self._home


def test_merge_duplicate_people_collapses_home_and_work_variants(tmp_path: Path) -> None:
    path = tmp_path / "state" / "rolodex" / "rolodex.json"
    save_store(
        path,
        RolodexStore(
            people=[
                PersonRecord(
                    person_id="base",
                    display_name="Jane",
                    first_name="Jane",
                    handles=["+15550001"],
                    channels=[Channel(type="imessage", handle="+15550001", chat_id=1)],
                    recent_messages=[MessageSample(rowid=3, direction="inbound", text="latest", at="2026-05-09T12:00:00+00:00")],
                    inbound_message_count=1,
                ),
                PersonRecord(
                    person_id="home",
                    display_name="Jane Home",
                    handles=["+15550002"],
                    channels=[Channel(type="imessage", handle="+15550002", chat_id=2)],
                    recent_messages=[MessageSample(rowid=2, direction="outbound", text="older", at="2026-05-08T12:00:00+00:00")],
                    outbound_message_count=1,
                ),
                PersonRecord(
                    person_id="work",
                    display_name="Jane Work",
                    handles=["+15550003"],
                    channels=[Channel(type="imessage", handle="+15550003", chat_id=3)],
                    recent_messages=[MessageSample(rowid=1, direction="inbound", text="oldest", at="2026-05-07T12:00:00+00:00")],
                    inbound_message_count=1,
                ),
            ]
        ),
    )

    result = merge_duplicate_people(path, settings=_Settings(tmp_path))
    store = load_store(path)

    assert result == {"before": 3, "after": 1, "merged": 2}
    assert len(store.people) == 1
    person = store.people[0]
    assert person.display_name == "Jane"
    assert set(person.handles) == {"+15550001", "+15550002", "+15550003"}
    assert [message.rowid for message in person.recent_messages] == [3, 2, 1]
