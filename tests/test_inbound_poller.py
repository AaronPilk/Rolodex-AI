from __future__ import annotations

from agent.channels.base import ChannelMessage
from agent.inbound_poller import poll_all_channels
from agent.models import Channel, MessageSample, PersonRecord, RolodexStore


class _FakeChannel:
    def __init__(self, configured: bool = True) -> None:
        self._configured = configured

    def is_configured(self) -> bool:
        return self._configured


async def test_poll_all_channels_routes_messages_to_matching_people(monkeypatch) -> None:
    store = RolodexStore(
        people=[
            PersonRecord(
                person_id="telegram-person",
                display_name="Taylor",
                handles=["12345"],
                channels=[Channel(type="telegram", handle="12345", chat_id=12345)],
            ),
            PersonRecord(
                person_id="whatsapp-person",
                display_name="Chris",
                handles=["whatsapp:+14155550111"],
                channels=[Channel(type="whatsapp", handle="whatsapp:+14155550111")],
            ),
            PersonRecord(
                person_id="instagram-person",
                display_name="Morgan",
                handles=["ig-user-1"],
                channels=[Channel(type="instagram", handle="ig-user-1")],
            ),
            PersonRecord(
                person_id="facebook-person",
                display_name="Riley",
                handles=["fb-user-1"],
                channels=[Channel(type="facebook", handle="fb-user-1")],
            ),
            PersonRecord(
                person_id="x-person",
                display_name="Sky",
                handles=["x-user-1"],
                channels=[Channel(type="x", handle="x-user-1")],
            ),
        ]
    )

    monkeypatch.setattr("agent.inbound_poller.ConnectionStore.apply_to_env", lambda self: None)
    monkeypatch.setattr("agent.inbound_poller.get_channel", lambda _name: _FakeChannel(True))
    async def _telegram(_store):
        return (
            [ChannelMessage(handle="12345", text="telegram hi", direction="inbound", sent_at="2026-05-11T13:00:00+00:00", message_id="tg-1", channel="telegram")],
            "12",
        )

    monkeypatch.setattr("agent.inbound_poller._fetch_telegram_messages", _telegram)
    monkeypatch.setattr(
        "agent.inbound_poller._fetch_whatsapp_messages",
        lambda _store: [ChannelMessage(handle="whatsapp:+14155550111", text="wa hi", direction="inbound", sent_at="2026-05-11T13:01:00+00:00", message_id="wa-1", channel="whatsapp")],
    )
    monkeypatch.setattr(
        "agent.inbound_poller._fetch_meta_messages",
        lambda channel_name, _platform, _account_env: [ChannelMessage(handle="fb-user-1" if channel_name == "facebook" else "ig-user-1", text=f"{channel_name} hi", direction="inbound", sent_at="2026-05-11T13:02:00+00:00", message_id=f"{channel_name}-1", channel=channel_name)],
    )
    monkeypatch.setattr(
        "agent.inbound_poller._fetch_x_messages",
        lambda _store: [ChannelMessage(handle="x-user-1", text="x hi", direction="inbound", sent_at="2026-05-11T13:03:00+00:00", message_id="x-1", channel="x")],
    )

    report = await poll_all_channels(store=store)

    assert report.channel_results["telegram"]["messages_pulled"] == 1
    assert report.channel_results["whatsapp"]["messages_pulled"] == 1
    assert report.channel_results["instagram"]["messages_pulled"] == 1
    assert report.channel_results["facebook"]["messages_pulled"] == 1
    assert report.channel_results["x"]["messages_pulled"] == 1
    assert next(person for person in store.people if person.person_id == "telegram-person").recent_messages[0].text == "telegram hi"
    assert next(person for person in store.people if person.person_id == "whatsapp-person").recent_messages[0].text == "wa hi"
    assert next(person for person in store.people if person.person_id == "instagram-person").recent_messages[0].channel == "instagram"
    assert next(person for person in store.people if person.person_id == "facebook-person").recent_messages[0].channel == "facebook"
    assert next(person for person in store.people if person.person_id == "x-person").recent_messages[0].text == "x hi"


async def test_poll_all_channels_dedupes_repeat_polls(monkeypatch) -> None:
    store = RolodexStore(
        people=[
            PersonRecord(
                person_id="p1",
                display_name="Taylor",
                handles=["12345"],
                recent_messages=[
                    MessageSample(
                        rowid=42,
                        direction="inbound",
                        text="older",
                        at="2026-05-10T12:00:00+00:00",
                        handle="12345",
                        channel="telegram",
                    )
                ],
                inbound_message_count=1,
                channels=[Channel(type="telegram", handle="12345", chat_id=12345, message_count=1)],
            )
        ]
    )
    monkeypatch.setattr("agent.inbound_poller.ConnectionStore.apply_to_env", lambda self: None)
    monkeypatch.setattr("agent.inbound_poller.get_channel", lambda _name: _FakeChannel(True))

    async def _telegram(_store):
        return (
            [ChannelMessage(handle="12345", text="same inbound", direction="inbound", sent_at="2026-05-11T13:00:00+00:00", message_id="tg-duplicate", channel="telegram")],
            "13",
        )

    monkeypatch.setattr("agent.inbound_poller._fetch_telegram_messages", _telegram)
    monkeypatch.setattr("agent.inbound_poller._fetch_whatsapp_messages", lambda _store: [])
    monkeypatch.setattr("agent.inbound_poller._fetch_meta_messages", lambda *_args: [])
    monkeypatch.setattr("agent.inbound_poller._fetch_x_messages", lambda _store: [])

    await poll_all_channels(store=store)
    first_count = len(store.people[0].recent_messages)
    first_inbound = store.people[0].inbound_message_count

    await poll_all_channels(store=store)

    assert len(store.people[0].recent_messages) == first_count
    assert store.people[0].inbound_message_count == first_inbound


async def test_poll_all_channels_creates_unknown_handle_records(monkeypatch) -> None:
    store = RolodexStore()
    monkeypatch.setattr("agent.inbound_poller.ConnectionStore.apply_to_env", lambda self: None)
    monkeypatch.setattr("agent.inbound_poller.get_channel", lambda _name: _FakeChannel(True))
    async def _telegram(_store):
        return (
            [ChannelMessage(handle="987654", text="new inbound", direction="inbound", sent_at="2026-05-11T13:05:00+00:00", message_id="tg-new", channel="telegram")],
            "33",
        )

    monkeypatch.setattr("agent.inbound_poller._fetch_telegram_messages", _telegram)
    monkeypatch.setattr("agent.inbound_poller._fetch_whatsapp_messages", lambda _store: [])
    monkeypatch.setattr("agent.inbound_poller._fetch_meta_messages", lambda *_args: [])
    monkeypatch.setattr("agent.inbound_poller._fetch_x_messages", lambda _store: [])

    await poll_all_channels(store=store)

    assert len(store.people) == 1
    person = store.people[0]
    assert person.person_id == "telegram:987654"
    assert person.source == "telegram_only"
    assert person.recent_messages[0].text == "new inbound"
