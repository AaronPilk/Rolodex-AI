from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agent.models import MessageSample, PersonRecord, RolodexStore
from agent.store import load_store, save_store
from agent.web import create_app


class _Settings:
    def __init__(self, home: Path) -> None:
        self._home = home
        self.rolodex_tier_days = {"T1": 14, "T2": 45, "T3": 90, "T4": 180, "T5": 365}
        self.rolodex_daily_send_cap = 5

    def resolve_home(self) -> Path:
        return self._home


def test_onboarding_queue_prioritizes_family_first(tmp_path: Path, monkeypatch) -> None:
    settings = _Settings(tmp_path)
    path = settings.resolve_home() / "state" / "rolodex" / "rolodex.json"
    save_store(
        path,
        RolodexStore(
            people=[
                PersonRecord(
                    person_id="tier3",
                    display_name="Loose Tie",
                    relationship_class="casual_friend",
                    tier="T3",
                    user_priority_boost=10,
                ),
                PersonRecord(
                    person_id="family",
                    display_name="Dad",
                    relationship_class="family",
                    tier="T1",
                    recent_messages=[
                        MessageSample(direction="inbound", text="Dinner Sunday?", at="2026-05-09T17:00:00+00:00"),
                    ],
                ),
            ]
        ),
    )
    monkeypatch.setattr("agent.web.get_settings", lambda: settings)

    client = TestClient(create_app())
    payload = client.get("/api/onboarding/queue?limit=20").json()

    assert [item["person_id"] for item in payload["items"]][:2] == ["family", "tier3"]


def test_onboarding_annotation_round_trips_social_handles(tmp_path: Path, monkeypatch) -> None:
    settings = _Settings(tmp_path)
    path = settings.resolve_home() / "state" / "rolodex" / "rolodex.json"
    save_store(path, RolodexStore(people=[PersonRecord(person_id="p1", display_name="Jane Doe")]))
    monkeypatch.setattr("agent.web.get_settings", lambda: settings)

    client = TestClient(create_app())
    response = client.post(
        "/api/people/p1/annotate",
        json={
            "user_note": "Met through a mutual friend",
            "how_we_met": "Art Basel 2024",
            "instagram_username": "janegram",
            "facebook_handle": "jane.doe",
            "twitter_handle": "janedoe",
            "linkedin_url": "https://linkedin.com/in/janedoe",
            "snapchat_username": "snapjane",
            "tiktok_handle": "@janedoe",
            "onboarding_reviewed": True,
        },
    )

    assert response.status_code == 200
    person = load_store(path).people[0]
    assert person.instagram_username == "janegram"
    assert person.facebook_handle == "jane.doe"
    assert person.twitter_handle == "janedoe"
    assert person.linkedin_url == "https://linkedin.com/in/janedoe"
    assert person.snapchat_username == "snapjane"
    assert person.tiktok_handle == "@janedoe"
    assert person.how_we_met == "Art Basel 2024"
    assert person.onboarding_reviewed is True
    assert person.onboarding_reviewed_at is not None


def test_onboarding_progress_endpoint_sums_correctly(tmp_path: Path, monkeypatch) -> None:
    settings = _Settings(tmp_path)
    path = settings.resolve_home() / "state" / "rolodex" / "rolodex.json"
    save_store(
        path,
        RolodexStore(
            people=[
                PersonRecord(person_id="p1", display_name="Dad", relationship_class="family", onboarding_reviewed=True, tier="T1"),
                PersonRecord(person_id="p2", display_name="Bestie", relationship_class="close_friend", tier="T2"),
                PersonRecord(person_id="p3", display_name="Coworker", relationship_class="professional", tier="T3", inbound_message_count=40, outbound_message_count=10),
                PersonRecord(person_id="p4", display_name="Done Already", first_name="Done", last_name="Already", user_note="Reviewed in the past"),
                PersonRecord(person_id="p5", display_name="Spam", relationship_class="spam_or_verification"),
            ]
        ),
    )
    monkeypatch.setattr("agent.web.get_settings", lambda: settings)

    client = TestClient(create_app())
    payload = client.get("/api/onboarding/progress").json()

    assert payload["reviewed"] == 1
    assert payload["total"] == 3
    assert payload["percent"] == 33.33
    assert payload["remaining_priority_segments"] == {
        "family": 0,
        "tier1": 0,
        "tier2": 1,
        "high_message_volume": 1,
        "other": 0,
    }
