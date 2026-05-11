from __future__ import annotations

import base64
import fcntl
import json
import os
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

from agent.models import PersonRecord, RolodexStore

_KEYCHAIN_SERVICE = "RolodexAI.store-key"
_KEY_FILE_MODE = 0o600
_KEY_BYTES = 32
_SALT_BYTES = 16
_NONCE_BYTES = 12
_KDF_ITERATIONS = 200_000
_LOCK_FILE_NAME = ".lock"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def store_path(settings=None) -> Path:
    if settings is None:
        from agent.config import get_settings

        settings = get_settings()
    return settings.resolve_home() / "state" / "rolodex" / "rolodex.json"


def _encrypted_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".enc")


def _salt_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".salt")


def _key_path(path: Path) -> Path:
    return path.parent / ".key"


def _read_json_dict(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _migrate_store_data(data: dict) -> dict:
    migrated = dict(data or {})
    migrated.setdefault("version", 1)
    migrated.setdefault("updated_at", None)
    migrated.setdefault("last_sync_at", None)
    migrated.setdefault("last_digest_at", None)
    migrated.setdefault("people", [])
    migrated.setdefault("drafts", {})
    migrated.setdefault("digests", {})
    migrated.setdefault("daily_sends", {})
    migrated.setdefault("inbound_poll_state", {})
    migrated.setdefault("inbound_poll_offsets", {})
    migrated.setdefault("inbound_poll_status", {})
    migrated.setdefault("recent_errors", [])
    for person in migrated.get("people", []):
        if not isinstance(person, dict):
            continue
        person.setdefault("display_name", None)
        person.setdefault("first_name", None)
        person.setdefault("last_name", None)
        person.setdefault("inferred_name", None)
        person.setdefault("company", None)
        person.setdefault("contact_organization", None)
        person.setdefault("contact_tags", [])
        person.setdefault("birthday", None)
        person.setdefault("source", "imessage")
        person.setdefault("notes", None)
        person.setdefault("user_note", None)
        person.setdefault("user_override_class", None)
        person.setdefault("user_override_tier", None)
        person.setdefault("user_priority_boost", None)
        person.setdefault("user_marked_at", None)
        person.setdefault("context_summary", None)
        person.setdefault("topics", [])
        person.setdefault("handles", [])
        person.setdefault("connected_channels", [])
        person.setdefault("channels", [])
        person.setdefault("tone_profile", {})
        person.setdefault("group_threads", [])
        person.setdefault("life_events", [])
        person.setdefault("scoring", {})
        person.setdefault("cadence", {})
        person.setdefault("recent_messages", [])
        person.setdefault("relationship_class", None)
        person.setdefault("tier", "T3")
        person.setdefault("user_priority", 0.0)
        person.setdefault("do_not_contact", False)
        person.setdefault("relationship_classification_hash", None)
        person.setdefault("relationship_classified_at", None)
        person.setdefault("profile_enrichment_hash", None)
        person.setdefault("profile_enriched_at", None)
        person.setdefault("sensitivity_flags", [])
        person.setdefault("sensitivity_classification_hash", None)
        person.setdefault("sensitivity_classified_at", None)
        person.setdefault("natural_end_classification", None)
        person.setdefault("last_contacted", None)
        person.setdefault("last_message_at", None)
        person.setdefault("last_message_direction", None)
        person.setdefault("inbound_message_count", 0)
        person.setdefault("outbound_message_count", 0)
        person.setdefault("history", {})
        person.setdefault("created_at", None)
        person.setdefault("updated_at", None)
        tone = person.get("tone_profile")
        if isinstance(tone, dict):
            tone.setdefault("shibboleth_phrases", [])
            tone.setdefault("topic_graph", [])
            tone.setdefault("callbacks", [])
            tone.setdefault("feedback_log", [])
        scoring = person.get("scoring")
        if isinstance(scoring, dict):
            scoring.setdefault("warmth", 0.0)
            scoring.setdefault("responsiveness", 0.0)
            scoring.setdefault("freshness_decay", 0.0)
            scoring.setdefault("user_priority_boost", 0.0)
            scoring.setdefault("life_event_proximity", 0.0)
            scoring.setdefault("priority_score", 0.0)
            scoring.setdefault("natural_end_score", 0.0)
        cadence = person.get("cadence")
        if isinstance(cadence, dict):
            cadence.setdefault("tier", person.get("tier") or "T3")
            cadence.setdefault("target_days", None)
            cadence.setdefault("days_since_last", None)
            cadence.setdefault("is_overdue", False)
            cadence.setdefault("days_overdue", 0)
            cadence.setdefault("snooze_until", None)
            cadence.setdefault("last_digest_run_id", None)
            cadence.setdefault("last_digest_at", None)
            cadence.setdefault("last_sent_at", None)
            cadence.setdefault("last_skipped_run_id", None)
        history = person.get("history")
        if isinstance(history, dict):
            history.setdefault("log", [])
            history.setdefault("total_contacts_initiated_by_me", 0)
            for entry in history.get("log", []):
                if isinstance(entry, dict):
                    entry.setdefault("message_id", None)
                    entry.setdefault("channel_used", None)
    return migrated


def _secure_write_bytes(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    os.chmod(path, _KEY_FILE_MODE)


def lock_path(path: Path) -> Path:
    return path.parent / _LOCK_FILE_NAME


@contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield fh
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _is_default_home_store(path: Path) -> bool:
    try:
        default_home = store_path().parent.parent.resolve()
        return path.expanduser().resolve().is_relative_to(default_home)
    except Exception:
        return False


def _keychain_get(path: Path) -> bytes | None:
    if not _is_default_home_store(path):
        return None
    account = base64.urlsafe_b64encode(str(path.parent).encode("utf-8")).decode("ascii")
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                account,
                "-s",
                _KEYCHAIN_SERVICE,
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    secret = result.stdout.strip()
    if not secret:
        return None
    return base64.b64decode(secret)


def _keychain_set(path: Path, key: bytes) -> bool:
    if not _is_default_home_store(path):
        return False
    account = base64.urlsafe_b64encode(str(path.parent).encode("utf-8")).decode("ascii")
    try:
        subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-a",
                account,
                "-s",
                _KEYCHAIN_SERVICE,
                "-w",
                base64.b64encode(key).decode("ascii"),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
    return True


def _prefer_file_key_backend() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ or os.environ.get("ROLODEX_TEST_FILE_KEYRING") == "1"


def _keyring_get() -> bytes | None:
    try:
        import keyring  # type: ignore
    except ImportError:
        return None
    try:
        secret = keyring.get_password(_KEYCHAIN_SERVICE, "default")
    except Exception:
        return None
    if not secret:
        return None
    return base64.b64decode(secret)


def _keyring_set(key: bytes) -> bool:
    try:
        import keyring  # type: ignore
    except ImportError:
        return False
    try:
        keyring.set_password(_KEYCHAIN_SERVICE, "default", base64.b64encode(key).decode("ascii"))
    except Exception:
        return False
    return True


def _load_or_create_master_key(path: Path) -> bytes:
    file_getter = lambda: _key_path(path).read_bytes() if _key_path(path).exists() else None
    getters = (file_getter,) if _prefer_file_key_backend() else (
        lambda: _keychain_get(path),
        _keyring_get,
        file_getter,
    )
    for getter in getters:
        key = getter()
        if key:
            return key
    key = os.urandom(_KEY_BYTES)
    if _prefer_file_key_backend():
        key_file = _key_path(path)
        key_file.parent.mkdir(parents=True, exist_ok=True)
        _secure_write_bytes(key_file, key)
        return key
    if _keychain_set(path, key):
        return key
    if _keyring_set(key):
        return key
    key_file = _key_path(path)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    _secure_write_bytes(key_file, key)
    return key


def _load_or_create_salt(path: Path) -> bytes:
    salt_file = _salt_path(path)
    if salt_file.exists():
        return salt_file.read_bytes()
    salt = os.urandom(_SALT_BYTES)
    salt_file.parent.mkdir(parents=True, exist_ok=True)
    _secure_write_bytes(salt_file, salt)
    return salt


def _derive_data_key(master_key: bytes, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=salt,
        iterations=_KDF_ITERATIONS,
    )
    return kdf.derive(master_key)


def _encrypt_bytes(path: Path, plaintext: bytes) -> bytes:
    key = _derive_data_key(_load_or_create_master_key(path), _load_or_create_salt(path))
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def _decrypt_bytes(path: Path, ciphertext: bytes) -> bytes:
    if len(ciphertext) <= _NONCE_BYTES:
        raise ValueError("rolodex ciphertext is truncated")
    key = _derive_data_key(_load_or_create_master_key(path), _load_or_create_salt(path))
    nonce = ciphertext[:_NONCE_BYTES]
    payload = ciphertext[_NONCE_BYTES:]
    return AESGCM(key).decrypt(nonce, payload, None)


def _load_encrypted_json_dict(path: Path) -> dict:
    enc_path = _encrypted_path(path)
    plaintext = _decrypt_bytes(path, enc_path.read_bytes())
    return json.loads(plaintext.decode("utf-8"))


def decrypt_store_to_dict(path: Path) -> dict:
    enc_path = _encrypted_path(path)
    if path.exists():
        return _migrate_store_data(_read_json_dict(path))
    if not enc_path.exists():
        return _migrate_store_data({})
    return _migrate_store_data(_load_encrypted_json_dict(path))


def decrypt_store_to_text(path: Path, *, indent: int = 2) -> str:
    return json.dumps(decrypt_store_to_dict(path), indent=indent)


def write_encrypted_store_from_plaintext(path: Path, plaintext: str) -> RolodexStore:
    data = _migrate_store_data(json.loads(plaintext))
    store = RolodexStore.model_validate(data)
    save_store(path, store)
    return store


def _load_store_unlocked(path: Path) -> RolodexStore:
    enc_path = _encrypted_path(path)
    if path.exists():
        data = _read_json_dict(path)
        migrated = _migrate_store_data(data)
        store = RolodexStore.model_validate(migrated)
        save_store(path, store, already_locked=True)
        path.unlink(missing_ok=True)
        return store
    if not enc_path.exists():
        return RolodexStore()
    data = _load_encrypted_json_dict(path)
    migrated = _migrate_store_data(data)
    store = RolodexStore.model_validate(migrated)
    if migrated != data:
        save_store(path, store, already_locked=True)
    return store


def load_store(path: Path) -> RolodexStore:
    with file_lock(lock_path(path)):
        return _load_store_unlocked(path)


def _atomic_write_encrypted_store(path: Path, payload: bytes) -> None:
    enc_path = _encrypted_path(path)
    tmp_path = enc_path.with_suffix(enc_path.suffix + ".tmp")
    with tmp_path.open("wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp_path, _KEY_FILE_MODE)
    os.replace(tmp_path, enc_path)
    dir_fd = os.open(str(enc_path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def save_store(path: Path, store: RolodexStore, *, already_locked: bool = False) -> None:
    if not already_locked:
        with file_lock(lock_path(path)):
            save_store(path, store, already_locked=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    store.updated_at = _now_iso()
    payload = store.model_dump_json(indent=2).encode("utf-8")
    _atomic_write_encrypted_store(path, _encrypt_bytes(path, payload))
    path.unlink(missing_ok=True)


@contextmanager
def store_transaction(path: Path):
    with file_lock(lock_path(path)):
        store = _load_store_unlocked(path)
        yield store
        save_store(path, store, already_locked=True)


def _handle_key(handle: str) -> str:
    text = handle.strip().lower()
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits:
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        return digits
    return text


def get_person_by_handle(store: RolodexStore, handle: str) -> PersonRecord | None:
    key = _handle_key(handle)
    for person in store.people:
        if any(_handle_key(h) == key for h in person.handles):
            return person
        if any(_handle_key(ch.handle) == key for ch in person.channels):
            return person
    return None


def upsert_person(store: RolodexStore, person: PersonRecord) -> PersonRecord:
    person.updated_at = _now_iso()
    if not person.created_at:
        person.created_at = person.updated_at
    seen: set[str] = set()
    deduped_handles: list[str] = []
    for handle in person.handles:
        key = _handle_key(handle)
        if key in seen:
            continue
        seen.add(key)
        deduped_handles.append(handle)
    person.handles = deduped_handles
    for idx, existing in enumerate(store.people):
        if existing.person_id == person.person_id:
            store.people[idx] = person
            return person
    store.people.append(person)
    return person
