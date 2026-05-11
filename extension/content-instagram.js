// Rolodex Capture — Instagram DM content script
// Watches the IG Direct page for messages and ships them to the background worker.
//
// IG's DOM is opaque and changes frequently. This script uses heuristic selectors
// that we expect to need real-world calibration. Each capture event is logged to the
// console with [Rolodex IG] so you can verify selectors are matching.

(() => {
  if (window.__rolodexIGInjected) return;
  window.__rolodexIGInjected = true;

  console.log("[Rolodex IG] content script loaded");

  const PLATFORM = "instagram";
  const seen = new WeakSet();

  function getConversationId() {
    // URL pattern: /direct/t/<conversation_id>/
    const m = location.pathname.match(/\/direct\/t\/([^\/]+)/);
    return m ? m[1] : "unknown";
  }

  function getParticipantInfo() {
    // IG renders the participant name in the conversation header
    // Heuristic: first <h2> or aria-label conversation header in the message thread
    const headerCandidates = [
      ...document.querySelectorAll('header h2, header [role="heading"], div[role="heading"]'),
    ];
    for (const el of headerCandidates) {
      const text = el.textContent?.trim();
      if (text && text.length < 60 && !text.includes("Messages") && !text.includes("Chats")) {
        return { name: text, handle: null };
      }
    }
    return { name: null, handle: null };
  }

  function classifyDirection(node) {
    // IG typically aligns sent messages to one side via flex/justify-content
    // Heuristic: check computed alignment or parent class containing 'right'/'sent'
    let cur = node;
    for (let i = 0; i < 6 && cur; i++) {
      const cls = cur.className || "";
      if (typeof cls === "string") {
        if (/sent|outgoing|right/i.test(cls)) return "outbound";
        if (/received|incoming|left/i.test(cls)) return "inbound";
      }
      const style = cur.style;
      if (style?.justifyContent === "flex-end") return "outbound";
      if (style?.justifyContent === "flex-start") return "inbound";
      cur = cur.parentElement;
    }
    return "unknown";
  }

  function extractText(node) {
    // Avoid pulling reactions/timestamps. Heuristic: longest text child.
    const text = (node.innerText || node.textContent || "").trim();
    if (!text) return null;
    if (text.length > 4000) return text.slice(0, 4000);
    return text;
  }

  function extractTimestamp(node) {
    // Look for nearest <time> element or aria-label with a date
    let cur = node;
    for (let i = 0; i < 5 && cur; i++) {
      const t = cur.querySelector?.("time");
      if (t) return t.getAttribute("datetime") || t.textContent;
      cur = cur.parentElement;
    }
    return new Date().toISOString();
  }

  function processCandidateMessage(node) {
    if (seen.has(node)) return;
    seen.add(node);

    const text = extractText(node);
    if (!text || text.length < 1) return;

    const direction = classifyDirection(node);
    const conversation_id = getConversationId();
    const participant = getParticipantInfo();

    const capture = {
      platform: PLATFORM,
      conversation_id,
      participant_name: participant.name,
      participant_handle: participant.handle,
      sender: direction === "outbound" ? "me" : participant.name || "unknown",
      direction,
      text,
      timestamp: extractTimestamp(node),
    };

    chrome.runtime
      .sendMessage({ type: "rolodex.capture", payload: capture })
      .then((res) => {
        if (res && !res.deduped) {
          console.log("[Rolodex IG] captured:", capture);
        }
      })
      .catch((err) => console.warn("[Rolodex IG] send failed:", err));
  }

  function scanForMessages(root = document) {
    // Heuristic selectors — IG uses lots of generated class names so we look for structural cues
    // Strategy: find elements that look like message bubbles (have text, have aria role of "row" or
    // are inside a div[role="grid"] with role="row")
    const candidates = root.querySelectorAll(
      'div[role="row"], div[role="listitem"], li[role="listitem"], div[data-testid*="message"], div[data-testid*="DirectMessage"]'
    );
    candidates.forEach(processCandidateMessage);
  }

  // Initial pass
  setTimeout(() => scanForMessages(), 1500);

  // Watch for new messages
  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      m.addedNodes.forEach((n) => {
        if (n.nodeType === 1) {
          if (n.matches?.('div[role="row"], div[role="listitem"], li[role="listitem"]')) {
            processCandidateMessage(n);
          } else {
            scanForMessages(n);
          }
        }
      });
    }
  });

  observer.observe(document.body, { childList: true, subtree: true });

  // Re-scan on URL change (IG SPA)
  let lastURL = location.href;
  new MutationObserver(() => {
    if (location.href !== lastURL) {
      lastURL = location.href;
      console.log("[Rolodex IG] URL changed, re-scanning...");
      setTimeout(() => scanForMessages(), 1500);
    }
  }).observe(document, { childList: true, subtree: true });
})();
