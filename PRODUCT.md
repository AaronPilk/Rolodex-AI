# Rolodex — Product Brief v0.1

> The AI that quietly remembers everyone in your life and reaches out at exactly the right moments — so the people who matter never feel forgotten.

---

## The 60-second pitch

Real relationships die quietly. Not from a fight — from drift. You meant to text Mike. You meant to thank Coach. You meant to congratulate Sarah on the promotion. You didn't, and now it's been 18 months, and reaching out feels weird.

Rolodex makes that drift impossible. It learns who matters to you, what you know about them, and the natural rhythm you'd want with each person. Then every morning it shows you who's due, with a message drafted in your voice, ready to send as a real iMessage from your number. You tap approve. The relationship stays alive.

It's a relationship operating system, but it feels like a thoughtful friend who never forgets.

---

## Who this is for

### Tier 1 — Consumer ($14/mo)
Ambitious 25–45 yo professionals who care about their network but lose people to busy lives. Founders, creators, freelancers, anyone whose career partly depends on who they know.

### Tier 2 — Pro ($49/mo) — *primary revenue driver*
Relationship-driven solo operators whose income depends on staying top-of-mind with hundreds of people:
- **Real estate agents** (sphere-of-influence is everything)
- **Financial advisors** (1 retained client = 30+ years of fees)
- **Talent managers / agents** (Pilk's adjacent world via skyway.media)
- **Recruiters** (warm reach >> cold reach)
- **Business coaches / consultants**
- **Insurance brokers**

These customers already pay $50–200/mo for inferior tools (Brevet, Sphere, Real Geeks, BoomTown). They have clear ROI math: re-activate one client per quarter, the tool pays for itself for 5 years.

### Tier 3 — Teams ($99/seat/mo)
Brokerages, agencies, advisory firms with 10–500 producers. Adds shared books, deal-flow alerts, CRM sync, SOC 2.

---

## Why now

1. **AI personalization just crossed the line.** Two years ago an AI-drafted "casual catchup" message was uncanny. Today it can be indistinguishable from how you write — *if* it has the right context.
2. **iMessage as a channel is undervalued.** Cold email is dead. SMS is sketchy. iMessage from a real person's number reads like a friend. Nothing else does.
3. **The "personal CRM" category exists but is anemic.** Dex, Clay, Monaru, Champ are journals/reminders. None actually *do the outreach for you*. That's the whole game.

---

## Differentiation — why we win

| Competitor | What they do | What they miss |
|---|---|---|
| **Dex / Monaru / Clay (personal)** | Manual personal CRM with reminders | You still write every message yourself. The hard part. |
| **Salesforce / HubSpot** | Enterprise CRM with mass sequencing | Feels corporate. Wrong for personal/SMB relationships. |
| **Apollo / Outreach** | Cold outbound for sales | Built for prospects, not people you actually know |
| **Folk / Attio** | Modern relationship CRM | No outreach engine. No drafts. |
| **Rolodex** | **AI drafts the actual message + native iMessage send + intelligent cadence** | — |

Our moat: the **personal context graph**. Every interaction makes the next message better. Switching costs grow over time.

---

## The hard technical truths (don't fight these)

1. **iOS cannot read iMessage / SMS / Contacts in the background.** Apple blocks all of it. There is no workaround. ✅ *Therefore: the engine lives on Mac. iPhone is a notification companion.*

2. **Instagram and Facebook DMs are not accessible via API.** Meta actively shuts down scrapers. ✅ *Therefore: v1 manually accepts pasted-in IG/FB context. v2 explores Meta's narrow Threads API and a Chrome extension that captures while the user browses.*

3. **iMessage send from third-party apps is technically constrained.** macOS allows sending via AppleScript / Messages Framework with user permission (Full Disk Access, Automation permissions). This works but is fragile across macOS versions. ✅ *Therefore: ship as a notarized Mac app, not a web app. Maintain compatibility tests against each macOS release.*

4. **Privacy is the trust contract.** People's contact lists are sacred. ✅ *Therefore: local-first storage. End-to-end encrypted optional cloud sync. Never train shared models on user data. SOC 2 in year 1.*

---

## What's built today (v0)

This session shipped the foundation files in `/Users/pilksclaes/rolodex AI/`:

| File | What it is |
|---|---|
| `app.html` | Interactive dashboard prototype. Open in any browser to see the product surface. Apple aesthetic, blue iMessage bubbles, full Due-Now feed with 5 example contacts, AI draft cards, send animations, add-person modal. |
| `landing.html` | Marketing landing page. Pricing, FAQ, waitlist signup, dark-mode Apple aesthetic. Drop on any domain to start collecting waitlist signups. |
| `rolodex.json` | Source-of-truth data schema. Person template includes contact info, tier, cadence, scoring, history, context. |
| `PRODUCT.md` | This document. |

### What it currently does
- Prototype is **visually shippable** as a demo for investors, customers, or developers
- Schema is **production-ready** and can power the real app on day one

### What's intentionally not built yet
- Real Mac app (Swift/AppKit) — that's the next $30–80k engineering investment
- Backend (only needed for cloud sync/Teams plan; v1 personal can be local-first)
- Auth / billing (Stripe + Sign in with Apple)
- AI message-drafting engine (Anthropic Claude API)
- iMessage send integration (AppleScript bridge)
- Mac Contacts + iMessage history bootstrap

---

## The path to a sellable product

### Phase 1 — Validate (next 2 weeks, $0)
1. Deploy `landing.html` on a domain (rolodex.app, getrolodex.com — check availability)
2. Drive 100 visitors via tight LinkedIn/X post to your network
3. Goal: 30+ waitlist signups → confirms demand
4. Talk to 10 of them. Specifically *ask if they'd pay $49/mo*. Record the answers.

### Phase 2 — Working prototype (4–8 weeks, ~$0–8k)
Two paths, pick one:

**Path A — Build it yourself with AI tools**
- Use Cursor + Claude to write the Mac app in Swift
- Use existing libraries (FluidGradient, MessageKit, Down) for UI
- Wire to Claude API for drafting, AppleScript for iMessage send
- Realistic for a determined non-engineer in 6–10 weeks

**Path B — Hire a Swift contractor**
- 1 senior Mac developer, 6–8 weeks, $15–25k
- You direct product. They build to the spec these files imply.

### Phase 3 — Charge real money (months 3–4)
- Beta with 30 customers paying $14–49/mo
- Iterate hard on message quality (the make-or-break feature)
- $1.5k–4k MRR validates a real business

### Phase 4 — Pick a wedge and lean in (months 5–12)
- Watch which segment converts hardest in beta
- If real estate agents → build agent-specific features (open-house follow-up, anniversaries)
- If founders → build investor-update + intro-flow features
- This becomes the wedge to dominate, then expand from there

---

## Pricing rationale

| Plan | Price | Why this number |
|---|---|---|
| Personal | $14/mo | Slightly above Dex ($12) and Monaru ($10). The "I'd pay for Spotify-level utility" zone. |
| Pro | $49/mo | Below ZoomInfo, Apollo, BoomTown ($89–199). Easy yes for anyone whose income depends on relationships. |
| Teams | $99/seat | Sub-Salesforce. Anchor with SOC 2 + admin controls. |

Free trial: 14 days. No credit card up front for Personal. CC required for Pro/Teams (qualifies intent).

---

## Risks & how to manage

| Risk | Mitigation |
|---|---|
| **Apple breaks AppleScript iMessage send** | Maintain version-pinned compatibility tests; have email + LinkedIn fallback channels |
| **AI messages start sounding generic at scale** | Per-person tone matching from real iMessage history. Strict "max 1 send/day/person" cap. |
| **Privacy backlash** | Local-first architecture. SOC 2. Never share contact lists. Make this loud in marketing. |
| **A friend realizes it's AI** | Always-approve flow + tone matching makes this rare. If it happens, it's a story, not a scandal — you wrote the values, AI wrote the words. |
| **Meta sues for scraping** | Don't scrape. Manual paste only. |

---

## Brand notes

- **Name:** "Rolodex" works — instantly understood, slightly retro-cool, owns the relationship-management archetype
- **Wordmark:** Lowercase logotype is friendlier than uppercase. Slightly tighter letter spacing than default SF Pro
- **Color:** iMessage blue (#007AFF) as the only saturated color. Everything else neutral. Dark mode marketing, light mode product.
- **Voice:** Warm, observational, slightly dry. Never preachy. "We're not selling you a productivity tool. We're selling you the version of yourself that didn't lose touch with Mike."
- **Anti-marketing:** Never use the word "network." Never say "leverage." Never gamify. Never show streaks.

---

## Next session — pick one

Tell me which to do next:

1. **"Productize the Mac app spec"** → I write a developer-grade SDD covering data model, AI prompt design, iMessage send integration, Contacts/iMessage bootstrap, error handling. Ready to hand to a Swift contractor.
2. **"Build the actual rolodex right now for me"** → We use the existing schema and start adding your real people, with Cowork (me) acting as the engine, sending iMessages via the iMessage MCP tonight.
3. **"Sharpen the landing page and ship it"** → Polish copy, add testimonials/screenshots, deploy to a domain, set up the waitlist database, draft your launch post.
4. **"Plan the wedge / GTM strategy"** → Pick a vertical (talent agents seems natural given skyway.media), design the first 30 customer outreach plan, write the cold-email/DM templates, build the proof-of-concept demo for that vertical.

The session's work persists. Pick up wherever.
