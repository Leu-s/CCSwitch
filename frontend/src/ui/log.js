import { LOG_PER_PAGE, LOG_REFRESH_MS } from "../constants.js";
import { state } from "../state.js";
import { qs, qsa, escapeHtml, fmtTime } from "../utils.js";
import { api } from "../api.js";
import { toast } from "../toast.js";

// Each log row arrives from the backend already resolved:
//   { id, from_account_id, to_account_id, from_email, to_email, reason, triggered_at }
// Emails are nullable only when the referenced account was deleted after
// the switch was logged — we fall back to "#<id>" in that case so the row
// still renders instead of disappearing.  We DO NOT look emails up from
// state.accounts, because that map is populated by a parallel fetch and
// can race with the WS-driven log reload, which is exactly what used to
// leave "from → to" showing raw IDs until the user reloaded the page.

// Timestamp-refresh interval — stored so it can be cleared and never leaks
// a second copy if initLogListeners() were ever called more than once.
let _tsInterval = null;

function relTime(iso) {
  if (!iso) return "—";
  try {
    const diff = Date.now() - new Date(iso).getTime();
    if (diff < 60000) return "now";
    if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
    if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
    if (diff < 604800000) return `${Math.floor(diff / 86400000)}d ago`;
    return fmtTime(iso);
  } catch { return "—"; }
}

const REASON_META = {
  manual: {
    icon: `<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>`,
    label: "Manual", cls: "manual"
  },
  threshold: {
    icon: `<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>`,
    label: "Limit", cls: "threshold"
  },
  api_error: {
    icon: `<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`,
    label: "Error", cls: "api_error"
  }
};

function getReasonMeta(reason) {
  return REASON_META[reason] || { icon: "", label: reason || "—", cls: "" };
}

function renderSwitchLog() {
  const list = qs("#switch-log-list");
  const empty = qs("#switch-log-empty");
  const pagination = qs("#switch-log-pagination");
  const badge = qs("#sl-badge");
  const lastEl = qs("#sl-last");

  if (!state.logTotal) {
    list.innerHTML = "";
    empty.classList.remove("hidden");
    pagination.classList.add("hidden");
    badge.classList.add("hidden");
    lastEl.classList.add("hidden");
    return;
  }

  badge.textContent = state.logTotal;
  badge.classList.remove("hidden");
  if (state.switchLog.length > 0) {
    lastEl.textContent = `last · ${relTime(state.switchLog[0].triggered_at)}`;
    lastEl.classList.remove("hidden");
  }

  empty.classList.add("hidden");
  const totalPages = Math.ceil(state.logTotal / LOG_PER_PAGE);
  pagination.classList.toggle("hidden", totalPages <= 1);
  qs("#log-page-info").textContent = `${state.logPage + 1} / ${totalPages}`;
  qs("#log-prev-btn").disabled = state.logPage === 0;
  qs("#log-next-btn").disabled = state.logPage >= totalPages - 1;

  list.innerHTML = state.switchLog.map((row, i) => {
    const fromEmail = row.from_account_id
      ? (row.from_email || `#${row.from_account_id}`)
      : null;
    const toEmail = row.to_email || `#${row.to_account_id}`;
    const reason = String(row.reason || "").toLowerCase();
    const rm = getReasonMeta(reason);
    return `<div class="sl-item sl-item--${rm.cls}" style="animation-delay:${i * 25}ms">
      <div class="sl-item-top">
        <span class="sl-time" title="${escapeHtml(fmtTime(row.triggered_at))}">${relTime(row.triggered_at)}</span>
        <span class="sl-pill sl-pill--${rm.cls}">${rm.icon}${escapeHtml(rm.label)}</span>
      </div>
      <div class="sl-route">
        ${fromEmail
          ? `<span class="sl-email" title="${escapeHtml(fromEmail)}">${escapeHtml(fromEmail)}</span>`
          : `<span class="sl-email sl-email--none">—</span>`}
        <span class="sl-email sl-email--to" title="${escapeHtml(toEmail)}">${escapeHtml(toEmail)}</span>
      </div>
    </div>`;
  }).join("");
}

let _switchLogGen = 0; // incremented on every call; stale responses are discarded

export async function loadSwitchLog(page) {
  if (page !== undefined) state.logPage = page;
  const gen = ++_switchLogGen;
  try {
    const [countData, data] = await Promise.all([
      api("/api/accounts/log/count"),
      api(`/api/accounts/log?limit=${LOG_PER_PAGE}&offset=${state.logPage * LOG_PER_PAGE}`)
    ]);
    if (gen !== _switchLogGen) return; // a newer call superseded this one — discard
    state.logTotal = countData.total;
    state.switchLog = data;
    renderSwitchLog();
  } catch(e) { toast("Failed to load switch log", e.message, "error"); }
}

export function initLogListeners() {
  qs("#log-prev-btn").addEventListener("click", () => loadSwitchLog(state.logPage - 1));
  qs("#log-next-btn").addEventListener("click", () => loadSwitchLog(state.logPage + 1));

  // Refresh relative timestamps every LOG_REFRESH_MS without a full re-render.
  // Guard against leaking a second interval if this function is ever called again.
  if (_tsInterval) { clearInterval(_tsInterval); _tsInterval = null; }
  _tsInterval = setInterval(() => {
    if (!state.switchLog.length) return;
    const lastEl = qs("#sl-last");
    if (lastEl) lastEl.textContent = `last · ${relTime(state.switchLog[0].triggered_at)}`;
    qsa(".sl-time").forEach((el, i) => {
      if (state.switchLog[i]) el.textContent = relTime(state.switchLog[i].triggered_at);
    });
  }, LOG_REFRESH_MS);
}
