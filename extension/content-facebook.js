// Rolodex Capture — Facebook Messenger content script
// Captures DMs from messenger.com and facebook.com/messages.

(() => {
  if (window.__rolodexFBInjected) return;
  window.__rolodexFBInjected = true;

  console.log("[Rolodex FB] content script loaded");

  const PLATFORM = "facebook_messenger";
  const seen = new WeakSet();

  function getConversationId() {
    // messenger.com/t/<id> or facebook.com/messages/t/<id>
    const m = location.pathname.match(/\/t\/([^\/]+)/);
    return m ? m[1] : "unknown";
  }

  function getParticipantInfo() {
    // Header heuristics
    const candidates = [
      ...document.querySelectorAll(
        'div[role="banner"] h1, div[role="banner"] [role="heading"], header h1, header [role="heading"]'
      ),
    ];
    for (const el of candidates) {
      const text = el.textContent?.trim();
      if (text && text.length < 80 && !/Chats|Messenger|Messages/i.test(text)) {
        return { name: text, handle: null };
      }
    }
    return { name: null, handle: null };
  }

  function classifyDirection(node) {
    let cur = node;
    for (let i = 0; i < 6 && cur; i++) {
      const cls = cur.className || "";
      if (typeof cls === "string") {
        if (/sent|outgoing|right|byMe/i.test(cls)) return "outbound";
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
    const text = (node.innerText || node.textContent || "").trim();
    if (!text) return null;
    if (text.length > 4000) return text.slice(0, 4000);
    return text;
  }

  function extractTimestamp(node) {
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
          console.log("[Rolodex FB] captured:", capture);
        }
      })
      .catch((err) => console.warn("[Rolodex FB] send failed:", err));
  }

  function scanForMessages(root = document) {
    const candidates = root.querySelectorAll(
      'div[role="row"], div[role="listitem"], li[role="listitem"], div[data-testid*="message"]'
    );
    candidates.forEach(processCandidateMessage);
  }

  setTimeout(() => scanForMessages(), 1500);

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

  let lastURL = location.href;
  new MutationObserver(() => {
    if (location.href !== lastURL) {
      lastURL = location.href;
      console.log("[Rolodex FB] URL changed, re-scanning...");
      setTimeout(() => scanForMessages(), 1500);
    }
  }).observe(document, { childList: true, subtree: true });
})();
