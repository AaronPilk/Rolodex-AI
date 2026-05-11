"""
contacts_reader.py — standalone macOS Contacts reader.

Replaces PILK's `contacts_search` tool. Uses AppleScript via osascript subprocess
because it's the lowest-friction path that works on every macOS without compiling
PyObjC bridges.

Requires Contacts permission for whatever app is running this. The first time
osascript talks to Contacts.app the OS prompts the user.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path


@dataclass
class Contact:
    """A macOS Contact card lookup result."""

    full_name: str
    first_name: str | None
    last_name: str | None
    phones: list[str]
    emails: list[str]
    organization: str | None
    parsed_tags: list[str] = field(default_factory=list)
    birthday: date | None = None
    notes: str | None = None

    @property
    def name(self) -> str:
        return self.full_name


_OSASCRIPT = shutil.which("osascript")
_ROW_SEP = "\u241e"
_FIELD_SEP = "\u241f"
_LIST_SEP = "\u241d"
_CONTACT_CACHE: tuple[datetime, list[Contact]] | None = None
_CONTACT_CACHE_TTL_SECONDS = 300

# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
# LIVE CONTACTS GATE \u2014 critical safety lock.
#
# Background loops (the 5-min inbound poller, the 9am digest cron) MUST NOT
# call macOS Contacts via AppleScript. Doing so:
#   1. Opens the Contacts.app GUI repeatedly (`open -a Contacts`)
#   2. Runs a 300-second AppleScript that pulls every contact
#   3. With unknown IG/FB handles in the inbound stream, this fires on
#      every poll cycle \u2192 locked up the operator's machine, requiring a
#      hard restart.
#
# Live AppleScript Contacts access is now ONLY enabled when the operator
# explicitly opts in via `python -m agent contacts-import` (which sets the
# env var below). Everything else uses the snapshot file at
# ~/.rolodex-ai/state/rolodex/contacts_snapshot.json that was written on
# the last explicit import.
# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
_LIVE_OK_ENV = "ROLODEX_LIVE_CONTACTS_ALLOWED"


def allow_live_contacts() -> None:
    """Called by `agent contacts-import` to opt in for this process."""
    os.environ[_LIVE_OK_ENV] = "1"


def _live_contacts_allowed() -> bool:
    return os.environ.get(_LIVE_OK_ENV, "").strip() in {"1", "true", "yes", "on"}


def _run_osa(script: str, timeout: float = 10.0) -> str:
    if not _OSASCRIPT:
        raise RuntimeError("osascript not found; not running on macOS?")
    if not _live_contacts_allowed():
        raise RuntimeError(
            "Live Contacts access is disabled. Snapshot-only mode. "
            "Run `python -m agent contacts-import` once explicitly to refresh."
        )
    proc = subprocess.run(
        [_OSASCRIPT, "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"osascript failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _ensure_contacts_running() -> None:
    # Never open Contacts.app from a background context. Only the explicit
    # `contacts-import` CLI command opts in via the gate above.
    if not _live_contacts_allowed():
        return
    try:
        subprocess.run(["open", "-a", "Contacts"], check=False, capture_output=True, text=True, timeout=10.0)
    except Exception:
        return


def _snapshot_path() -> Path:
    from agent.config import get_settings

    settings = get_settings()
    return settings.resolve_home() / "state" / "rolodex" / "contacts_snapshot.json"


def _normalize_phone(raw: str) -> str:
    s = raw.strip()
    if not s:
        return s
    has_plus = s.startswith("+")
    digits = "".join(ch for ch in s if ch.isdigit())
    return ("+" + digits) if has_plus else digits


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    tags: list[str] = []
    for token in re.split(r"[\s,]+", raw.strip()):
        cleaned = token.strip().strip(".").lower()
        if not cleaned or cleaned in tags:
            continue
        tags.append(cleaned)
    return tags


def _clean_text(value: str) -> str | None:
    stripped = value.strip()
    if not stripped or stripped == "missing value":
        return None
    return stripped


def _parse_birthday(value: str) -> date | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned)
    except ValueError:
        return None


def _contact_script(filter_clause: str = "", limit: int | None = None) -> str:
    limit_clause = f"if i > {int(limit)} then exit repeat" if limit else ""
    selector = f"every person {filter_clause}".strip()
    return f'''
        on joinValues(itemsList, separatorText)
            set AppleScript's text item delimiters to separatorText
            set joinedText to itemsList as text
            set AppleScript's text item delimiters to ""
            return joinedText
        end joinValues

        on cleanText(v)
            if v is missing value then return ""
            set t to (v as text)
            set t to my replaceText(return, " ", t)
            set t to my replaceText(linefeed, " ", t)
            set t to my replaceText(tab, " ", t)
            return t
        end cleanText

        on replaceText(findText, replaceText, sourceText)
            set AppleScript's text item delimiters to findText
            set textItems to every text item of sourceText
            set AppleScript's text item delimiters to replaceText
            set sourceText to textItems as text
            set AppleScript's text item delimiters to ""
            return sourceText
        end replaceText

        on birthdayText(p)
            try
                set b to birthday of p
                if b is missing value then return ""
                set y to text -4 thru -1 of ("0000" & (year of b as integer))
                set m to text -2 thru -1 of ("00" & (month of b as integer))
                set d to text -2 thru -1 of ("00" & (day of b as integer))
                return y & "-" & m & "-" & d
            on error
                return ""
            end try
        end birthdayText

        set rowSep to "{_ROW_SEP}"
        set fieldSep to "{_FIELD_SEP}"
        set listSep to "{_LIST_SEP}"
        set outputRows to {{}}
        tell application "Contacts" to launch
        tell application "Contacts"
            set theMatches to {selector}
            repeat with i from 1 to (count of theMatches)
                {limit_clause if limit_clause else '-- no limit'}
                set p to item i of theMatches
                set thePhones to {{}}
                repeat with ph in (phones of p)
                    copy my cleanText(value of ph) to end of thePhones
                end repeat
                set theEmails to {{}}
                repeat with em in (emails of p)
                    copy my cleanText(value of em) to end of theEmails
                end repeat
                set rowFields to {{}}
                copy my cleanText(name of p) to end of rowFields
                copy my cleanText(first name of p) to end of rowFields
                copy my cleanText(last name of p) to end of rowFields
                copy my joinValues(thePhones, listSep) to end of rowFields
                copy my joinValues(theEmails, listSep) to end of rowFields
                copy my cleanText(organization of p) to end of rowFields
                copy my birthdayText(p) to end of rowFields
                copy my cleanText(note of p) to end of rowFields
                set rowText to my joinValues(rowFields, fieldSep)
                copy rowText to end of outputRows
            end repeat
        end tell
        return my joinValues(outputRows, rowSep)
    '''


def _parse_rows(raw: str) -> list[Contact]:
    out: list[Contact] = []
    for row in raw.split(_ROW_SEP):
        if not row.strip():
            continue
        parts = row.split(_FIELD_SEP)
        while len(parts) < 8:
            parts.append("")
        full_name, first, last, phones, emails, org, birthday_raw, notes = parts[:8]
        organization = _clean_text(org)
        out.append(
            Contact(
                full_name=_clean_text(full_name) or "Unknown",
                first_name=_clean_text(first),
                last_name=_clean_text(last),
                phones=[p for p in phones.split(_LIST_SEP) if p.strip()],
                emails=[e for e in emails.split(_LIST_SEP) if e.strip()],
                organization=organization,
                parsed_tags=_parse_tags(organization),
                birthday=_parse_birthday(birthday_raw),
                notes=_clean_text(notes),
            )
        )
    return out


def _write_snapshot(contacts: list[Contact]) -> None:
    path = _snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = []
    for contact in contacts:
        item = asdict(contact)
        item["birthday"] = contact.birthday.isoformat() if contact.birthday else None
        payload.append(item)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_contacts_snapshot() -> list[Contact]:
    path = _snapshot_path()
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    contacts: list[Contact] = []
    for item in payload:
        contacts.append(
            Contact(
                full_name=str(item.get("full_name") or "Unknown"),
                first_name=item.get("first_name"),
                last_name=item.get("last_name"),
                phones=list(item.get("phones") or []),
                emails=list(item.get("emails") or []),
                organization=item.get("organization"),
                parsed_tags=list(item.get("parsed_tags") or []),
                birthday=_parse_birthday(item.get("birthday") or ""),
                notes=item.get("notes"),
            )
        )
    return contacts


def _all_handles(contact: Contact) -> list[str]:
    seen: list[str] = []
    for handle in [*contact.phones, *contact.emails]:
        cleaned = handle.strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


def list_all_contacts() -> list[Contact]:
    global _CONTACT_CACHE
    _ensure_contacts_running()
    contacts = _parse_rows(_run_osa(_contact_script(), timeout=300.0))
    _write_snapshot(contacts)
    _CONTACT_CACHE = (datetime.now(UTC), contacts)
    return contacts


def _cached_contacts() -> list[Contact]:
    """
    Return cached contacts. In SNAPSHOT-ONLY mode (default for background
    callers), this returns whatever is in the on-disk snapshot — it will
    NEVER trigger live AppleScript even if the in-memory cache has expired.
    Live refresh only happens when the operator explicitly runs
    `agent contacts-import` (which calls allow_live_contacts() first).
    """
    global _CONTACT_CACHE
    if _CONTACT_CACHE is not None:
        cached_at, contacts = _CONTACT_CACHE
        if (datetime.now(UTC) - cached_at).total_seconds() <= _CONTACT_CACHE_TTL_SECONDS:
            return contacts
    if not _live_contacts_allowed():
        # Fall back to disk snapshot — no AppleScript, no Contacts.app launch.
        snapshot = load_contacts_snapshot()
        _CONTACT_CACHE = (datetime.now(UTC), snapshot)
        return snapshot
    contacts = list_all_contacts()
    _CONTACT_CACHE = (datetime.now(UTC), contacts)
    return contacts


def search_by_name(query: str, *, limit: int = 10) -> list[Contact]:
    # Snapshot-only fallback when live access isn't allowed — never opens
    # Contacts.app from a background loop.
    if not _live_contacts_allowed():
        needle = query.strip().lower()
        if not needle:
            return []
        return [c for c in load_contacts_snapshot() if needle in c.full_name.lower()][:limit]
    _ensure_contacts_running()
    safe = query.replace('"', '\\"')
    filter_clause = f'whose name contains "{safe}"'
    raw = _run_osa(_contact_script(filter_clause=filter_clause, limit=limit))
    return _parse_rows(raw)


def lookup_by_phone(phone: str) -> Contact | None:
    """
    Snapshot-first lookup. Critically, DOES NOT fall through to a live
    AppleScript Contacts query if the snapshot misses — that fallback was
    the bug that hammered Contacts.app on every inbound poll for unknown
    IG/FB handles. Refresh the snapshot manually via `agent contacts-import`.
    """
    target = _normalize_phone(phone)
    if not target:
        return None
    digits_only = target.lstrip("+")
    snapshot_match = lookup_in_snapshot(phone)
    if snapshot_match is not None:
        return snapshot_match
    # Only fall through to the live (cached) contact list when the operator
    # has explicitly opted in for live access. Background callers stop here.
    if not _live_contacts_allowed():
        return None
    for contact in _cached_contacts():
        for candidate in contact.phones:
            if _normalize_phone(candidate).endswith(digits_only[-10:]):
                return contact
    return None


def lookup_in_snapshot(handle: str, contacts: list[Contact] | None = None) -> Contact | None:
    query = handle.strip()
    if not query:
        return None
    contacts = contacts if contacts is not None else load_contacts_snapshot()
    if "@" in query:
        lowered = query.lower()
        for contact in contacts:
            if any(email.strip().lower() == lowered for email in contact.emails):
                return contact
        return None
    normalized = _normalize_phone(query).lstrip("+")
    if not normalized:
        return None
    for contact in contacts:
        for candidate in contact.phones:
            if _normalize_phone(candidate).lstrip("+").endswith(normalized[-10:]):
                return contact
    return None


def import_contacts(settings=None) -> dict[str, int]:
    from agent.channels.dispatcher import infer_channels_from_handles
    from agent.models import PersonRecord
    from agent.scoring import auto_assign_tiers
    from agent.store import get_person_by_handle, load_store, save_store, store_path, upsert_person

    if settings is None:
        from agent.config import get_settings

        settings = get_settings()
    contacts = list_all_contacts()
    path = store_path(settings)
    store = load_store(path)
    matched_existing = 0
    new_records = 0

    for contact in contacts:
        handles = _all_handles(contact)
        matched = None
        for handle in handles:
            matched = get_person_by_handle(store, handle)
            if matched is not None:
                break
        if matched is not None:
            matched_existing += 1
            matched.display_name = matched.display_name or contact.full_name
            matched.first_name = matched.first_name or contact.first_name
            matched.last_name = matched.last_name or contact.last_name
            matched.inferred_name = matched.inferred_name or contact.first_name or contact.full_name
            matched.contact_organization = contact.organization or matched.contact_organization
            if contact.parsed_tags:
                merged = list(matched.contact_tags)
                for tag in contact.parsed_tags:
                    if tag not in merged:
                        merged.append(tag)
                matched.contact_tags = merged
            matched.birthday = matched.birthday or contact.birthday
            if contact.notes and not matched.notes:
                matched.notes = contact.notes
            for handle in handles:
                if handle not in matched.handles:
                    matched.handles.append(handle)
            matched.connected_channels = infer_channels_from_handles(matched.handles)
            matched.source = "imessage+contacts" if matched.source != "contacts_only" else "contacts_only"
            if matched.channels:
                matched.source = "imessage+contacts"
            upsert_person(store, matched)
            continue

        new_records += 1
        anchor = handles[0] if handles else (contact.full_name or "unknown")
        slug = re.sub(r"[^a-z0-9]+", "-", anchor.lower()).strip("-") or "unknown"
        person = PersonRecord(
            person_id=f"contacts:{slug}",
            display_name=contact.full_name,
            first_name=contact.first_name,
            last_name=contact.last_name,
            inferred_name=contact.first_name or contact.full_name,
            contact_organization=contact.organization,
            contact_tags=list(contact.parsed_tags),
            birthday=contact.birthday,
            notes=contact.notes,
            handles=handles,
            connected_channels=infer_channels_from_handles(handles),
            tier="T4",
            source="contacts_only",
            created_at=datetime.now(UTC).isoformat(),
        )
        upsert_person(store, person)

    auto_assign_tiers(store)
    save_store(path, store)
    return {
        "contacts_found": len(contacts),
        "matched_existing": matched_existing,
        "new_records_created": new_records,
        "total_people_now": len(store.people),
    }


def health_check() -> tuple[bool, str]:
    if not _OSASCRIPT:
        return False, "osascript not found (not on macOS?)"
    try:
        out = _run_osa('tell application "Contacts" to return count of every person')
        n = int(out.strip())
        return True, f"Contacts.app accessible, {n:,} people indexed"
    except Exception as e:
        msg = str(e).lower()
        if "not authorized" in msg or "permission" in msg:
            return False, "Contacts permission required"
        return False, f"Contacts error: {e}"
