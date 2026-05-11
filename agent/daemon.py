from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

from agent.config import get_settings
from agent.digest import archive_digest_to_brain, select_daily_candidates
from agent.draft import generate_draft
from agent.imessage_sender import SendUnavailable, send_with_fallback
from agent.ingest import sync_imessage_threads
from agent.llm_client import classify as llm_classify
from agent.models import DigestCandidate, HistoryEntry, PersonRecord
from agent.ops import append_audit_entry, push_recent_error
from agent.store import load_store, save_store, store_path
from agent.telegram_bot import run_callback_listener, send_digest, send_simple


def _today_key() -> str:
    return datetime.now(UTC).date().isoformat()


def _draft_key(run_id: str, person_id: str) -> str:
    return f"{run_id}:{person_id}"


def _find_person(store, person_id: str) -> PersonRecord:
    for person in store.people:
        if person.person_id == person_id:
            return person
    raise KeyError(f"Unknown person_id: {person_id}")


def _find_candidate(store, run_id: str, person_id: str) -> DigestCandidate:
    for candidate in store.digests.get(run_id, []):
        if candidate.person_id == person_id:
            return candidate
    raise KeyError(f"No digest candidate for {person_id} in {run_id}")


def _preferred_handle(person: PersonRecord) -> str:
    for channel in person.channels:
        if channel.active and channel.handle:
            return channel.handle
    if person.handles:
        return person.handles[0]
    raise ValueError(f"{person.person_id} has no sendable handle")


def _record_event(person: PersonRecord, *, kind: str, run_id: str, message: str | None = None, status: str | None = None, message_id: str | None = None, channel_used: str | None = None) -> None:
    person.history.log.append(
        HistoryEntry(
            at=datetime.now(UTC).isoformat(),
            kind=kind,
            message=message,
            run_id=run_id,
            status=status,
            message_id=message_id,
            channel_used=channel_used,
        )
    )


def _operator_name() -> str:
    raw = (os.environ.get("ROLODEX_OPERATOR_NAME") or "Aaron").strip()
    return raw or "Aaron"


def _render_morning_digest(candidates: list[DigestCandidate]) -> str:
    greeting = f"Good morning {_operator_name()}. "
    if not candidates:
        return greeting + "No reconnects are due today."
    lines = [greeting + f"Here are your {len(candidates)} reconnects today:", ""]
    for idx, candidate in enumerate(candidates, start=1):
        lines.append(f"{idx}. {candidate.display_name}")
        lines.append(f"   Suggested draft: {candidate.draft_preview or 'Draft unavailable'}")
    return "\n".join(lines)


async def daily_run(*, dry_run: bool | None = None, limit: int = 5):
    """Canonical morning routine: sync, pick five, draft, deliver to Telegram, archive."""
    settings = get_settings()
    path = store_path(settings)
    store = load_store(path)
    sync_imessage_threads(
        store=store,
        settings=settings,
        enrich=True,
        sensitive_classifier=lambda prompt: llm_classify(
            prompt, labels=["NONE", "MEDICAL", "LEGAL", "MENTAL_HEALTH", "SEXUAL", "GRIEF", "CONFLICT"]
        ),
    )
    candidates = await select_daily_candidates(store, settings, limit=limit)
    now_iso = datetime.now(UTC).isoformat()
    for candidate in candidates:
        person = _find_person(store, candidate.person_id)
        bundle = await generate_draft(person, candidate.reason, None)
        bundle.run_id = candidate.run_id
        candidate.draft_preview = bundle.top_draft
        store.drafts[_draft_key(candidate.run_id, candidate.person_id)] = bundle
    is_dry_run = bool(dry_run) or os.environ.get("ROLODEX_DRY_RUN") == "1"
    if candidates:
        for candidate in candidates:
            person = _find_person(store, candidate.person_id)
            person.cadence.last_digest_run_id = candidate.run_id
            if not is_dry_run:
                person.cadence.last_digest_at = now_iso
        store.digests = {**store.digests, candidates[0].run_id: candidates}
    if not is_dry_run:
        store.last_digest_at = now_iso
    save_store(path, store)

    text = _render_morning_digest(candidates)
    if is_dry_run:
        print(text)
    else:
        await send_digest(text, candidates=[c.model_dump() for c in candidates])
        archive_digest_to_brain(candidates, run_at=datetime.now(UTC))
    return candidates


async def on_telegram_callback(action, person_id, run_id) -> str:
    """Handle button taps from the digest."""
    settings = get_settings()
    path = store_path(settings)
    store = load_store(path)
    person = _find_person(store, person_id)
    candidate = _find_candidate(store, run_id, person_id)
    bundle = store.drafts.get(_draft_key(run_id, person_id))
    now = datetime.now(UTC)

    if action == "send":
        cap = max(1, int(settings.rolodex_daily_send_cap or 5))
        sends_today = int(store.daily_sends.get(_today_key(), 0) or 0)
        if sends_today >= cap:
            return f"Daily cap reached ({sends_today}/{cap})"
        if bundle is None:
            bundle = await generate_draft(person, candidate.reason, None)
            bundle.run_id = run_id
            store.drafts[_draft_key(run_id, person_id)] = bundle
        try:
            receipt = send_with_fallback(_preferred_handle(person), bundle.top_draft)
        except SendUnavailable as exc:
            push_recent_error(store, str(exc))
            save_store(path, store)
            return str(exc)
        candidate.status = "sent"
        person.cadence.last_sent_at = now.isoformat()
        person.last_contacted = now.isoformat()
        person.history.total_contacts_initiated_by_me += 1
        _record_event(
            person,
            kind="send",
            run_id=run_id,
            message=bundle.top_draft,
            status="sent",
            message_id=receipt.provider_id,
            channel_used=receipt.channel,
        )
        store.daily_sends[_today_key()] = sends_today + 1
        append_audit_entry(
            settings,
            {"ts": now.isoformat(), "action": "send", "person_id": person_id, "run_id": run_id, "details": {"channel": receipt.channel}},
        )
        save_store(path, store)
        return "Sent ✓"
    if action == "skip":
        candidate.status = "skipped"
        person.cadence.last_skipped_run_id = run_id
        _record_event(person, kind="skip", run_id=run_id, status="skipped")
        append_audit_entry(
            settings,
            {"ts": now.isoformat(), "action": "skip", "person_id": person_id, "run_id": run_id, "details": {}},
        )
        save_store(path, store)
        return "Skipped"
    if action == "snooze":
        candidate.status = "snoozed"
        person.cadence.snooze_until = (now + timedelta(days=7)).isoformat()
        _record_event(person, kind="snooze", run_id=run_id, status="snoozed")
        append_audit_entry(
            settings,
            {"ts": now.isoformat(), "action": "snooze", "person_id": person_id, "run_id": run_id, "details": {"days": 7}},
        )
        save_store(path, store)
        return "Snoozed 7d"
    if action == "edit":
        if bundle:
            await send_simple(f"Reply with your edited version for {person.display_name or person.person_id}.\nCurrent draft:\n{bundle.top_draft}")
        return "Editing — type your version"
    return f"Unknown action: {action}"


async def _run_daemon() -> None:
    from agent.scheduler import RolodexScheduler

    sched = RolodexScheduler(run_callback=daily_run)
    sched.start()
    await run_callback_listener(on_telegram_callback)


def main():
    asyncio.run(_run_daemon())
