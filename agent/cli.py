from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from agent.channels import available_channels, get_channel
from agent.channels.dispatcher import handle_for_channel
from agent.config import get_settings
from agent.contacts_reader import import_contacts
from agent.ingest import (
    collect_store_audit_stats,
    enrich_people,
    merge_duplicate_people,
    reclassify_people_deterministically,
    sync_imessage_threads,
    verify_contact_names,
)
from agent.scoring import auto_assign_tiers
from agent.models import PersonRecord
from agent.ops import collect_health
from agent.person_utils import display_name
from agent.store import (
    decrypt_store_to_dict,
    decrypt_store_to_text,
    load_store,
    store_path,
    store_transaction,
    write_encrypted_store_from_plaintext,
)
from agent.web import ACTION_DEFAULT_MAX_TARGETS, ask_rolodex_action, onboarding_progress_snapshot


def _rolodex_path() -> Path:
    return store_path(get_settings())


def _require_confirmation(action: str) -> None:
    if os.getenv("ROLODEX_ALLOW_SCRIPTED_DECRYPT") == "1":
        return
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise SystemExit(
            f"Refusing to {action} outside an interactive TTY. "
            "Set ROLODEX_ALLOW_SCRIPTED_DECRYPT=1 to override."
        )
    prompt = f"Type '{action}' to continue: "
    if input(prompt).strip() != action:
        raise SystemExit("Confirmation failed.")


def _write_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _cmd_decrypt(args: argparse.Namespace) -> int:
    _require_confirmation("decrypt")
    plaintext = decrypt_store_to_text(_rolodex_path())
    if args.stdout:
        sys.stdout.write(plaintext)
        if not plaintext.endswith("\n"):
            sys.stdout.write("\n")
        return 0
    if args.output:
        output = Path(args.output)
    else:
        fd, raw_path = tempfile.mkstemp(prefix="rolodex-", suffix=".json")
        os.close(fd)
        output = Path(raw_path)
    _write_private_text(output, plaintext)
    print(output)
    return 0


def _cmd_reencrypt(args: argparse.Namespace) -> int:
    _require_confirmation("reencrypt")
    plaintext_path = Path(args.from_path)
    store = write_encrypted_store_from_plaintext(
        _rolodex_path(),
        plaintext_path.read_text(encoding="utf-8"),
    )
    print(f"Re-encrypted {len(store.people)} people into {_rolodex_path().with_suffix('.json.enc')}")
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    _require_confirmation("inspect")
    data = decrypt_store_to_dict(_rolodex_path())
    for raw in data.get("people", []):
        person = PersonRecord.model_validate(raw)
        if person.person_id == args.person:
            print(json.dumps(person.model_dump(), indent=2))
            return 0
    raise SystemExit(f"Person not found: {args.person}")


def _color(ok: bool, text: str) -> str:
    return f"\033[{32 if ok else 31}m{text}\033[0m"


def _resolve_person(path: Path, query: str) -> PersonRecord:
    store = load_store(path)
    query_lower = query.strip().lower()
    digits = "".join(ch for ch in query if ch.isdigit())
    for person in store.people:
        if person.person_id == query:
            return person
        if digits and any("".join(ch for ch in handle if ch.isdigit()) == digits for handle in person.handles):
            return person
        if query_lower and query_lower in display_name(person).lower():
            return person
    raise SystemExit(f"Person not found: {query}")


def _mutate_person(path: Path, person_id: str, mutator) -> PersonRecord:
    with store_transaction(path) as store:
        for person in store.people:
            if person.person_id != person_id:
                continue
            mutator(person)
            person.user_marked_at = datetime.now(UTC)
            return person
    raise SystemExit(f"Person not found: {person_id}")


def _print_year_histogram(stats: dict[str, object]) -> None:
    histogram = dict(stats.get("year_histogram", {}))
    print("Last-message years:")
    if not histogram:
        print("- none")
        return
    for year, count in histogram.items():
        print(f"- {year}: {count}")


def _print_audit_summary(stats: dict[str, object]) -> None:
    _print_year_histogram(stats)
    print(f"Recent messages: {stats['with_recent_messages']} with messages, {stats['zero_recent_messages']} empty")
    print(f"Classification: {stats['classified_people']} classified, {stats['unclassified_people']} unclassified")
    print("Top active people:")
    top_people = list(stats.get("top_active_people", []))
    if not top_people:
        print("- none")
        return
    for item in top_people:
        print(
            f"- {item['display_name']} | messages={item['message_count']} | "
            f"last_year={item['last_message_year']} | class={item['relationship_class']}"
        )


def _cmd_status(_args: argparse.Namespace) -> int:
    health = collect_health(get_settings())
    data = health.model_dump()
    store = load_store(_rolodex_path())
    stats = collect_store_audit_stats(store)
    print("Rolodex status")
    print(f"People: {data['person_count']}")
    print(f"Last sync: {data['last_sync_at'] or '-'}")
    print(f"Last digest: {data['last_digest_at'] or '-'}")
    print(f"Sends today: {data['sends_today']}/{data['cap']}")
    print(f"Encrypted store: {_color(data['encrypted_store_present'], str(data['encrypted_store_present']))}")
    print(f"Keychain accessible: {_color(data['keychain_accessible'], str(data['keychain_accessible']))}")
    print(f"iMessage DB accessible: {_color(data['imessage_db_accessible'], str(data['imessage_db_accessible']))}")
    print(f"Twilio configured: {_color(data['twilio_configured'], str(data['twilio_configured']))}")
    print("Recent errors:")
    if not data["recent_errors"]:
        print("- none")
    else:
        for item in data["recent_errors"]:
            print(f"- {item}")
    _print_year_histogram(stats)
    return 0


def _cmd_onboarding_status(_args: argparse.Namespace) -> int:
    progress = onboarding_progress_snapshot(get_settings())
    print("Rolodex onboarding-status")
    print(f"Reviewed: {progress['reviewed']}/{progress['total']} ({progress['percent']}%)")
    print("Remaining priority segments:")
    for key, count in dict(progress["remaining_priority_segments"]).items():
        print(f"- {key}: {count}")
    print("Reviewed breakdown by class:")
    breakdown = dict(progress.get("breakdown_by_class", {}))
    if not breakdown:
        print("- none")
    else:
        for key, count in breakdown.items():
            print(f"- {key}: {count}")
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    max_threads = None if int(args.max_threads) <= 0 else int(args.max_threads)
    max_messages = None if int(args.max_messages_per_thread) <= 0 else int(args.max_messages_per_thread)
    report = sync_imessage_threads(
        settings=get_settings(),
        max_threads=max_threads,
        max_messages_per_thread=max_messages,
    )
    if args.enrich:
        subprocess.Popen(
            [sys.executable, "-m", "agent", "enrich"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    print("Rolodex sync")
    print(f"Scanned threads: {report.scanned_threads}")
    print(f"Created people: {report.created_people}")
    print(f"Updated people: {report.updated_people}")
    print(f"Total people: {report.total_people}")
    print(f"Skipped group threads: {report.skipped_group_threads}")
    print(f"Tagged group threads: {report.tagged_group_threads}")
    print(f"Enrichment: {'started in background' if args.enrich else 'disabled'}")
    print(f"Store path: {report.store_path or '-'}")
    if report.warnings:
        print("Warnings:")
        for warning in report.warnings:
            print(f"- {warning}")
    else:
        print("Warnings:")
        print("- none")
    return 0


def _cmd_enrich(args: argparse.Namespace) -> int:
    result = enrich_people(
        settings=get_settings(),
        missing_only=not args.force_reclassify,
        force_reclassify=bool(args.force_reclassify),
    )
    print("Rolodex enrich")
    print(f"Processed people: {result['processed']}")
    print(f"Updated people: {result['updated']}")
    warnings = list(result["warnings"])
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"- {warning}")
    else:
        print("Warnings:")
        print("- none")
    return 0


def _cmd_reclassify_deterministic(_args: argparse.Namespace) -> int:
    result = reclassify_people_deterministically(settings=get_settings())
    print("Rolodex reclassify-deterministic")
    print(f"Updated people: {result['updated']}")
    print(f"Preserved user overrides: {result['preserved_overrides']}")
    print("Applied by rule:")
    if result["applied_by_rule"]:
        for rule_id, count in dict(result["applied_by_rule"]).items():
            print(f"- {rule_id}: {count}")
    else:
        print("- none")
    print("Bucket counts before:")
    for label, count in dict(result["before_counts"]).items():
        print(f"- {label}: {count}")
    print("Bucket counts after:")
    for label, count in dict(result["after_counts"]).items():
        print(f"- {label}: {count}")
    return 0


def _cmd_merge_duplicates(_args: argparse.Namespace) -> int:
    result = merge_duplicate_people(settings=get_settings())
    print("Rolodex merge-duplicates")
    print(f"People before: {result['before']}")
    print(f"People after: {result['after']}")
    print(f"Records merged: {result['merged']}")
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    del args
    store = load_store(_rolodex_path())
    stats = collect_store_audit_stats(store)
    print("Rolodex audit")
    _print_audit_summary(stats)
    return 0


def _cmd_resync(_args: argparse.Namespace) -> int:
    settings = get_settings()
    path = _rolodex_path()
    with store_transaction(path) as store:
        for person in store.people:
            person.recent_messages = []
    report = sync_imessage_threads(
        settings=settings,
        max_threads=None,
        max_messages_per_thread=None,
        enrich=False,
    )
    result = enrich_people(
        settings=settings,
        missing_only=False,
        force_reclassify=True,
    )
    stats = collect_store_audit_stats(load_store(path))
    print("Rolodex resync")
    print(f"Scanned threads: {report.scanned_threads}")
    print(f"Created people: {report.created_people}")
    print(f"Updated people: {report.updated_people}")
    print(f"Total people: {report.total_people}")
    print(f"Enriched people: {result['updated']}/{result['processed']}")
    _print_year_histogram(stats)
    if report.warnings or result["warnings"]:
        print("Warnings:")
        for warning in [*report.warnings, *list(result["warnings"])]:
            print(f"- {warning}")
    else:
        print("Warnings:")
        print("- none")
    return 0


def _cmd_digest(args: argparse.Namespace) -> int:
    import asyncio
    import os

    from agent.daemon import daily_run

    previous = os.environ.get("ROLODEX_DRY_RUN")
    if args.dry_run:
        os.environ["ROLODEX_DRY_RUN"] = "1"
    try:
        asyncio.run(daily_run(dry_run=args.dry_run, limit=int(args.limit)))
    finally:
        if args.dry_run:
            if previous is None:
                os.environ.pop("ROLODEX_DRY_RUN", None)
            else:
                os.environ["ROLODEX_DRY_RUN"] = previous
    return 0


def _cmd_poll_inbound(args: argparse.Namespace) -> int:
    import asyncio

    from agent.inbound_poller import format_report, poll_all_channels

    report = asyncio.run(poll_all_channels(dry_run=bool(args.dry_run)))
    print(format_report(report))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from agent.web import serve

    serve(host=str(args.host), port=int(args.port), open_browser=not args.no_open)
    return 0


def _cmd_contacts_import(_args: argparse.Namespace) -> int:
    # Explicitly opt in for live Contacts.app access — this is the ONLY
    # CLI entry point where AppleScript is allowed to touch Contacts.
    # Background callers (inbound poller, daily digest, etc.) never get
    # to flip this gate; they stay snapshot-only.
    from agent.contacts_reader import allow_live_contacts
    allow_live_contacts()
    result = import_contacts(settings=get_settings())
    print("Rolodex contacts import")
    print(f"Contacts found: {result['contacts_found']}")
    print(f"Matched existing people: {result['matched_existing']}")
    print(f"New records created: {result['new_records_created']}")
    print(f"Total people now: {result['total_people_now']}")
    return 0


def _cmd_verify_names(_args: argparse.Namespace) -> int:
    result = verify_contact_names(settings=get_settings())
    print("Rolodex verify-names")
    print(f"People checked: {result['checked']}")
    print(f"Names filled in: {result['updated']}")
    return 0


def _cmd_retier(_args: argparse.Namespace) -> int:
    path = _rolodex_path()
    with store_transaction(path) as store:
        counts = auto_assign_tiers(store)
    print("Rolodex retier")
    for label in ("T1", "T2", "T3", "T4", "T5"):
        print(f"{label}: {counts.get(label, 0)}")
    return 0


def _cmd_note(args: argparse.Namespace) -> int:
    path = _rolodex_path()
    person = _resolve_person(path, args.person)
    updated = _mutate_person(path, person.person_id, lambda p: setattr(p, "user_note", args.note))
    print(f"Saved note for {display_name(updated)}")
    return 0


def _cmd_override(args: argparse.Namespace) -> int:
    updated = _mutate_person(_rolodex_path(), args.person_id, lambda p: setattr(p, "user_override_class", args.override_class))
    print(f"Override class set for {display_name(updated)}: {args.override_class}")
    return 0


def _cmd_deprioritize(args: argparse.Namespace) -> int:
    updated = _mutate_person(_rolodex_path(), args.person_id, lambda p: setattr(p, "user_priority_boost", -100))
    print(f"Deprioritized {display_name(updated)}")
    return 0


def _cmd_boost(args: argparse.Namespace) -> int:
    updated = _mutate_person(_rolodex_path(), args.person_id, lambda p: setattr(p, "user_priority_boost", int(args.by)))
    print(f"Priority boost set for {display_name(updated)}: {args.by}")
    return 0


def _cmd_dnc(args: argparse.Namespace) -> int:
    updated = _mutate_person(_rolodex_path(), args.person_id, lambda p: setattr(p, "do_not_contact", True))
    print(f"Do-not-contact set for {display_name(updated)}")
    return 0


def _cmd_undnc(args: argparse.Namespace) -> int:
    updated = _mutate_person(_rolodex_path(), args.person_id, lambda p: setattr(p, "do_not_contact", False))
    print(f"Do-not-contact cleared for {display_name(updated)}")
    return 0


def _cmd_channels(_args: argparse.Namespace) -> int:
    table = Table(title="Rolodex Channels")
    table.add_column("Channel")
    table.add_column("Configured")
    table.add_column("Healthy")
    table.add_column("Detail")
    for name in available_channels():
        health = get_channel(name).health_check()
        table.add_row(
            name,
            "yes" if health.configured else "no",
            "yes" if health.healthy else "no",
            health.detail or "-",
        )
    Console().print(table)
    return 0


def _cmd_send_test(args: argparse.Namespace) -> int:
    if os.environ.get("ROLODEX_DRY_RUN") == "1":
        raise SystemExit("Refusing to send while ROLODEX_DRY_RUN=1")
    result = get_channel(args.channel).send(args.handle, args.text)
    if not result.ok:
        raise SystemExit(result.error or "send failed")
    print(f"Sent via {result.channel} to {result.handle}")
    return 0


def _cmd_ask_action(args: argparse.Namespace) -> int:
    payload = ask_rolodex_action(
        args.instruction,
        dry_run=not args.execute,
        max_targets=int(args.max_targets),
        settings=get_settings(),
    )
    print(json.dumps(payload, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rolodex")
    sub = parser.add_subparsers(dest="command", required=True)

    decrypt = sub.add_parser("decrypt")
    decrypt.add_argument("--output")
    decrypt.add_argument("--stdout", action="store_true")
    decrypt.set_defaults(func=_cmd_decrypt)

    reencrypt = sub.add_parser("reencrypt")
    reencrypt.add_argument("--from", dest="from_path", required=True)
    reencrypt.set_defaults(func=_cmd_reencrypt)

    inspect = sub.add_parser("inspect")
    inspect.add_argument("--person", required=True)
    inspect.set_defaults(func=_cmd_inspect)

    sync = sub.add_parser("sync")
    sync.add_argument("--max-threads", type=int, default=0)
    sync.add_argument("--max-messages-per-thread", type=int, default=0)
    sync.add_argument("--enrich", dest="enrich", action="store_true", default=True)
    sync.add_argument("--no-enrich", dest="enrich", action="store_false")
    sync.set_defaults(func=_cmd_sync)

    enrich = sub.add_parser("enrich")
    enrich.add_argument("--force-reclassify", action="store_true")
    enrich.set_defaults(func=_cmd_enrich)

    reclassify_deterministic = sub.add_parser("reclassify-deterministic")
    reclassify_deterministic.set_defaults(func=_cmd_reclassify_deterministic)

    merge_duplicates = sub.add_parser("merge-duplicates")
    merge_duplicates.set_defaults(func=_cmd_merge_duplicates)

    status = sub.add_parser("status")
    status.set_defaults(func=_cmd_status)

    onboarding_status = sub.add_parser("onboarding-status")
    onboarding_status.set_defaults(func=_cmd_onboarding_status)

    audit = sub.add_parser("audit")
    audit.set_defaults(func=_cmd_audit)

    resync = sub.add_parser("resync")
    resync.set_defaults(func=_cmd_resync)

    digest = sub.add_parser("digest")
    digest.add_argument("--dry-run", action="store_true")
    digest.add_argument("--limit", type=int, default=5)
    digest.set_defaults(func=_cmd_digest)

    poll_inbound = sub.add_parser("poll-inbound")
    poll_inbound.add_argument("--dry-run", action="store_true")
    poll_inbound.set_defaults(func=_cmd_poll_inbound)

    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-open", action="store_true")
    serve.set_defaults(func=_cmd_serve)

    contacts = sub.add_parser("contacts")
    contacts_sub = contacts.add_subparsers(dest="contacts_command", required=True)
    contacts_import = contacts_sub.add_parser("import")
    contacts_import.set_defaults(func=_cmd_contacts_import)

    verify_names = sub.add_parser("verify-names")
    verify_names.set_defaults(func=_cmd_verify_names)

    retier = sub.add_parser("retier")
    retier.set_defaults(func=_cmd_retier)

    note = sub.add_parser("note")
    note.add_argument("person")
    note.add_argument("note")
    note.set_defaults(func=_cmd_note)

    override = sub.add_parser("override")
    override.add_argument("person_id")
    override.add_argument("--class", dest="override_class", required=True)
    override.set_defaults(func=_cmd_override)

    deprioritize = sub.add_parser("deprioritize")
    deprioritize.add_argument("person_id")
    deprioritize.set_defaults(func=_cmd_deprioritize)

    boost = sub.add_parser("boost")
    boost.add_argument("person_id")
    boost.add_argument("--by", type=int, required=True)
    boost.set_defaults(func=_cmd_boost)

    dnc = sub.add_parser("dnc")
    dnc.add_argument("person_id")
    dnc.set_defaults(func=_cmd_dnc)

    undnc = sub.add_parser("un-dnc")
    undnc.add_argument("person_id")
    undnc.set_defaults(func=_cmd_undnc)

    channels = sub.add_parser("channels")
    channels.set_defaults(func=_cmd_channels)

    send_test = sub.add_parser("send-test")
    send_test.add_argument("channel")
    send_test.add_argument("handle")
    send_test.add_argument("text")
    send_test.set_defaults(func=_cmd_send_test)

    ask_action = sub.add_parser("ask-action")
    ask_action.add_argument("instruction")
    ask_action.add_argument("--max-targets", type=int, default=ACTION_DEFAULT_MAX_TARGETS)
    ask_action.add_argument("--execute", action="store_true")
    ask_action.set_defaults(func=_cmd_ask_action)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
