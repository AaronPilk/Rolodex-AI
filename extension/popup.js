// Rolodex Capture — popup logic

const $total = document.getElementById("stat-total");
const $convs = document.getElementById("stat-conversations");
const $platforms = document.getElementById("stat-platforms");
const $list = document.getElementById("recent-list");
const $status = document.getElementById("hd-status");
const $ftLast = document.getElementById("ft-last");
const $btnExport = document.getElementById("btn-export");
const $btnClear = document.getElementById("btn-clear");

function avatarText(name, fallback) {
  if (!name) return fallback || "?";
  const parts = name.trim().split(/\s+/).slice(0, 2);
  return parts.map((p) => p[0]?.toUpperCase()).join("") || fallback || "?";
}

function relTime(iso) {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  const diff = Date.now() - t;
  const min = 60_000, hr = 3_600_000, day = 86_400_000;
  if (diff < min) return "just now";
  if (diff < hr) return `${Math.round(diff / min)}m`;
  if (diff < day) return `${Math.round(diff / hr)}h`;
  if (diff < day * 7) return `${Math.round(diff / day)}d`;
  return new Date(iso).toLocaleDateString();
}

async function refresh() {
  const stats = await chrome.runtime.sendMessage({ type: "rolodex.getStats" });
  if (!stats) return;

  $total.textContent = stats.total ?? 0;
  $convs.textContent = stats.conversations?.length ?? 0;
  $platforms.textContent = Object.keys(stats.byPlatform || {}).length;

  if (stats.total > 0) {
    $status.textContent = "capturing";
    $status.className = "hd-status active";
  } else {
    $status.textContent = "ready — open IG or Messenger";
    $status.className = "hd-status inactive";
  }

  $ftLast.textContent = stats.lastCapturedAt
    ? `Last capture: ${relTime(stats.lastCapturedAt)} ago`
    : "No captures yet";

  if (!stats.conversations?.length) {
    $list.innerHTML = `<div class="empty">No captures yet.<br>Open Instagram or Messenger to start.</div>`;
    return;
  }

  $list.innerHTML = "";
  for (const conv of stats.conversations.slice(0, 8)) {
    const div = document.createElement("div");
    div.className = "conv";
    const platformClass = conv.platform === "instagram" ? "ig" : conv.platform === "facebook_messenger" ? "fb" : "";
    div.innerHTML = `
      <div class="conv-avatar ${platformClass}">${avatarText(conv.participant, "?")}</div>
      <div class="conv-info">
        <div class="conv-name">${conv.participant || conv.conversation_id || "Unknown"}</div>
        <div class="conv-meta">${conv.message_count} msg · ${relTime(conv.last_message_at)}</div>
      </div>
    `;
    $list.appendChild(div);
  }
}

$btnExport.addEventListener("click", async () => {
  $btnExport.textContent = "Exporting…";
  $btnExport.disabled = true;
  try {
    const result = await chrome.runtime.sendMessage({ type: "rolodex.exportJSON" });
    if (result?.ok) {
      $btnExport.textContent = `Exported ✓ (${result.message_count} msgs)`;
      setTimeout(() => {
        $btnExport.textContent = "Export to JSON";
        $btnExport.disabled = false;
      }, 1800);
    } else {
      $btnExport.textContent = "Export failed";
      setTimeout(() => {
        $btnExport.textContent = "Export to JSON";
        $btnExport.disabled = false;
      }, 1800);
    }
  } catch (e) {
    $btnExport.textContent = "Error";
    $btnExport.disabled = false;
  }
});

$btnClear.addEventListener("click", async () => {
  if (!confirm("Delete all captured messages? This cannot be undone.")) return;
  await chrome.runtime.sendMessage({ type: "rolodex.clear" });
  refresh();
});

refresh();
setInterval(refresh, 3000);
