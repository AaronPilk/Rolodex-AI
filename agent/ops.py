from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from shutil import which

from agent.imessage_reader import health_check as imessage_health_check
from agent.models import RolodexHealth, RolodexStore
from agent.store import _encrypted_path, _key_path, load_store, store_path

_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5
_ERROR_BUFFER_LIMIT = 50


@dataclass
class MessageStatus:
    available: bool
    reason: str = ""


def check_messages_status() -> MessageStatus:
    ok, reason = imessage_health_check()
    return MessageStatus(available=ok, reason=reason)


def rolodex_state_dir(settings) -> Path:
    return settings.resolve_home() / "state" / "rolodex"


def sends_log_path(settings) -> Path:
    return rolodex_state_dir(settings) / "sends.log"


def errors_log_path(settings) -> Path:
    return rolodex_state_dir(settings) / "errors.log"


def audit_log_path(settings) -> Path:
    return rolodex_state_dir(settings) / "audit.log"


def _rotating_handler(path: Path, *, max_bytes: int = _MAX_BYTES) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def append_rotating_log(path: Path, message: str, *, max_bytes: int = _MAX_BYTES) -> None:
    logger = logging.getLogger(f"rolodex.{path.name}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(
        isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == path
        for handler in logger.handlers
    ):
        logger.handlers.clear()
        logger.addHandler(_rotating_handler(path, max_bytes=max_bytes))
    logger.info(message)


def append_audit_entry(settings, entry: dict) -> None:
    append_rotating_log(audit_log_path(settings), json.dumps(entry, sort_keys=True))


def tail_audit_entries(settings, *, count: int = 50) -> list[dict]:
    path = audit_log_path(settings)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-max(0, count):]
    out: list[dict] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"raw": line})
    return out


def push_recent_error(store: RolodexStore, message: str) -> None:
    store.recent_errors.append(message)
    if len(store.recent_errors) > _ERROR_BUFFER_LIMIT:
        del store.recent_errors[:-_ERROR_BUFFER_LIMIT]


def keychain_accessible(settings) -> bool:
    path = store_path(settings)
    if _key_path(path).exists():
        return True
    if os.getenv("PYTHON_KEYRING_BACKEND"):
        return True
    return which("security") is not None


def twilio_configured() -> bool:
    return bool(
        os.getenv("TWILIO_ACCOUNT_SID")
        and os.getenv("TWILIO_AUTH_TOKEN")
        and os.getenv("TWILIO_FROM_NUMBER")
    )


def collect_health(settings) -> RolodexHealth:
    path = store_path(settings)
    store = load_store(path)
    from datetime import UTC, datetime

    today = datetime.now(UTC).date().isoformat()
    messages_status = check_messages_status()
    cap = max(1, int(getattr(settings, "rolodex_daily_send_cap", 5) or 5))
    return RolodexHealth(
        person_count=len(store.people),
        last_sync_at=store.last_sync_at,
        last_digest_at=store.last_digest_at,
        sends_today=int(store.daily_sends.get(today, 0) or 0),
        cap=cap,
        recent_errors=list(store.recent_errors[-5:]),
        encrypted_store_present=_encrypted_path(path).exists(),
        keychain_accessible=keychain_accessible(settings),
        imessage_db_accessible=messages_status.available,
        twilio_configured=twilio_configured(),
    )
