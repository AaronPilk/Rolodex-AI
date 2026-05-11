# Rolodex Capture — Chrome Extension

Phase 4 of the Rolodex architecture, built standalone.

Captures your Instagram and Facebook Messenger DMs **passively** as you browse normally. No automation, no scraping, no posting on your behalf — it just observes the messages already on your screen and stores them locally so the Rolodex agent has the full picture of your relationships.

---

## Why this exists

Apple blocks third-party iOS apps from reading messages from other apps. Meta blocks DM API access for personal Instagram and Facebook accounts. The only way Rolodex can know about IG/FB conversations is to capture them as the user browses normally on a desktop browser.

This extension is that capture surface. It runs only on:

- `instagram.com/direct/*`
- `messenger.com/*`
- `facebook.com/messages/*`

It does nothing on any other site.

---

## How to install (developer mode, ~30 seconds)

1. Open Chrome (or any Chromium browser — Brave, Edge, Arc).
2. Go to `chrome://extensions`.
3. Toggle **Developer mode** on (top right).
4. Click **Load unpacked**.
5. Select the `extension/` folder inside `/Users/pilksclaes/rolodex AI/`.
6. Pin the Rolodex Capture icon to your toolbar.

You should see "Rolodex Capture · 0.1.0" in your extensions list.

> **Note on icons:** This prototype ships without bundled icons. Chrome will use a placeholder. For a real launch you'd add `icons/icon16.png`, `icons/icon48.png`, `icons/icon128.png`. The manifest references them but Chrome won't refuse to load if they're missing.

---

## How to use

1. Open Instagram DMs (`instagram.com/direct/inbox`) or Messenger.
2. Click into a conversation. The extension automatically captures messages on the screen.
3. Scroll up in a thread to load older messages — those get captured too.
4. Click the Rolodex Capture icon to see your stats: total captures, conversations, and recent activity.
5. When you have a useful chunk of data, click **Export to JSON** in the popup — it downloads `rolodex-capture-YYYY-MM-DD.json`.
6. Hand that JSON to the Rolodex agent (it imports as a new channel per person).

---

## What gets captured

Per message:
- `platform`: `"instagram"` or `"facebook_messenger"`
- `conversation_id`: the IG/FB internal ID from the URL
- `participant_name`: best-guess name from the conversation header
- `participant_handle`: when available
- `direction`: `"outbound"` (you sent) / `"inbound"` (they sent) / `"unknown"`
- `text`: the message body
- `timestamp`: when the message was sent (or when the extension first saw it)
- `_captured_at`: when the extension recorded it

**What's NOT captured:** images, videos, voice notes, reactions, link previews, profile photos. Just text and metadata.

**Storage:** all captures live in `chrome.storage.local` on your machine. They never leave the browser unless you click Export. The extension has zero network permissions to anywhere except IG/FB themselves.

---

## Honest caveats — read this

**1. The DOM selectors are heuristic and will need calibration.**

Instagram and Facebook use heavily obfuscated, frequently-changing class names. The selectors in `content-instagram.js` and `content-facebook.js` use generic structural cues (role="row", role="listitem") that should survive most layout changes — but no guarantee. The first time you load it on a real conversation, open the browser console and look for `[Rolodex IG]` or `[Rolodex FB]` logs to confirm captures are landing.

If you see "0 captures" after browsing a real conversation, the selectors need tuning. Open `content-instagram.js` and adjust the `scanForMessages` selector list to match the real DOM you see in DevTools. This is a normal part of shipping any DOM-scraping extension and is the price of Meta not exposing a real API.

**2. Direction detection is heuristic too.**

We classify "outbound" vs "inbound" by looking for class names containing `sent`/`outgoing`/`right` or flexbox alignment. IG/FB don't label messages this way explicitly. Expect ~80% accuracy on the first pass; gets better with calibration.

**3. The extension only sees what's on screen.**

If you don't open a conversation, it doesn't get captured. There's no background scraping. If you have 200 conversations and want them all in your Rolodex, you'll need to scroll through each one once. Chrome storage caps at ~5MB per extension so we cap at 5,000 messages and start dropping the oldest.

**4. ToS surface area.**

Browser extensions that *passively read DOM content while the user browses normally* are well-established and not against Chrome's policies. Meta's ToS prohibit *scraping* and *automation*, neither of which this does — it's a screen reader, essentially. That said, Meta is litigious. If they ever flag your account for unusual extension activity, removing the extension is a one-click revert.

---

## How it integrates with Rolodex agent

The exported JSON has this shape:

```json
{
  "schema_version": "rolodex-extension-export-1.0",
  "exported_at": "2026-05-09T...",
  "source": "rolodex_chrome_extension",
  "conversation_count": 42,
  "message_count": 1872,
  "conversations": [
    {
      "platform": "instagram",
      "conversation_id": "...",
      "participant_handle": "@friend",
      "participant_name": "Friend Name",
      "messages": [
        {
          "timestamp": "2026-05-08T14:32:00Z",
          "direction": "outbound",
          "text": "yo what's up",
          "sender": "me"
        },
        ...
      ]
    },
    ...
  ]
}
```

To import into the PILK rolodex agent, run:

```python
from core.rolodex.ingest import import_extension_export
import_extension_export("/path/to/rolodex-capture-2026-05-09.json")
```

(That importer doesn't exist yet — Phase 4.5 work. The export shape is already aligned with the schema so adding the importer is ~50 lines.)

---

## Files in this folder

| File | Purpose |
|---|---|
| `manifest.json` | Manifest V3 declaration. Permissions, content scripts, popup. |
| `background.js` | Service worker. Receives captures, dedupes, persists, handles export. |
| `content-instagram.js` | Runs on instagram.com/direct/*. Watches DOM mutations, extracts messages. |
| `content-facebook.js` | Same but for messenger.com / facebook.com/messages. |
| `popup.html` + `popup.css` + `popup.js` | The toolbar UI: Apple-aesthetic, light + dark mode, stats, recent conversations, export & clear. |
| `README.md` | This document. |

---

## Roadmap

**v0.1 (this build):** core capture + popup UI + JSON export.

**v0.2:** import into PILK rolodex agent (one CLI command). Better selector calibration based on real-world usage. Direction detection accuracy ↑.

**v0.3:** LinkedIn profile activity capture (job changes, post engagement). Twitter/X DM capture. WhatsApp Web capture.

**v1.0:** Direct push to a running PILK daemon (no JSON file step). Encrypted local cache. Per-conversation include/exclude controls.
