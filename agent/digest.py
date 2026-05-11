from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from agent.config import get_settings
from agent.llm_client import classify as llm_classify
from agent.models import DigestCandidate, RolodexStore
from agent.person_utils import display_name
from agent.scoring import active_relationship_class, classify_natural_end, compute_priority


async def _default_natural_end_llm(*, prompt: str, task_type: str | None = None) -> str:
    del task_type
    label = llm_classify(prompt, labels=["WAITING", "ENDED"])
    score = 0.9 if label == "ENDED" else 0.1
    reason = (
        "LLM classified the conversation as naturally ended."
        if label == "ENDED"
        else "LLM classified the conversation as still waiting for a reply."
    )
    return json.dumps({"score": score, "reason": reason})


async def select_daily_candidates(
    store: RolodexStore,
    profile=None,
    *,
    llm=None,
    limit: int = 5,
    natural_end_threshold: float = 0.7,
) -> list[DigestCandidate]:
    profile = profile or get_settings()
    llm = llm or _default_natural_end_llm
    today = datetime.now(UTC).date()
    candidates: list[DigestCandidate] = []
    run_id = f"rolodex-{datetime.now(UTC).strftime('%Y%m%d')}"
    for person in store.people:
        score = compute_priority(person, profile, today)
        if score is None:
            continue
        if active_relationship_class(person) in {"spam_or_verification", "unknown"}:
            continue
        if person.outbound_message_count <= 0:
            continue
        if (person.inbound_message_count + person.outbound_message_count) < 4:
            continue
        if person.sensitivity_flags:
            continue
        natural_end = await classify_natural_end(person, llm)
        if natural_end.score >= natural_end_threshold:
            continue
        candidates.append(
            DigestCandidate(
                run_id=run_id,
                person_id=person.person_id,
                display_name=display_name(person),
                inferred_name=person.inferred_name,
                relationship_class=active_relationship_class(person),
                reason="cadence-due" if person.cadence.is_overdue else "priority-top",
                priority=score,
                due_days=person.cadence.days_overdue,
            )
        )
    return sorted(candidates, key=lambda item: item.priority, reverse=True)[:limit]


def select_manual_review_candidates(store: RolodexStore, profile, limit: int = 5) -> list[DigestCandidate]:
    today = datetime.now(UTC).date()
    run_id = f"rolodex-{datetime.now(UTC).strftime('%Y%m%d')}"
    manual: list[DigestCandidate] = []
    for person in store.people:
        score = compute_priority(person, profile, today)
        if score is None:
            continue
        if active_relationship_class(person) in {"spam_or_verification", "unknown"}:
            continue
        if not person.sensitivity_flags:
            continue
        manual.append(
            DigestCandidate(
                run_id=run_id,
                person_id=person.person_id,
                display_name=display_name(person),
                inferred_name=person.inferred_name,
                relationship_class=active_relationship_class(person),
                reason=f"manual-review:{','.join(person.sensitivity_flags)}",
                priority=score,
                due_days=person.cadence.days_overdue,
            )
        )
    return sorted(manual, key=lambda item: item.priority, reverse=True)[:limit]


def render_telegram_digest(
    candidates: list[DigestCandidate],
    manual_review_candidates: list[DigestCandidate] | None = None,
) -> str:
    manual_review_candidates = manual_review_candidates or []
    if not candidates and not manual_review_candidates:
        return "Rolodex digest: no candidates due today."
    lines = ["Rolodex digest", ""]
    for idx, candidate in enumerate(candidates, start=1):
        label = candidate.relationship_class or "unknown"
        name = candidate.display_name
        lines.append(
            f"{idx}. {name} [{label}] | {candidate.reason} | priority {candidate.priority:.1f}"
        )
        if candidate.draft_preview:
            lines.append(f"   Draft: {candidate.draft_preview}")
        if candidate.due_days is not None:
            lines.append(f"   Due: {candidate.due_days} day(s)")
    if manual_review_candidates:
        if candidates:
            lines.append("")
        lines.append("Needs your attention manually")
        for candidate in manual_review_candidates:
            label = candidate.relationship_class or "unknown"
            name = candidate.display_name
            lines.append(
                f"- {name} [{label}] | {candidate.reason} | priority {candidate.priority:.1f}"
            )
    return "\n".join(lines)


def render_telegram_digest_markup(
    run_id: str,
    candidates: list[DigestCandidate],
) -> dict[str, list[list[dict[str, str]]]]:
    keyboard: list[list[dict[str, str]]] = []
    for idx, _candidate in enumerate(candidates):
        keyboard.append(
            [
                {
                    "text": "\U0001F4E4 Send",
                    "callback_data": f"rolodex:send:{run_id}:{idx}",
                },
                {
                    "text": "\u270F\uFE0F Edit",
                    "callback_data": f"rolodex:edit:{run_id}:{idx}",
                },
                {
                    "text": "\u23ED\uFE0F Skip",
                    "callback_data": f"rolodex:skip:{run_id}:{idx}",
                },
                {
                    "text": "\U0001F4A4 Snooze",
                    "callback_data": f"rolodex:snooze:{run_id}:{idx}",
                },
            ]
        )
    return {"inline_keyboard": keyboard}


def render_brain_note(
    candidates: list[DigestCandidate],
    run_at: datetime,
    manual_review_candidates: list[DigestCandidate] | None = None,
) -> str:
    manual_review_candidates = manual_review_candidates or []
    lines = [
        f"# Rolodex Digest - {run_at.date().isoformat()}",
        "",
        f"Run at: {run_at.isoformat()}",
        "",
    ]
    if not candidates and not manual_review_candidates:
        lines.append("No candidates due today.")
        return "\n".join(lines)
    for candidate in candidates:
        lines.append(f"## {candidate.display_name}")
        lines.append(f"- person_id: {candidate.person_id}")
        lines.append(f"- reason: {candidate.reason}")
        lines.append(f"- priority: {candidate.priority:.2f}")
        lines.append(f"- due_days: {candidate.due_days or 0}")
        lines.append("")
    if manual_review_candidates:
        lines.append("## Needs your attention manually")
        lines.append("")
        for candidate in manual_review_candidates:
            lines.append(f"- {candidate.display_name}")
            lines.append(f"  reason: {candidate.reason}")
            lines.append(f"  priority: {candidate.priority:.2f}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def archive_digest_to_brain(
    candidates: list[DigestCandidate],
    run_at: datetime,
    manual_review_candidates: list[DigestCandidate] | None = None,
) -> Path:
    settings = get_settings()
    base = settings.brain_vault_path
    if os.getenv("ROLODEX_BRAIN_FLAT", "0") == "1":
        path = base / f"rolodex-{run_at.date().isoformat()}.md"
    else:
        path = base / "rolodex" / "daily" / f"{run_at.date().isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_brain_note(candidates, run_at, manual_review_candidates),
        encoding="utf-8",
    )
    return path
