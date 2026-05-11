from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.models import PersonRecord, RolodexStore
from agent.ops import append_audit_entry, append_rotating_log, audit_log_path, collect_health
from agent.store import save_store


class _Settings:
    def __init__(self, home: Path) -> None:
        self._home = home
        self.rolodex_daily_send_cap = 5

    def resolve_home(self) -> Path:
        return self._home


def test_append_rotating_log_rotates(tmp_path: Path) -> None:
    path = tmp_path / "errors.log"
    for idx in range(12):
        append_rotating_log(path, f"line-{idx}-{'x' * 40}", max_bytes=80)
    assert path.exists()
    assert path.with_name("errors.log.1").exists()


def test_audit_log_writes_json_lines(tmp_path: Path) -> None:
    settings = _Settings(tmp_path)
    append_audit_entry(
        settings,
        {"ts": "2026-05-08T12:00:00+00:00", "action": "skip", "person_id": "p1", "details": {}},
    )
    line = audit_log_path(settings).read_text(encoding="utf-8").splitlines()[-1]
    payload = json.loads(line)
    assert payload["action"] == "skip"
    assert payload["person_id"] == "p1"


def test_collect_health_reports_failure_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _Settings(tmp_path)
    path = settings.resolve_home() / "state" / "rolodex" / "rolodex.json"
    today = datetime.now(UTC).date().isoformat()
    save_store(
        path,
        RolodexStore(
            people=[PersonRecord(person_id="p1")],
            daily_sends={today: 2},
            recent_errors=["e1", "e2"],
            last_sync_at="2026-05-08T09:00:00+00:00",
            last_digest_at="2026-05-08T09:05:00+00:00",
        ),
    )
    monkeypatch.setattr(
        "agent.ops.check_messages_status",
        lambda: type("Status", (), {"available": False})(),
    )
    monkeypatch.setattr("agent.ops.keychain_accessible", lambda _settings: False)
    monkeypatch.setattr("agent.ops.twilio_configured", lambda: False)

    health = collect_health(settings).model_dump()
    assert health["person_count"] == 1
    assert health["sends_today"] == 2
    assert health["imessage_db_accessible"] is False
    assert health["keychain_accessible"] is False
