// Rolodex Capture — service worker
// Receives captured messages from content scripts, deduplicates, persists to chrome.storage.local

const STORAGE_KEY = "rolodex_captures";
const MAX_CAPTURES = 5000;

chrome.runtime.onInstalled.addListener(async () => {
  const existing = await chrome.storage.local.get(STORAGE_KEY);
  if (!existing[STORAGE_KEY]) {
    await chrome.storage.local.set({ [STORAGE_KEY]: [] });
  }
  console.log("[Rolodex] installed. Storage initialized.");
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "rolodex.capture") {
    handleCapture(msg.payload).then((result) => sendResponse(result));
    return true; // async
  }
  if (msg.type === "rolodex.exportJSON") {
    exportToJSON().then((result) => sendResponse(result));
    return true;
  }
  if (msg.type === "rolodex.getStats") {
    getStats().then((stats) => sendResponse(stats));
    return true;
  }
  if (msg.type === "rolodex.clear") {
    clearCaptures().then(() => sendResponse({ ok: true }));
    return true;
  }
  return false;
});

async function handleCapture(capture) {
  const data = await chrome.storage.local.get(STORAGE_KEY);
  const list = data[STORAGE_KEY] || [];

  // dedupe by composite key
  const key = `${capture.platform}::${capture.conversation_id}::${capture.timestamp}::${capture.text?.slice(0, 60)}`;
  if (list.some((c) => c._key === key)) {
    return { ok: true, deduped: true };
  }

  capture._key = key;
  capture._captured_at = new Date().toISOString();
  list.push(capture);

  if (list.length > MAX_CAPTURES) {
    list.splice(0, list.length - MAX_CAPTURES);
  }

  await chrome.storage.local.set({ [STORAGE_KEY]: list });
  return { ok: true, total: list.length };
}

async function getStats() {
  const data = await chrome.storage.local.get(STORAGE_KEY);
  const list = data[STORAGE_KEY] || [];

  const byPlatform = {};
  const byConversation = new Map();
  let lastCapturedAt = null;

  for (const c of list) {
    byPlatform[c.platform] = (byPlatform[c.platform] || 0) + 1;
    const conv = byConversation.get(c.conversation_id) || {
      platform: c.platform,
      conversation_id: c.conversation_id,
      participant: c.participant_handle || c.participant_name || c.conversation_id,
      message_count: 0,
      last_message_at: null,
    };
    conv.message_count += 1;
    if (!conv.last_message_at || c.timestamp > conv.last_message_at) {
      conv.last_message_at = c.timestamp;
    }
    byConversation.set(c.conversation_id, conv);
    if (!lastCapturedAt || c._captured_at > lastCapturedAt) {
      lastCapturedAt = c._captured_at;
    }
  }

  return {
    total: list.length,
    byPlatform,
    conversations: [...byConversation.values()].sort((a, b) =>
      (b.last_message_at || "").localeCompare(a.last_message_at || "")
    ),
    lastCapturedAt,
  };
}

async function exportToJSON() {
  const data = await chrome.storage.local.get(STORAGE_KEY);
  const list = data[STORAGE_KEY] || [];

  // Group by conversation, shape into rolodex.json-compatible Channel records
  const conversations = new Map();
  for (const c of list) {
    const key = `${c.platform}:${c.conversation_id}`;
    if (!conversations.has(key)) {
      conversations.set(key, {
        platform: c.platform,
        conversation_id: c.conversation_id,
        participant_handle: c.participant_handle || null,
        participant_name: c.participant_name || null,
        messages: [],
      });
    }
    conversations.get(key).messages.push({
      timestamp: c.timestamp,
      direction: c.direction,
      text: c.text,
      sender: c.sender,
    });
  }

  const exportShape = {
    schema_version: "rolodex-extension-export-1.0",
    exported_at: new Date().toISOString(),
    source: "rolodex_chrome_extension",
    conversation_count: conversations.size,
    message_count: list.length,
    conversations: [...conversations.values()],
  };

  const json = JSON.stringify(exportShape, null, 2);
  const blob = new Blob([json], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const filename = `rolodex-capture-${new Date().toISOString().slice(0, 10)}.json`;

  await chrome.downloads.download({
    url,
    filename,
    saveAs: true,
  });

  return { ok: true, message_count: list.length, conversation_count: conversations.size };
}

async function clearCaptures() {
  await chrome.storage.local.set({ [STORAGE_KEY]: [] });
}
