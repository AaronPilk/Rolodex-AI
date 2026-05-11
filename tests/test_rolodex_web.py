from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from agent.ops import append_audit_entry
from agent.models import DigestCandidate, DraftBundle, PersonRecord, RolodexStore
from agent.store import load_store, save_store
from agent.web import create_app


class _Settings:
    def __init__(self, home: Path) -> None:
        self._home = home
        self.rolodex_tier_days = {"T1": 14, "T2": 45, "T3": 90, "T4": 180, "T5": 365}
        self.rolodex_daily_send_cap = 5

    def resolve_home(self) -> Path:
        return self._home


def test_web_api_reads_real_store_payload(tmp_path: Path, monkeypatch) -> None:
    settings = _Settings(tmp_path)
    path = settings.resolve_home() / "state" / "rolodex" / "rolodex.json"
    run_id = "rolodex-20260509"
    store = RolodexStore(
        last_sync_at="2026-05-09T20:00:00+00:00",
        people=[
            PersonRecord(
                person_id="p1",
                display_name="Jane Doe",
                inferred_name="Jane",
                relationship_class="close_friend",
                handles=["+14155551234"],
                contact_organization="st.pete client",
                contact_tags=["st.pete", "client"],
                user_note="actually my cousin",
                user_override_class="family",
                user_override_tier="T1",
                user_priority_boost=50,
                last_contacted="2026-05-01T00:00:00+00:00",
                outbound_message_count=3,
                inbound_message_count=2,
                context_summary="Friends who keep in touch casually.",
                topics=["coffee", "travel"],
                tier="T1",
                recent_messages=[{"direction": "outbound", "text": "hey", "at": "2026-05-01T00:00:00+00:00"}],
            )
        ],
        digests={
            run_id: [
                DigestCandidate(
                    run_id=run_id,
                    person_id="p1",
                    display_name="Jane Doe",
                    inferred_name="Jane",
                    relationship_class="close_friend",
                    reason="priority-top",
                    priority=42.5,
                )
            ]
        },
        drafts={
            f"{run_id}:p1": DraftBundle(
                run_id=run_id,
                person_id="p1",
                reason="priority-top",
                prompt="prompt",
                top_draft="hey jane, checking in",
                alternates=["alt one", "alt two"],
                created_at="2026-05-09T20:05:00+00:00",
            )
        },
    )
    save_store(path, store)
    monkeypatch.setattr("agent.web.get_settings", lambda: settings)

    client = TestClient(create_app())
    people = client.get("/api/people").json()
    digest = client.get("/api/digest").json()

    assert people["total_people"] == 1
    assert people["buckets"][0]["bucket"] == "family"
    assert people["buckets"][0]["people"][0]["name"] == "Jane Doe"
    assert people["buckets"][0]["people"][0]["contact_tags"] == ["st.pete", "client"]
    assert people["buckets"][0]["people"][0]["manually_set"] is True
    assert people["counts"]["tiers"]["T1"] == 1
    assert digest["run_id"] == run_id
    assert digest["drafts_by_person_id"]["p1"]["draft_preview"] == "hey jane, checking in"

    detail = client.get("/api/people/p1").json()
    assert detail["recent_messages"][0]["text"] == "hey"


def test_web_api_annotation_updates_store(tmp_path: Path, monkeypatch) -> None:
    settings = _Settings(tmp_path)
    path = settings.resolve_home() / "state" / "rolodex" / "rolodex.json"
    save_store(path, RolodexStore(people=[PersonRecord(person_id="p1", display_name="Jane Doe")]))
    monkeypatch.setattr("agent.web.get_settings", lambda: settings)

    client = TestClient(create_app())
    response = client.post(
        "/api/people/p1/annotate",
        json={
            "user_note": "real family friend",
            "user_override_class": "casual_friend",
            "user_override_tier": "T2",
            "user_priority_boost": 25,
            "do_not_contact": True,
        },
    )
    assert response.status_code == 200

    people = load_store(path).people
    assert people[0].user_note == "real family friend"
    assert people[0].user_override_class == "casual_friend"
    assert people[0].user_override_tier == "T2"
    assert people[0].user_priority_boost == 25
    assert people[0].do_not_contact is True


def test_web_api_annotation_auto_classifies_from_user_note(tmp_path: Path, monkeypatch) -> None:
    settings = _Settings(tmp_path)
    path = settings.resolve_home() / "state" / "rolodex" / "rolodex.json"
    save_store(path, RolodexStore(people=[PersonRecord(person_id="p1", display_name="Stephanie Tocado")]))
    monkeypatch.setattr("agent.web.get_settings", lambda: settings)

    client = TestClient(create_app())
    response = client.post(
        "/api/people/p1/annotate",
        json={"user_note": "Stephanie Tocado is my mother"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["classification"]["label"] == "family"
    assert payload["classification"]["rule_id"] == "operator_note_role"

    people = load_store(path).people
    assert people[0].relationship_class == "family"


def test_web_api_people_hides_spam_or_verification(tmp_path: Path, monkeypatch) -> None:
    settings = _Settings(tmp_path)
    path = settings.resolve_home() / "state" / "rolodex" / "rolodex.json"
    save_store(
        path,
        RolodexStore(
            people=[
                PersonRecord(person_id="p1", display_name="Jane Doe", relationship_class="family"),
                PersonRecord(person_id="p2", display_name="Code", relationship_class="spam_or_verification"),
            ]
        ),
    )
    monkeypatch.setattr("agent.web.get_settings", lambda: settings)

    client = TestClient(create_app())
    payload = client.get("/api/people").json()

    assert payload["total_people"] == 1
    assert payload["people"][0]["person_id"] == "p1"
    assert all(bucket["bucket"] != "spam_or_verification" for bucket in payload["buckets"])


def test_web_api_exposes_audit_and_settings(tmp_path: Path, monkeypatch) -> None:
    settings = _Settings(tmp_path)
    path = settings.resolve_home() / "state" / "rolodex" / "rolodex.json"
    save_store(path, RolodexStore())
    append_audit_entry(settings, {"ts": "2026-05-09T20:00:00+00:00", "action": "send_succeeded", "person_id": "p1"})
    monkeypatch.setattr("agent.web.get_settings", lambda: settings)

    client = TestClient(create_app())
    audit = client.get("/api/audit").json()
    config = client.get("/api/settings").json()

    assert audit["sent_entries"][0]["action"] == "send_succeeded"
    assert config["send_cap"] == 5
    assert config["decrypt_url"] == "/api/decrypt"
    assert "next_fire_at" in config["schedule"]


def test_web_api_exposes_roi_snapshot(tmp_path: Path, monkeypatch) -> None:
    settings = _Settings(tmp_path)
    path = settings.resolve_home() / "state" / "rolodex" / "rolodex.json"
    save_store(
        path,
        RolodexStore(
            people=[
                PersonRecord(
                    person_id="p1",
                    display_name="Pat",
                    relationship_class="business",
                    tier="T2",
                    last_contacted="2026-05-09T12:00:00+00:00",
                    recent_messages=[{"direction": "inbound", "text": "old thread", "at": "2025-12-01T00:00:00+00:00"}],
                ),
                PersonRecord(
                    person_id="p2",
                    display_name="Chris",
                    relationship_class="close_friend",
                    tier="T1",
                    last_contacted="2026-05-08T12:00:00+00:00",
                    recent_messages=[{"direction": "inbound", "text": "recent", "at": "2026-04-20T00:00:00+00:00"}],
                ),
            ]
        ),
    )
    append_audit_entry(settings, {"ts": "2026-05-05T20:00:00+00:00", "action": "send_succeeded", "person_id": "p1"})
    append_audit_entry(settings, {"ts": "2026-05-08T20:00:00+00:00", "action": "send_succeeded", "person_id": "p2"})
    monkeypatch.setattr("agent.web.get_settings", lambda: settings)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 5, 10, 12, 0, tzinfo=tz or UTC)

    monkeypatch.setattr("agent.web.datetime", _FixedDateTime)

    client = TestClient(create_app())
    payload = client.get("/api/roi").json()

    assert payload["active_relationships"] == 2
    assert payload["reconnects_30d"] == 1
    assert payload["reconnects_90d"] == 1
    assert payload["total_sends_30d"] == 2
    assert payload["estimated_pipeline_value_low"] == 5000
    assert payload["method_note"]


def test_web_api_ask_returns_payload(tmp_path: Path, monkeypatch) -> None:
    settings = _Settings(tmp_path)
    path = settings.resolve_home() / "state" / "rolodex" / "rolodex.json"
    save_store(path, RolodexStore(people=[PersonRecord(person_id="p1", display_name="Jane Doe")]))
    monkeypatch.setattr("agent.web.get_settings", lambda: settings)
    monkeypatch.setattr(
        "agent.web.ask_rolodex_query",
        lambda query: {"answer": f"answer for {query}", "people": [{"person_id": "p1", "name": "Jane Doe"}], "person_ids": ["p1"]},
    )

    client = TestClient(create_app())
    payload = client.post("/api/ask", json={"query": "recent context with Jane"}).json()
    assert payload["answer"] == "answer for recent context with Jane"


def test_web_api_ask_action_returns_drafts(tmp_path: Path, monkeypatch) -> None:
    settings = _Settings(tmp_path)
    path = settings.resolve_home() / "state" / "rolodex" / "rolodex.json"
    save_store(
        path,
        RolodexStore(
            people=[
                PersonRecord(
                    person_id="p1",
                    display_name="Taylor",
                    first_name="Taylor",
                    relationship_class="met_briefly",
                    inbound_message_count=1,
                    outbound_message_count=1,
                    recent_messages=[
                        {"direction": "inbound", "text": "this is Taylor", "at": "2026-05-01T00:00:00+00:00"},
                        {"direction": "outbound", "text": "i'm Aaron", "at": "2026-05-01T00:01:00+00:00"},
                    ],
                )
            ]
        ),
    )
    monkeypatch.setattr("agent.web.get_settings", lambda: settings)

    client = TestClient(create_app())
    payload = client.post(
        "/api/ask/action",
        json={
            "instruction": "find people I just exchanged names with and queue a reconnect message: 'hey {name}, it's Aaron, good to reconnect'",
            "dry_run": True,
            "max_targets": 10,
        },
    ).json()

    assert payload["action"] == "draft_outreach"
    assert payload["matched_count"] == 1
    assert payload["selected_count"] == 1
    assert payload["would_send"] is True
    assert payload["drafts"][0]["draft"] == "hey Taylor, it's Aaron, good to reconnect"


def test_web_api_regenerates_draft(tmp_path: Path, monkeypatch) -> None:
    settings = _Settings(tmp_path)
    path = settings.resolve_home() / "state" / "rolodex" / "rolodex.json"
    run_id = "rolodex-20260509"
    save_store(
        path,
        RolodexStore(
            people=[PersonRecord(person_id="p1", display_name="Taylor", relationship_class="met_briefly")],
            digests={run_id: []},
        ),
    )
    monkeypatch.setattr("agent.web.get_settings", lambda: settings)

    async def _fake_generate(person, reason="post-feedback"):
        return DraftBundle(
            run_id="ignored",
            person_id=person.person_id,
            reason=reason,
            prompt="prompt",
            top_draft="Fresh post-feedback draft",
            alternates=["alt"],
            created_at="2026-05-10T00:00:00+00:00",
        )

    monkeypatch.setattr("agent.web._generate_post_feedback_draft", _fake_generate)

    client = TestClient(create_app())
    payload = client.post("/api/people/p1/regenerate-draft", json={}).json()

    assert payload["ok"] is True
    assert payload["draft"]["draft_preview"] == "Fresh post-feedback draft"


def test_web_api_exposes_inbound_poll_status_and_activity(tmp_path: Path, monkeypatch) -> None:
    settings = _Settings(tmp_path)
    path = settings.resolve_home() / "state" / "rolodex" / "rolodex.json"
    save_store(
        path,
        RolodexStore(
            inbound_poll_state={"telegram": "2026-05-11T12:55:00+00:00"},
            inbound_poll_status={"telegram": {"last_polled_at": "2026-05-11T12:55:00+00:00", "messages_last_pull": 3, "last_error": None}},
            people=[
                PersonRecord(
                    person_id="p1",
                    display_name="Jane Doe",
                    recent_messages=[{"direction": "inbound", "text": "latest inbound hello", "at": "2026-05-11T12:54:00+00:00", "channel": "telegram"}],
                )
            ],
        ),
    )
    monkeypatch.setattr("agent.web.get_settings", lambda: settings)

    client = TestClient(create_app())
    status = client.get("/api/inbound-poll/status").json()
    people = client.get("/api/people").json()

    assert status["telegram"]["messages_last_pull"] == 3
    assert people["inbound_activity"][0]["sender_name"] == "Jane Doe"


def test_web_api_digest_trigger_now_calls_scheduler(tmp_path: Path, monkeypatch) -> None:
    settings = _Settings(tmp_path)
    path = settings.resolve_home() / "state" / "rolodex" / "rolodex.json"
    save_store(path, RolodexStore())
    monkeypatch.setattr("agent.web.get_settings", lambda: settings)

    called: list[str] = []

    class _Scheduler:
        async def run_now(self):
            called.append("scheduler")

    monkeypatch.setattr("agent.web.get_active_scheduler", lambda: _Scheduler())

    client = TestClient(create_app())
    payload = client.post("/api/digest/trigger-now").json()

    assert payload["ok"] is True
    assert called == ["scheduler"]
