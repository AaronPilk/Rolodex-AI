"""
Rolodex AI — the agent package.

Standalone version. Does not depend on any other system.

Modules in this package:
- imessage_reader: read iMessage history from chat.db
- contacts_reader: read macOS Contacts
- llm_client: Anthropic API wrapper (drafts, classification)
- imessage_sender: send iMessages via AppleScript with Twilio SMS fallback
- telegram_bot: daily digest delivery + inline approval callbacks
- scheduler: APScheduler-based 9am daily trigger

Modules copied from the original PILK build (Codex migration step):
- models: Pydantic data models (PersonRecord, ToneProfile, etc.)
- store: encrypted local store (rolodex.json.enc)
- scoring: cadence + priority + natural-end suppression
- tonality: per-person stylometric fingerprinting
- draft: LLM-powered draft generation
- digest: daily digest assembly + brain-mirror archive
- ingest: chat.db sync + Contacts enrichment + sensitivity classification
- ops: health checks + audit log + retry decorators
- cli: operator CLI (status / inspect / decrypt / audit)
"""

__version__ = "0.1.0"
