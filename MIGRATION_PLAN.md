# Rolodex AI — Migration Plan (PILK → Standalone)

> Step-by-step instructions for Codex when the PILK MCP comes back online.
> Goal: extract Rolodex AI as a standalone Python package, leave PILK clean.

---

## 1. Standalone scaffolding (already done in this session)

The following files already live in `/Users/pilksclaes/rolodex AI/` and are ready:

| File | Purpose |
|---|---|
| `pyproject.toml` | Package definition, dependencies, entry points |
| `.env.example` | Required env vars (Anthropic, Telegram, Twilio, schedule) |
| `agent/__init__.py` | Package marker + module roster |
| `agent/imessage_reader.py` | Standalone chat.db reader (replaces PILK's `messages_search_mine`) |
| `agent/contacts_reader.py` | Standalone Contacts reader (replaces PILK's `contacts_search`) |
| `agent/llm_client.py` | Direct Anthropic SDK (replaces PILK's `llm_ask`) |
| `agent/imessage_sender.py` | AppleScript send + Twilio SMS fallback (replaces PILK's `messages_send`) |
| `agent/telegram_bot.py` | Telegram bot for digest + approvals (replaces PILK's `telegram_notify` + approval queue) |
| `agent/scheduler.py` | APScheduler 9am daily trigger (replaces PILK's trigger system) |

These do not depend on PILK. Codex does NOT need to recreate them — just route imports to them.

---

## 2. Codex's job (when PILK MCP is back)

### Step A — Copy portable rolodex modules from PILK to standalone

Source: `/Users/pilksclaes/Pilk Ai/pilk-ai/core/rolodex/`
Destination: `/Users/pilksclaes/rolodex AI/agent/`

Files to copy (all of them):

| Source | Destination | Refactor needed? |
|---|---|---|
| `core/rolodex/models.py` | `agent/models.py` | None — pure Pydantic |
| `core/rolodex/store.py` | `agent/store.py` | None — file I/O + cryptography only |
| `core/rolodex/scoring.py` | `agent/scoring.py` | None — pure logic |
| `core/rolodex/tonality.py` | `agent/tonality.py` | None — pure logic |
| `core/rolodex/draft.py` | `agent/draft.py` | Replace PILK `llm_ask` import with `from agent.llm_client import draft as llm_draft` |
| `core/rolodex/digest.py` | `agent/digest.py` | None expected — verify no PILK imports |
| `core/rolodex/ingest.py` | `agent/ingest.py` | Replace PILK iMessage/Contacts imports with `from agent.imessage_reader import ...` and `from agent.contacts_reader import ...` |
| `core/rolodex/ops.py` | `agent/ops.py` | None — pure logging |
| `core/rolodex/cli.py` | `agent/cli.py` | None expected — verify no PILK imports |
| `core/rolodex/send.py` | `agent/_pilk_send.py` (renamed) | DELETE — replaced by `agent/imessage_sender.py` |

### Step B — Replace `core/tools/builtin/rolodex.py` with `agent/daemon.py`

The PILK file `core/tools/builtin/rolodex.py` (~1,070 lines) is the integration glue
that wires the rolodex into PILK's tool registry, approval queue, and trigger system.

It should NOT be copied. Instead, write `agent/daemon.py` from scratch using the
standalone substitutes already shipped:

```python
# agent/daemon.py — entry point for the standalone Rolodex daemon

import asyncio
from agent.scheduler import RolodexScheduler
from agent.telegram_bot import run_callback_listener, send_digest
from agent.digest import select_daily_candidates, render_telegram_digest, archive_digest_to_brain
from agent.ingest import sync_imessage_threads
from agent.store import load_store, save_store, store_path
from agent.imessage_sender import send_with_fallback
# ... etc

async def daily_run():
    """The 9am job: sync, score, draft, deliver, archive."""
    store = load_store(store_path())
    sync_imessage_threads(store=store)
    save_store(store_path(), store)
    candidates = select_daily_candidates(store, profile="personal", limit=5)
    text = render_telegram_digest(candidates)
    await send_digest(text, candidates=[c.model_dump() for c in candidates])
    archive_digest_to_brain(candidates, run_at=datetime.now())

async def on_telegram_callback(action, person_id, run_id):
    """Handle button taps from the digest."""
    if action == "send":
        # ... look up draft, send via send_with_fallback, mark sent
    elif action == "skip":
        # ... mark skipped
    elif action == "snooze":
        # ... set snooze_until
    # etc

def main():
    sched = RolodexScheduler(run_callback=daily_run)
    sched.start()
    asyncio.run(run_callback_listener(on_telegram_callback))

if __name__ == "__main__":
    main()
```

### Step C — Copy and adapt the tests

Source: `/Users/pilksclaes/Pilk Ai/pilk-ai/tests/test_rolodex_*.py` (10 files)
Destination: `/Users/pilksclaes/rolodex AI/tests/`

Refactor imports: `from core.rolodex.X import Y` → `from agent.X import Y`.
Tests for PILK-specific tool wrappers (`test_rolodex_tools.py`, `test_rolodex_send.py`)
should be replaced with new tests against the standalone equivalents in
`agent/imessage_sender.py`, `agent/telegram_bot.py`, etc.

### Step D — Run the test suite

```bash
cd "/Users/pilksclaes/rolodex AI"
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -v
```

Target: as close to the original 63 passing as possible. Some tests may need
real adaptation since they tested PILK-specific tool wrappers. Expect 50-60 to pass
out of the box; the rest need ~30 min of adaptation.

### Step E — Remove rolodex from PILK

Once the standalone tests are green, clean up PILK:

```bash
cd "/Users/pilksclaes/Pilk Ai/pilk-ai"
git checkout main
# Delete the rolodex code
rm -rf core/rolodex/
rm core/tools/builtin/rolodex.py
rm -rf agents/rolodex_agent/
rm -rf triggers/rolodex_daily_digest/
rm tests/test_rolodex_*.py

# Revert the wire-up changes in app.py and __init__.py
# (Codex: identify the rolodex-specific imports/registrations and remove them.
#  Leave anything that was already there before we added rolodex code.)
git diff core/api/app.py core/tools/builtin/__init__.py pyproject.toml

git add -A
git commit -m "chore(rolodex): extract to standalone Rolodex AI package"
```

This kills the `UnboundLocalError` permanently (the broken wire-up is gone) and
PILK boots clean.

### Step F — Smoke test the standalone

```bash
cd "/Users/pilksclaes/rolodex AI"
source .venv/bin/activate
cp .env.example .env
# Fill in ANTHROPIC_API_KEY and TELEGRAM_BOT_TOKEN/CHAT_ID at minimum

ROLODEX_DRY_RUN=1 rolodex digest
# Should sync, score, build 5 drafts, print them, and NOT send to Telegram

rolodex status
# Should show all health checks green
```

If both work, the migration is done. Switch to: `rolodexd` (the daemon) to start
the 9am trigger.

---

## 3. Acceptance criteria

- [ ] All 9 portable modules copied to `agent/` with imports rewired
- [ ] `agent/daemon.py` exists and orchestrates the daily flow using standalone substitutes
- [ ] Tests adapted and ≥80% passing
- [ ] PILK boots clean — no UnboundLocalError, no rolodex-related code anywhere
- [ ] `rolodex digest` (CLI) works end-to-end in dry-run mode on the user's actual data
- [ ] `rolodexd` (daemon) starts and registers the 9am trigger

---

## 4. What stays in `rolodex AI/` (unchanged)

The visual / marketing surface stays where it is:

- `app.html` — interactive Mac dashboard prototype
- `mobile-app.html` — iPhone onboarding mockup
- `landing.html` — marketing site
- `rolodex.json` — sample data shape (now also written to `~/PILK/state/rolodex/` by the agent)
- `extension/` — Chrome extension for IG/FB DM capture
- `ARCHITECTURE.md`, `PRODUCT.md`, `OVERVIEW.md` — strategy + spec docs
- `fix_pilk_app.py` — obsolete after Step E (PILK no longer has the bug); keep for reference or delete

---

## 5. Order of operations summary

1. ✅ Standalone scaffolding written (this session, no MCP needed)
2. ⏳ Codex copies portable modules from PILK → `agent/` (needs MCP)
3. ⏳ Codex writes `agent/daemon.py` orchestration layer
4. ⏳ Codex adapts tests, runs pytest
5. ⏳ Codex removes rolodex from PILK and verifies PILK boots clean
6. ⏳ Operator runs `rolodex status` and `ROLODEX_DRY_RUN=1 rolodex digest` to smoke test
7. ⏳ Operator starts `rolodexd` for daily 9am operation

Steps 2-5 are one Codex job (~10-15 min). Steps 6-7 are operator commands.
