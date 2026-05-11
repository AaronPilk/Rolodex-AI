from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent.cli import build_parser
from agent.models import MessageSample, PersonRecord, RolodexStore
from agent.store import decrypt_store_to_dict, save_store


class _Settings:
    def __init__(self, home: Path) -> None:
        self._home = home

    def resolve_home(self) -> Path:
        return self._home


def _seed_store(path: Path) -> None:
    save_store(
        path,
        RolodexStore(
            people=[
                PersonRecord(
                    person_id="p1",
                    display_name="Jane",
                    handles=["+14155551234"],
                    recent_messages=[
                        MessageSample(
                            rowid=1,
                            direction="outbound",
                            text="hey",
                            at="2026-05-01T00:00:00+00:00",
                        )
                    ],
                    last_message_at="2026-05-01T00:00:00+00:00",
                    relationship_class="close_friend",
                    outbound_message_count=1,
                )
            ]
        ),
    )


def test_inspect_pretty_prints_one_person_without_plaintext_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "state" / "rolodex" / "rolodex.json"
    _seed_store(path)
    monkeypatch.setattr("agent.cli.get_settings", lambda: _Settings(tmp_path))
    monkeypatch.setenv("ROLODEX_ALLOW_SCRIPTED_DECRYPT", "1")

    args = build_parser().parse_args(["inspect", "--person", "p1"])
    assert args.func(args) == 0

    out = capsys.readouterr().out
    assert json.loads(out)["display_name"] == "Jane"
    assert not path.exists()
    assert path.with_suffix(".json.enc").exists()


def test_reencrypt_roundtrip_preserves_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "state" / "rolodex" / "rolodex.json"
    _seed_store(path)
    original = decrypt_store_to_dict(path)
    plaintext = tmp_path / "edited.json"
    plaintext.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr("agent.cli.get_settings", lambda: _Settings(tmp_path))
    monkeypatch.setenv("ROLODEX_ALLOW_SCRIPTED_DECRYPT", "1")

    args = build_parser().parse_args(["reencrypt", "--from", str(plaintext)])
    assert args.func(args) == 0
    capsys.readouterr()

    reloaded = decrypt_store_to_dict(path)
    assert reloaded["people"] == original["people"]
    assert reloaded["drafts"] == original["drafts"]
    assert reloaded["digests"] == original["digests"]
    assert reloaded["daily_sends"] == original["daily_sends"]


def test_status_prints_health_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("agent.cli.get_settings", lambda: _Settings(tmp_path))
    _seed_store(tmp_path / "state" / "rolodex" / "rolodex.json")
    monkeypatch.setattr(
        "agent.cli.collect_health",
        lambda _settings: type(
            "_Health",
            (),
            {
                "model_dump": lambda self: {
                    "person_count": 1,
                    "last_sync_at": None,
                    "last_digest_at": None,
                    "sends_today": 0,
                    "cap": 5,
                    "recent_errors": [],
                    "encrypted_store_present": True,
                    "keychain_accessible": True,
                    "imessage_db_accessible": False,
                    "twilio_configured": False,
                }
            },
        )(),
    )

    args = build_parser().parse_args(["status"])
    assert args.func(args) == 0

    out = capsys.readouterr().out
    assert "Rolodex status" in out
    assert "People: 1" in out
    assert "Last-message years:" in out
    assert "- 2026: 1" in out


def test_sync_runs_ingest_and_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("agent.cli.get_settings", lambda: _Settings(tmp_path))
    started: list[list[str]] = []
    monkeypatch.setattr(
        "agent.cli.sync_imessage_threads",
        lambda **kwargs: type(
            "_Report",
            (),
            {
                "scanned_threads": 3,
                "created_people": 2,
                "updated_people": 1,
                "total_people": 4,
                "skipped_group_threads": 1,
                "tagged_group_threads": 2,
                "store_path": str(tmp_path / "state" / "rolodex" / "rolodex.json"),
                "warnings": [],
            },
        )(),
    )
    monkeypatch.setattr(
        "agent.cli.subprocess.Popen",
        lambda cmd, **_kwargs: started.append(cmd) or type("_Proc", (), {})(),
    )

    args = build_parser().parse_args(["sync", "--max-threads", "10", "--max-messages-per-thread", "20"])
    assert args.func(args) == 0

    out = capsys.readouterr().out
    assert "Rolodex sync" in out
    assert "Scanned threads: 3" in out
    assert "Created people: 2" in out
    assert "Total people: 4" in out
    assert started == [[sys.executable, "-m", "agent", "enrich"]]


def test_enrich_runs_and_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("agent.cli.get_settings", lambda: _Settings(tmp_path))
    monkeypatch.setattr(
        "agent.cli.enrich_people",
        lambda **_kwargs: {"processed": 3, "updated": 2, "warnings": []},
    )

    args = build_parser().parse_args(["enrich", "--force-reclassify"])
    assert args.func(args) == 0

    out = capsys.readouterr().out
    assert "Rolodex enrich" in out
    assert "Processed people: 3" in out


def test_reclassify_deterministic_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("agent.cli.get_settings", lambda: _Settings(tmp_path))
    monkeypatch.setattr(
        "agent.cli.reclassify_people_deterministically",
        lambda **_kwargs: {
            "updated": 2,
            "preserved_overrides": 1,
            "applied_by_rule": {"role_name_first_name": 1, "service_provider_keyword": 1},
            "before_counts": {"business": 1, "unknown": 2},
            "after_counts": {"family": 1, "service_provider": 1, "unknown": 1},
        },
    )

    args = build_parser().parse_args(["reclassify-deterministic"])
    assert args.func(args) == 0

    out = capsys.readouterr().out
    assert "Rolodex reclassify-deterministic" in out
    assert "Updated people: 2" in out
    assert "- role_name_first_name: 1" in out


def test_merge_duplicates_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("agent.cli.get_settings", lambda: _Settings(tmp_path))
    monkeypatch.setattr(
        "agent.cli.merge_duplicate_people",
        lambda **_kwargs: {"before": 10, "after": 7, "merged": 3},
    )

    args = build_parser().parse_args(["merge-duplicates"])
    assert args.func(args) == 0

    out = capsys.readouterr().out
    assert "Rolodex merge-duplicates" in out
    assert "People before: 10" in out
    assert "Records merged: 3" in out


def test_contacts_import_runs_and_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("agent.cli.get_settings", lambda: _Settings(tmp_path))
    monkeypatch.setattr(
        "agent.cli.import_contacts",
        lambda **_kwargs: {
            "contacts_found": 12,
            "matched_existing": 7,
            "new_records_created": 5,
            "total_people_now": 20,
        },
    )

    args = build_parser().parse_args(["contacts", "import"])
    assert args.func(args) == 0

    out = capsys.readouterr().out
    assert "Rolodex contacts import" in out
    assert "Contacts found: 12" in out


def test_retier_runs_and_prints_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("agent.cli.get_settings", lambda: _Settings(tmp_path))
    path = tmp_path / "state" / "rolodex" / "rolodex.json"
    _seed_store(path)
    monkeypatch.setattr("agent.cli.auto_assign_tiers", lambda store: {"T1": 1, "T2": 2, "T3": 3, "T4": 4, "T5": 5})

    args = build_parser().parse_args(["retier"])
    assert args.func(args) == 0

    out = capsys.readouterr().out
    assert "Rolodex retier" in out
    assert "T1: 1" in out


def test_note_updates_person_by_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent.cli.get_settings", lambda: _Settings(tmp_path))
    path = tmp_path / "state" / "rolodex" / "rolodex.json"
    _seed_store(path)

    args = build_parser().parse_args(["note", "Jane", "real friend from college"])
    assert args.func(args) == 0

    payload = decrypt_store_to_dict(path)
    assert payload["people"][0]["user_note"] == "real friend from college"


def test_ask_action_prints_draft_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("agent.cli.get_settings", lambda: _Settings(tmp_path))
    path = tmp_path / "state" / "rolodex" / "rolodex.json"
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
                        MessageSample(direction="inbound", text="this is Taylor", at="2026-05-01T00:00:00+00:00"),
                        MessageSample(direction="outbound", text="i'm Aaron", at="2026-05-01T00:01:00+00:00"),
                    ],
                )
            ]
        ),
    )

    args = build_parser().parse_args([
        "ask-action",
        "find people I just exchanged names with and draft a reconnect: 'hey {name}, it's Aaron'",
    ])
    assert args.func(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_count"] == 1
    assert payload["drafts"][0]["draft"] == "hey Taylor, it's Aaron"


def test_dnc_and_override_update_person(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent.cli.get_settings", lambda: _Settings(tmp_path))
    path = tmp_path / "state" / "rolodex" / "rolodex.json"
    _seed_store(path)

    args = build_parser().parse_args(["override", "p1", "--class", "casual_friend"])
    assert args.func(args) == 0
    args = build_parser().parse_args(["dnc", "p1"])
    assert args.func(args) == 0

    payload = decrypt_store_to_dict(path)
    assert payload["people"][0]["user_override_class"] == "casual_friend"
    assert payload["people"][0]["do_not_contact"] is True


def test_digest_passes_limit_and_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _daily_run(*, dry_run: bool | None = None, limit: int = 5):
        captured["dry_run"] = dry_run
        captured["limit"] = limit
        return []

    monkeypatch.setattr("agent.daemon.daily_run", _daily_run)

    args = build_parser().parse_args(["digest", "--dry-run", "--limit", "7"])
    assert args.func(args) == 0
    assert captured == {"dry_run": True, "limit": 7}


def test_audit_prints_store_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("agent.cli.get_settings", lambda: _Settings(tmp_path))
    _seed_store(tmp_path / "state" / "rolodex" / "rolodex.json")

    args = build_parser().parse_args(["audit"])
    assert args.func(args) == 0

    out = capsys.readouterr().out
    assert "Rolodex audit" in out
    assert "Last-message years:" in out
    assert "Classification: 1 classified, 0 unclassified" in out


def test_resync_wipes_messages_runs_sync_and_force_enrich(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("agent.cli.get_settings", lambda: _Settings(tmp_path))
    path = tmp_path / "state" / "rolodex" / "rolodex.json"
    _seed_store(path)
    calls: dict[str, object] = {}

    def _sync(**kwargs):
        calls["sync"] = kwargs
        store = decrypt_store_to_dict(path)
        assert store["people"][0]["recent_messages"] == []
        save_store(
            path,
            RolodexStore(
                people=[
                    PersonRecord(
                        person_id="p1",
                        display_name="Jane",
                        handles=["+14155551234"],
                        recent_messages=[
                            MessageSample(
                                rowid=2,
                                direction="inbound",
                                text="fresh",
                                at="2026-05-10T00:00:00+00:00",
                            )
                        ],
                        last_message_at="2026-05-10T00:00:00+00:00",
                        relationship_class="unknown",
                        inbound_message_count=1,
                    )
                ]
            ),
        )
        return type(
            "_Report",
            (),
            {
                "scanned_threads": 1,
                "created_people": 0,
                "updated_people": 1,
                "total_people": 1,
                "warnings": [],
            },
        )()

    monkeypatch.setattr("agent.cli.sync_imessage_threads", _sync)

    def _enrich(**kwargs):
        calls["enrich"] = kwargs
        save_store(
            path,
            RolodexStore(
                people=[
                    PersonRecord(
                        person_id="p1",
                        display_name="Jane",
                        handles=["+14155551234"],
                        recent_messages=[
                            MessageSample(
                                rowid=2,
                                direction="inbound",
                                text="fresh",
                                at="2026-05-10T00:00:00+00:00",
                            )
                        ],
                        last_message_at="2026-05-10T00:00:00+00:00",
                        relationship_class="close_friend",
                        inbound_message_count=1,
                    )
                ]
            ),
        )
        return {"processed": 1, "updated": 1, "warnings": []}

    monkeypatch.setattr("agent.cli.enrich_people", _enrich)

    args = build_parser().parse_args(["resync"])
    assert args.func(args) == 0

    out = capsys.readouterr().out
    assert "Rolodex resync" in out
    assert "- 2026: 1" in out
    assert calls["sync"]["max_threads"] is None
    assert calls["sync"]["max_messages_per_thread"] is None
    assert calls["enrich"]["force_reclassify"] is True
