# Rolodex — System Architecture (v1.0)

> The honest engineering blueprint for an AI relationship manager that knows everyone in your life across every channel you've ever used to talk to them — and reaches out at the right moment, in the right voice, on the right device.

This document is what a Swift/iOS engineering team builds from. It's deliberately direct about what's possible, what isn't, and what workarounds actually ship.

---

## 1. Product thesis (one paragraph)

A user has thousands of relationships scattered across iMessage, SMS, WhatsApp, Instagram DMs, Facebook Messenger, Telegram, several email accounts, multiple devices, and at least two life "modes" (personal vs business). Every relationship has its own tone, its own cadence, and its own un-stated rules — *I lowercase-text my brother, I capitalize-and-punctuate my mother, I never close emails with "best" to my high-school friends*. Rolodex's job is to ingest all of that, understand the unique shape of each relationship, and surface — once a day — a small ranked list of people the user is at risk of losing, each with a draft message that sounds *exactly* like the user wrote it after thinking about that specific person.

---

## 2. Data sources — what's possible, what isn't, how to ship anyway

Each row below is a real engineering decision. "Possible?" is a hard truth, not aspirational.

| Source | Possible? | Mechanism | Honesty notes |
|---|---|---|---|
| **iMessage / SMS history** | ✅ macOS only | Read `~/Library/Messages/chat.db` (SQLite) with Full Disk Access permission; iCloud-synced messages flow in too. iOS apps cannot do this — Apple blocks it. | Reads back to first message ever sent on this Apple ID. Works today via the existing PILK MCP `messages_search_mine` tool. |
| **macOS Contacts** | ✅ macOS | `Contacts.app` AppleScript / Contacts framework with Automation permission. | Need first-launch permission flow. PILK MCP `contacts_search` works when Contacts.app is running. |
| **Calendars (Apple, Google, Outlook)** | ✅ | EventKit (Apple), Google Calendar API, MS Graph API. | Birthdays + meetings + travel — high-signal life events. PILK MCP has `calendar_read_my_today` / `_range` already. |
| **Gmail (multi-account)** | ✅ | OAuth 2.0 per account, Gmail API for read + push notifications via Pub/Sub. | One auth per account. Tone differs *significantly* between work email vs personal email and the system MUST partition them. PILK MCP has `gmail_*` tools. |
| **Microsoft 365 / Outlook** | ✅ | MS Graph API. | Same model as Gmail. |
| **WhatsApp** | ⚠️ Hard | WhatsApp does NOT have a personal-data API. Two paths: (a) **WhatsApp Web** — open in a hidden WebView and scrape the DOM (fragile, against ToS); (b) **WhatsApp Business API** — only works for business accounts, not personal. | **Recommended:** require user to export chat history (Settings → Chats → Export Chat) and ingest the .txt file. Manual, but legal and stable. Future: explore Apple's Shortcuts integration for partial reads. |
| **Instagram DMs + comments** | ⚠️ Restricted | Meta blocks DM reads via API for personal accounts. Business / Creator accounts have **Messenger Platform API** for IG (limited to 24h response window). | **Recommended:** Chrome extension that captures DMs/comments while user is browsing IG normally — read-only, on-device, never leaves their machine. This is the realistic shipping path. |
| **Facebook Messenger** | ⚠️ Restricted | Same Meta API constraints. | Same Chrome-extension or `/your_facebook_information/messages.json` export approach. |
| **Telegram** | ✅ | Telegram **MTProto API** (user-account level) is well-documented and supported. Can read history + receive new messages. | Easy win. Aaron already uses Telegram with PILK — same auth flow extends. |
| **Discord** | ✅ | Discord **bot/user token** API (user tokens are unofficial but functional; bots are limited to mutual servers). | Useful for community-style relationships (gamer friends). |
| **LinkedIn DMs / activity** | ⚠️ Restricted | No DM API. LinkedIn Sales Navigator + people search are available via paid tier. | **Recommended:** Chrome extension captures profile views, role changes, post activity. |
| **Slack (workspaces)** | ✅ | OAuth into each workspace; user-level token reads DMs + channels they're in. | Important for business-mode users. |
| **Phone call history** | ⚠️ macOS partial | macOS keeps call log in iCloud-synced `CallHistory.db` if the user has iPhone Continuity. Read with Full Disk Access. | Frequency of calls is a strong relationship signal; integrate where available. |
| **Voice transcription / dictated messages** | ✅ | Whisper / Apple SpeechRecognizer on local audio. | Voicemail transcripts add another tone signal. |

**Design principle:** every data source must be opt-in per user, removable per user, and never leave their device unless they explicitly turn on encrypted cloud sync.

---

## 2.5 iPhone permission flow per source — what "Allow" actually triggers

The user-facing experience must be: tap **Allow** → wait → done. Behind that button, each source uses a different mechanism. This table is what an iOS engineer implements.

| Source | What user sees | What "Allow" actually triggers | Mac required? | iPhone-direct? |
|---|---|---|---|---|
| **iMessage / SMS** | Native-style permission card → Allow | Sends an Apple Push notification to the user's paired Mac (companion app). Mac begins indexing `chat.db`. iPhone receives encrypted index summary via CloudKit. If no Mac paired, app prompts to download companion. | ✅ Yes | ❌ |
| **Contacts** | iOS native permission dialog | Standard `CNContactStore.requestAccess()` | ❌ | ✅ |
| **Calendar (iCloud)** | iOS native permission dialog | Standard `EKEventStore.requestAccess()` | ❌ | ✅ |
| **Calendar (Google)** | OAuth bounce | Google OAuth 2.0 in `ASWebAuthenticationSession`. Read-only Calendar scope. | ❌ | ✅ |
| **Calendar (Outlook)** | OAuth bounce | MS Graph OAuth, same flow. | ❌ | ✅ |
| **Gmail (multi-account)** | "Sign in with Google" | OAuth 2.0, Gmail.readonly + metadata scope. Repeat for each account. | ❌ | ✅ |
| **Outlook / M365** | "Sign in with Microsoft" | OAuth 2.0, MS Graph Mail.Read scope. | ❌ | ✅ |
| **Telegram** | Phone number → SMS code → 2FA password | TDLib (official Telegram client library) embedded in app. User logs in directly. App keeps long-lived session. | ❌ | ✅ |
| **WhatsApp** | "Show me how to export" → instructional cards | Walks user through WhatsApp's own Export Chat feature, then accepts the .txt/.zip via iOS Share Sheet. Reads in-app. Re-import to refresh. | ❌ | ✅ (manual) |
| **Instagram DMs + comments** | "Install browser extension" → universal link → desktop email | Sends an email/iMessage with a link to install the Rolodex Chrome/Safari extension on the user's computer. Extension does passive read-only capture. iPhone surfaces the data once it lands in the synced brain. | ⚠️ Computer required (not necessarily Mac — any browser) | ❌ for capture, ✅ for digest |
| **Facebook Messenger** | Same browser-extension flow OR Facebook data-export upload via Share Sheet | Either Chrome extension passive capture, or import of `messages.json` from FB's data export. | ⚠️ Computer for ext.; iPhone OK for export upload | Partially |
| **TikTok DMs** | "Request your data" instructional card | Walks user through TikTok's data download flow (Settings → Privacy → Download your data, ~24h delivery). Then upload via Share Sheet. | ❌ | ✅ (manual) |
| **X DMs** | "Sign in with X" | X API v2 OAuth, paid Basic tier ($100/mo for the platform — or pass through to user's own dev account on Free tier). | ❌ | ✅ |
| **LinkedIn** | "Install browser extension" | Same Chrome/Safari extension. Passively logs profile views, role changes, post engagement. | ⚠️ Computer required | Partially |
| **Discord** | "Sign in with Discord" | OAuth 2.0 + user-token long-poll for DM messages. | ❌ | ✅ |
| **Phone call history** | "Allow access to call history" | macOS only — reads `~/Library/Application Support/CallHistoryDB/CallHistory.db` with Full Disk Access. iOS itself does not expose call history to third-party apps. | ✅ Yes | ❌ |

### Onboarding UX rules
1. **Never block on a source.** Every screen has "Skip this source — add later." First-launch bounce rate spikes if any single source feels mandatory.
2. **Show the honesty note.** The amber-colored "How this works" note in the mockup is the trust-builder. Users who understand WHY a step is manual don't churn on it; users who don't see why feel scammed.
3. **Phase the asks.** Don't show all 14 sources on day one. Show the 4 best (Contacts, iMessage via Mac, Gmail, Calendar) at first launch. Surface the rest in a "Connect more sources" tile on the home screen.
4. **Re-prompt for re-auth.** Tokens expire. The iPhone home screen must surface a yellow chip "Gmail · needs reconnect" the moment a token fails — not silently degrade.
5. **Paired-device awareness.** When user adds a Mac as companion, iPhone shows it as "Mac · indexing iMessage" with a live progress bar.

### What an iOS engineer needs to build
- `SourceConnector` protocol (Swift), one implementation per source
- `OAuthCoordinator` for the OAuth-based ones (reusable)
- `MacCompanionPairingService` using CloudKit-shared encrypted database
- `ChatExportImporter` for WhatsApp/FB/TikTok exports (Share Sheet → SQLite)
- `BrowserExtensionPairing` (deep-link handoff with QR code on desktop)
- `ReauthorizationManager` (background token refresh + UI prompt)

The mockup at `mobile-app.html` shows what each of these screens should look like.

---

## 3. The Tonal Model — how Rolodex learns how you speak to each person

This is the *moat*. Most "AI message generator" tools produce slop because they treat all your relationships as one voice. Rolodex builds a per-relationship tonal fingerprint.

### Inputs (per person)
- All historical messages between you and them (across every channel)
- Frequency / time-of-day patterns
- Who initiates more
- Average reply latency
- Group threads they're in with you
- The relationship classification (family, business, childhood friend, etc.)

### Tonal fingerprint dimensions (extract per relationship)
| Dimension | Measure | Example signal |
|---|---|---|
| **Capitalization** | % of messages with sentence-case | Brother: 5%. Mom: 95%. |
| **Punctuation** | period / question / exclamation density | Best friend: rare. Boss: precise. |
| **Emoji rate** | per 100 words | Partner: 12. Coach: 0.2. |
| **Profanity** | per 100 words | Best friend: 8. Mom: 0. |
| **Avg msg length (words)** | mean & stdev | Brother: 6 words. Client: 28 words. |
| **Slang / shibboleth phrases** | top n-grams unique to this thread | "yo bro", "lmaoo", "dude", "Pop", "g" |
| **Sign-off patterns** | message-tail analysis | Mom: "love you". Buddy: just stops. |
| **Topic graph** | recurring subjects between you | Dad: baseball mechanics, Hornets. Cam: client billing, NV ads. |
| **Inside jokes / callbacks** | repeated phrases that aren't generic | "Mack the dog", "Peanut clip", "the field thing" |
| **Reply-required vs not** | classification of "did this thread end naturally?" (see §4) | Group memes: rarely require reply. Direct ask: requires reply. |

### Storage
A per-person `tone_profile` JSON object. Updated incrementally as new messages flow in.

### Generation
When drafting an outreach message:
1. Retrieve last 50 messages with this person
2. Retrieve their `tone_profile`
3. Retrieve current life context (calendar, IG/FB recent activity if available, last_contact_summary)
4. Prompt LLM with a **system prompt** that includes:
   - The tonal fingerprint as constraints ("write in lowercase with no terminal punctuation, ~6-12 words, no emoji, no exclamation marks")
   - 3-5 verbatim past messages from the user as few-shot examples
   - The relationship class
   - The reason the message is being sent (cadence-due / life-event / specific context)
5. Generate 3 candidates, pick the one that scores highest on tonal-match (cosine similarity of stylometric features against the fingerprint).
6. Show user with a "regenerate" button.

---

## 4. Natural-end-of-conversation detector

> The user's instinct: not every message needs a reply. The system must learn this or it'll feel robotic.

### Signal: did this thread end naturally?

Train a classifier on labeled iMessage threads. Features:
- Last message direction (inbound vs outbound)
- Last message type: question? statement? meme/image? "lol"? "k"?
- Time-of-day of last message
- Historical pattern: does this person typically reply to messages of this shape?
- Did the user open the thread but not respond? (read receipts / opened-without-reply signal)

### Output
Each thread gets a `natural_end_score` (0.0 = clearly waiting on a reply, 1.0 = thread is done). Rolodex won't surface as "overdue" any thread where:
- `natural_end_score > 0.7` AND
- it was the *user* who chose not to respond

**Why this matters:** prevents the dashboard from nagging the user about threads that are already complete. The user said: *"sometimes the conversation ends, and you don't have to reply back."*

### Bonus: detect WHY user didn't reply
Cluster un-replied threads. Common reasons:
- "Asked an open-ended question I didn't have a good answer to" → suggest deferred reply
- "Group thread spam I didn't engage with" → suppress
- "Sales/spam pitch" → suppress + tag person as low-priority
- "Heavy emotional content I was avoiding" → flag privately with care, don't auto-send

---

## 5. Group thread handling

Group threads are messy. The system MUST treat them differently.

### Rules
1. **Group threads do NOT count toward 1:1 cadence.** Just because you sent a meme to the family group chat doesn't mean you "talked to Mom" today.
2. **But they DO count toward relationship signal.** Frequent inclusion in a group with someone = warm tie even without 1:1 contact.
3. **Group context informs draft messages.** If you're about to text Mom and she just posted in the family group about her vacation, the draft should reference it.
4. **Members of long-running groups get auto-clustered as a "circle"** (e.g., "high-school crew", "agency Slack folks"). Suggest birthday-appropriate reach-outs to whole circles.

---

## 6. Multi-device, multi-account, multi-mode

This is the user's specific ask: *"if you have multiple phone numbers, you can download this app and sign in to that app on all of your phone devices and you can basically declare what this device is for."*

### Account model
```
User (1)
├── Profile [partition: personal]
│   ├── Apple ID: aaronpilk@…
│   ├── Phone: +1-727-…
│   ├── Email accounts: [pilkingtonent@gmail, …]
│   ├── Connected devices: [Personal MacBook, Personal iPhone]
│   └── People (filtered to this profile)
└── Profile [partition: business]
    ├── Apple ID: aaron@skyway.media
    ├── Phone: +1-XXX-… (business line)
    ├── Email accounts: [aaron@skyway.media, sentientpilkai@gmail]
    ├── Connected devices: [Business MacBook, Business iPhone]
    └── People (filtered to this profile)
```

### Profile rules
- People can belong to multiple profiles (e.g., a friend who's also a business contact)
- Cadence and tier can be set differently per profile (e.g., a client gets a 30-day biz cadence AND a 90-day "they're also kind of a friend" personal cadence)
- Outreach drafts respect the profile that surfaced them — business drafts use the business voice, business email, and business phone
- The user can switch profiles in the app top bar (like Slack workspaces)
- Devices declare a default profile but users can override per session

### Storage / sync
- Each device runs the engine locally
- A user's profile config + tone profiles + people db can sync via end-to-end encrypted iCloud / Dropbox / self-hosted vault
- Conflict resolution: last-write-wins per record, with a per-device journal so nothing gets silently overwritten

---

## 7. Privacy model — non-negotiable

This product has god-tier access to the user's life. The privacy contract has to be airtight or it dies in the press.

### Principles
1. **Local-first.** Raw message bodies, contact lists, and tone fingerprints live on-device. Nothing transits the network unless the user opts into encrypted sync.
2. **No training on user data.** User content is never pooled into a shared model. Period.
3. **Per-relationship redaction.** User can mark any person as "do not analyze" — fully excluded from the system.
4. **Clear deletion.** One-click wipe per profile, per device, per data source.
5. **Audit log.** Every external API call (LLM inference, sync) is logged so user can see what was sent and when.
6. **Soc 2 + GDPR + CCPA ready** before launch.
7. **Approval-gated send.** Aaron's standing rule (`mem_e676305c5337`): every iMessage requires explicit approval. Bake this into the schema (`approval_required: true`, `never_auto_send: true`).
8. **No retention of raw messages on third-party LLMs.** Use Anthropic / OpenAI with **zero-retention enterprise contracts** OR run inference on-device via Apple Intelligence / Llama.cpp / MLX.

### Sensitive content handling
The system WILL encounter:
- Medical / mental health discussions
- Legal matters (Aaron's case is a real example)
- Sexual content
- Substance use
- Family conflict

These are tagged at ingest with a `sensitivity` flag. Drafts are NEVER auto-generated for sensitive threads. The dashboard surfaces sensitive context to the user privately — never embeds it in a draft.

---

## 8. The Inference Stack

| Task | Model | Why |
|---|---|---|
| Tone fingerprinting | Lightweight stylometric extractor (locally, no LLM) | Cheap, deterministic, runs per-message |
| Relationship classification | Claude Haiku via API | Cheap classification ($0.003/msg) |
| Natural-end-of-thread detection | Fine-tuned classifier on user's data | Personalized, runs on-device after first 30 days |
| Draft generation | Claude Sonnet 4.6 | Best-in-class voice matching |
| Bulk re-draft / regeneration | Claude Sonnet | User-triggered, paid tier |
| Embedding + semantic search across history | OpenAI text-embedding-3-small or local MiniLM | "Find every time we talked about [topic]" |

Aaron's standing instruction (`mem_3bcfd8ab0d0a`): always use the cheapest adequate model. Honor: classify with Haiku, draft with Sonnet, escalate to Opus only if the user manually asks for a "really polished" draft.

---

## 9. Phased build plan

### Phase 0 — Validation (today, $0)
- Landing page is live (already built: `landing.html`)
- 100 waitlist signups → green light to invest

### Phase 1 — macOS App MVP (8–12 weeks, $25–40k contract)
- iMessage + Contacts ingestion
- Single Apple ID profile
- Tonal fingerprinting v0
- Daily digest with 5 drafts
- Approve + send via AppleScript / Messages framework
- iCloud sync for backup (encrypted)

### Phase 2 — Multi-channel (8–12 weeks)
- Gmail OAuth
- Calendar
- Telegram
- WhatsApp (export-based)
- Tonal model trained on first 30 days of usage

### Phase 3 — Multi-profile / multi-device (6–8 weeks)
- Business vs personal partitions
- iPhone companion app for approve-on-the-go
- Device picker
- Cross-device sync

### Phase 4 — Browser extension for IG / FB / LinkedIn (4–6 weeks)
- Chrome + Safari extension
- Captures messages and profile activity user is already viewing
- Streams into local engine

### Phase 5 — Teams / B2B (8–12 weeks)
- Shared books-of-business
- CRM sync (Salesforce, HubSpot, GHL)
- Admin panel + SOC 2

---

## 10. Tech stack (recommendation)

| Layer | Choice | Why |
|---|---|---|
| Mac app shell | Swift + SwiftUI | Native, fast, Apple-grade UX |
| Local DB | SQLite + SQLCipher | Encrypted at rest, mature |
| Sync layer | CRDT (Automerge) over CloudKit | Multi-device safe, end-to-end encrypted |
| LLM provider | Anthropic Claude via API + (optional) on-device MLX/Llama for paranoid users | Best voice quality + offline mode |
| Email connectors | Gmail API + MS Graph + IMAP fallback | Multi-account |
| Telegram | tdlib (official) | Reliable user-level access |
| Browser extension | Manifest V3 (Chrome + Safari) | Cross-browser shippable |
| Web (landing + dashboard for Teams) | Next.js + Tailwind + Vercel | Speed of iteration |
| Auth | Sign in with Apple primary, Google secondary | Privacy-aligned |
| Payments | Stripe | Standard |
| Backend (Teams only) | Supabase or Convex | Postgres + realtime, low-ops |

---

## 11. Open architectural questions (decide before Phase 1 starts)

1. **Self-hosted vault as primary, OR Apple-cloud-only?** Self-hosted attracts power users; Apple-cloud is simpler. Recommend Apple-cloud default with self-host as an "advanced" option.
2. **Should the iPhone app be able to draft on its own** or only act as approval surface? Recommend: approval-only in Phase 3, draft-on-device in Phase 5+ once Apple Intelligence matures.
3. **How aggressive on the "intelligent send window"?** Should the system automatically delay a 11pm draft until 9am the next day even if approved? Recommend yes, with override.
4. **Group thread membership: opt-in per group?** Some users won't want every family group analyzed. Recommend a per-thread "include in analysis" toggle.

---

## 12. What's already built in this session

Everything in `/Users/pilksclaes/rolodex AI/`:

| File | Status |
|---|---|
| `app.html` | Working dashboard prototype with real Aaron data (5 cards, 13 people in DB) |
| `landing.html` | Marketing site with pricing |
| `rolodex.json` | Schema seeded with 13 real people from iMessage history |
| `PRODUCT.md` | Strategic brief — positioning, pricing, paths to revenue |
| `ARCHITECTURE.md` | This document |

The prototype demonstrates the core UX. The architecture demonstrates that the rest is buildable. Together: enough to raise money, hire a team, or hand to a Swift contractor.

---

## 13. The one-page version (for investors)

> Rolodex is the relationship operating system for ambitious people. It ingests every channel where your relationships live — iMessage, Gmail, Instagram, WhatsApp, Telegram, Calendar — learns the unique voice of each relationship, and once a day surfaces the small set of people you're at risk of losing, with a draft message that sounds exactly like you. macOS-native engine; iPhone companion; Chrome extension for browser-based capture. Personal: $14/mo. Pro: $49/mo. Teams: $99/seat/mo. Path to $1M ARR in 18 months on the Pro tier alone with 1,700 customers.

---

*Last updated: 2026-05-08, in-session, Cowork mode.*
