// =============================================================
// Constants
// =============================================================
const API_TIMEOUT_MS = 30000;
const WS_PING_INTERVAL_MS = 25000;
const TOAST_TTL_MS = 4000;
const TOAST_FADEOUT_MS = 300;
const MAX_TOASTS = 5;
const SLIDER_DEBOUNCE_MS = 400;
const TERMINAL_REFRESH_MS = 2000;
const _sliderDebounce = new Map(); // accountId → timer handle
const MAX_EVENT_FEED = 20;
const MAX_RECONNECT_MS = 15000;
const LOG_REFRESH_MS = 30000;
const TOKEN_EXPIRY_SOON_MS = 3600000;

// =============================================================
// State
// =============================================================
const state = {
  accounts: [],
  service: { enabled: false, active_email: null, default_account_id: null },
  switchLog: [], logPage: 0, logTotal: 0,
  sessions: [],
  monitors: [],
  currentTab: "accounts",
};

function qs(sel, root = document) { return root.querySelector(sel); }
function qsa(sel, root = document) { return Array.from(root.querySelectorAll(sel)); }

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString([], { month:"short", day:"numeric", hour:"2-digit", minute:"2-digit" });
  } catch { return iso; }
}

// Parse any timestamp format: ISO string, ms number, or seconds number
function tsToMs(ts) {
  if (!ts) return null;
  if (typeof ts === "string") { const d = new Date(ts); return isNaN(d) ? null : d.getTime(); }
  return ts > 1e12 ? ts : ts * 1000;
}

function fmtReset(ts) {
  const ms = tsToMs(ts);
  if (!ms) return "—";
  try {
    const d = new Date(ms);
    if (isNaN(d)) return "—";
    const diffH = (d - Date.now()) / 3600000;
    if (diffH < 0) {
      // Expired — still show when it expired so users know the date
      if (diffH > -24) return d.toLocaleTimeString([], { hour:"2-digit", minute:"2-digit" });
      return d.toLocaleDateString([], { month:"short", day:"numeric" });
    }
    if (diffH < 24) return d.toLocaleTimeString([], { hour:"2-digit", minute:"2-digit" });
    return d.toLocaleDateString([], { month:"short", day:"numeric" }) + " " + d.toLocaleTimeString([], { hour:"2-digit", minute:"2-digit" });
  } catch { return "—"; }
}

function fmtRelative(ts) {
  const ms = tsToMs(ts);
  if (!ms) return "";
  try {
    const diff = ms - Date.now();
    if (diff <= 0) return "expired";
    const h = Math.floor(diff / 3600000);
    const m = Math.floor((diff % 3600000) / 60000);
    if (h >= 24) {
      const d = Math.floor(h / 24);
      return `in ${d}d ${h % 24}h`;
    }
    if (h > 0) return `in ${h}h ${m}m`;
    return `in ${m}m`;
  } catch { return ""; }
}

function usageClass(pct, threshold = 95) {
  if (pct == null) return "";
  if (pct >= threshold) return "crit";
  if (pct >= threshold * 0.75) return "warn";
  return "";
}

// =============================================================
// Toast
// =============================================================
function toast(title, body = "", kind = "info", ttl = TOAST_TTL_MS) {
  const container = qs("#toast-container");
  // Cap at 5 concurrent toasts — evict the oldest if at limit
  const existing = container.querySelectorAll(".toast");
  if (existing.length >= MAX_TOASTS) existing[0].remove();
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.innerHTML = `<div class="toast-title">${escapeHtml(title)}</div>${body ? `<div class="toast-body">${escapeHtml(body)}</div>` : ""}`;
  container.appendChild(el);
  setTimeout(() => { el.classList.add("fade-out"); setTimeout(() => el.remove(), TOAST_FADEOUT_MS); }, ttl);
}

// =============================================================
// API helpers
// =============================================================
async function api(path, opts = {}) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), API_TIMEOUT_MS);
  const init = { headers: { "Content-Type": "application/json" }, ...opts, signal: controller.signal };
  if (opts.body && typeof opts.body === "object") init.body = JSON.stringify(opts.body);
  try {
    const res = await fetch(path, init);
    clearTimeout(timeoutId);
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`;
      try { const j = await res.json(); if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
      throw new Error(msg);
    }
    if (res.status === 204) return null;
    const ct = res.headers.get("content-type") || "";
    return ct.includes("application/json") ? res.json() : res.text();
  } catch (e) {
    clearTimeout(timeoutId);
    if (e.name === "AbortError") throw new Error("Request timed out");
    throw e;
  }
}

async function withLoading(btn, fn) {
  btn.classList.add("loading"); btn.disabled = true;
  try { return await fn(); }
  finally { btn.classList.remove("loading"); btn.disabled = false; }
}

// =============================================================
// Tabs
// =============================================================
qsa(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    const tab = btn.dataset.tab;
    state.currentTab = tab;
    qsa(".tab-btn").forEach(b => { const a = b.dataset.tab === tab; b.classList.toggle("active", a); b.setAttribute("aria-selected", a ? "true" : "false"); });
    qsa(".tab-panel").forEach(p => p.classList.toggle("active", p.id === "tab-" + tab));
    if (tab === "tmux") loadTmuxData();
  });
});

// =============================================================
// Service status
// =============================================================
async function loadServiceStatus() {
  try {
    const s = await api("/api/service");
    state.service = s;
    updateServiceUI(s);
  } catch (e) {
    console.warn("service status:", e);
  }
}


function updateServiceUI(s) {
  const btn = qs("#service-toggle-btn");
  const stateLabel = qs("#service-btn-state");
  const flow = qs("#service-btn-flow");
  const currentEl = qs("#service-btn-current");
  const autoRow = qs("#sl-auto-row");

  if (s.enabled) {
    btn.dataset.on = "true";
    stateLabel.textContent = "Switch: ON";
    if (s.active_email) {
      currentEl.textContent = s.active_email;
      flow.hidden = false;
    } else {
      flow.hidden = true;
    }
    if (autoRow) autoRow.style.display = "";
  } else {
    btn.dataset.on = "false";
    stateLabel.textContent = "Switch: OFF";
    flow.hidden = true;
    if (autoRow) autoRow.style.display = "none";
  }
}

// =============================================================
// Auto-switch setting
// =============================================================
async function loadAutoSwitchSetting() {
  try {
    const settings = await api("/api/settings");
    const entry = settings.find(s => s.key === "auto_switch_enabled");
    const enabled = entry ? entry.value !== "false" : true;
    const cb = qs("#auto-switch-cb");
    if (cb) cb.checked = enabled;
  } catch (e) {
    console.warn("auto-switch setting:", e);
  }
}

qs("#auto-switch-cb").addEventListener("change", async (e) => {
  const val = e.target.checked ? "true" : "false";
  try {
    await api("/api/settings/auto_switch_enabled", {
      method: "PATCH",
      body: { value: val },
    });
    toast(e.target.checked ? "Auto-switch on" : "Auto-switch off", null, "success", 2000);
  } catch (err) {
    e.target.checked = !e.target.checked; // revert
    toast("Update failed", err.message, "error");
  }
});

qs("#service-toggle-btn").addEventListener("click", async () => {
  const btn = qs("#service-toggle-btn");
  const isOn = btn.dataset.on === "true";
  await withLoading(btn, async () => {
    try {
      if (isOn) {
        const r = await api("/api/service/disable", { method: "POST" });
        toast("Service disabled", "Auto-switching stopped", "success");
      } else {
        const r = await api("/api/service/enable", { method: "POST" });
        toast("Service enabled", `Active: ${r.active_email}`, "success");
      }
      await loadServiceStatus();
      await loadAccounts();
    } catch (e) {
      toast(isOn ? "Disable failed" : "Enable failed", e.message, "error");
    }
  });
});

// =============================================================
// Accounts rendering
// =============================================================
function updateSliderFill(slider) {
  const min = Number(slider.min) || 0;
  const max = Number(slider.max) || 100;
  const pct = Math.round(((Number(slider.value) - min) / (max - min)) * 100);
  slider.style.background =
    `linear-gradient(to right, var(--accent) ${pct}%, var(--slider-bg) ${pct}%)`;
}

let pendingRenderAfterDrag = false;

function renderAccounts() {
  if (isDragging || isSavingPriorities) { pendingRenderAfterDrag = true; return; }
  pendingRenderAfterDrag = false;
  // Clear all pending slider debounce timers before re-render
  _sliderDebounce.forEach(t => clearTimeout(t));
  _sliderDebounce.clear();
  const grid = qs("#accounts-grid");
  const empty = qs("#accounts-empty");
  const countEl = qs("#accounts-count");
  if (!state.accounts.length) {
    grid.innerHTML = "";
    empty.style.display = "block";
    if (countEl) countEl.textContent = "";
    return;
  }
  empty.style.display = "none";
  if (countEl) countEl.textContent = state.accounts.length;
  grid.innerHTML = state.accounts.map((acc, i) => accountCardHtml(acc, i)).join("");
  attachCardEvents();
}

function usageBlockHtml(acc) {
  const usage = acc.usage || {};
  const threshold = acc.threshold_pct ?? 95;

  const fiveH = usage.five_hour_pct;
  const sevenD = usage.seven_day_pct;
  const err = usage.error;
  const rateLimited = usage.rate_limited;

  function usageWindow(label, pct, resetsAt) {
    const cls = usageClass(pct, threshold);
    const rel = fmtRelative(resetsAt);
    const resetStr = fmtReset(resetsAt);
    return `
      <div class="usage-window">
        <div class="usage-header">
          <span class="usage-label">${label}</span>
          <span class="usage-reset">${resetStr}${rel ? " · " + rel : ""}</span>
        </div>
        <div class="usage-bar"><div class="usage-fill ${cls}" style="width:${Math.min(100,pct)}%"></div></div>
        <div style="display:flex;justify-content:flex-end;margin-top:1px"><span class="usage-pct ${cls}">${pct.toFixed(0)}%</span></div>
      </div>`;
  }

  const fiveBlock = fiveH != null ? usageWindow("5 h window", fiveH, usage.five_hour_resets_at) : "";
  const sevenBlock = sevenD != null ? usageWindow("7 d window", sevenD, usage.seven_day_resets_at) : "";

  const hasBars = fiveBlock || sevenBlock;
  const divider = hasBars ? `<div class="usage-divider"></div>` : "";

  const errBlock = err ? `<div class="usage-error">⚠ ${escapeHtml(err)}</div>` : "";
  const rateLimitedBadge = rateLimited
    ? `<span class="usage-rate-limited">⚡ Rate limited${(fiveH != null || sevenD != null) ? " (stale data)" : ""}</span>`
    : "";
  const usageEmpty = (!hasBars && !err && !rateLimited) ? `<div class="usage-empty">No usage data yet</div>` : "";

  // Footer: subscription badge + token expiry
  const subBadge = usage.subscription_type
    ? `<span class="sub-badge">${escapeHtml(usage.subscription_type)}</span>` : "";
  const tokenExpMs = tsToMs(usage.token_expires_at);
  const tokenExpiringSoon = tokenExpMs && (tokenExpMs - Date.now()) < TOKEN_EXPIRY_SOON_MS;
  const sessionLine = tokenExpMs
    ? `<span class="session-expiry${tokenExpiringSoon ? " expiring-soon" : ""}">Token ${fmtReset(usage.token_expires_at)} · ${fmtRelative(usage.token_expires_at)}</span>` : "";
  const footer = (subBadge || sessionLine || rateLimitedBadge)
    ? `<div class="usage-footer">${subBadge}${rateLimitedBadge}${sessionLine}</div>` : "";

  return `<div class="usage-block">${fiveBlock}${sevenBlock}${divider}${errBlock}${usageEmpty}${footer}</div>`;
}

function accountCardHtml(acc, index) {
  const isActive = !!acc.is_active;
  const disabled = !acc.enabled;
  const isStale = !!acc.stale_reason;
  const isDefault = Number(acc.id) === Number(state.service.default_account_id);
  const threshold = acc.threshold_pct ?? 95;

  const staleBanner = isStale ? `
    <div class="stale-banner" title="${escapeHtml(acc.stale_reason)}">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
      <span>${escapeHtml(acc.stale_reason)} — click "Re-login" in the Add Account flow.</span>
    </div>` : "";

  return `
    <article class="account-card ${isActive?"active":""} ${disabled?"disabled":""} ${isStale?"stale":""}"
             draggable="true" data-id="${acc.id}" data-email="${escapeHtml(acc.email)}">
      ${index !== undefined ? `<span class="card-num">${String(index + 1).padStart(2, "0")}</span>` : ""}
      <div class="card-header">
        <div class="drag-handle" title="Drag to reorder">
          <svg width="12" height="20" viewBox="0 0 12 20" fill="currentColor"><circle cx="4" cy="4" r="1.5"/><circle cx="8" cy="4" r="1.5"/><circle cx="4" cy="10" r="1.5"/><circle cx="8" cy="10" r="1.5"/><circle cx="4" cy="16" r="1.5"/><circle cx="8" cy="16" r="1.5"/></svg>
        </div>
        <div class="card-identity">
          <div class="email" title="${escapeHtml(acc.email)}">${escapeHtml(acc.email)}</div>
          <div class="card-meta">
            ${isActive ? `<span class="active-pill">Active</span>` : ""}
            ${isDefault ? `<span class="default-pill">Default</span>` : ""}
            ${isStale ? `<span class="stale-pill" title="${escapeHtml(acc.stale_reason)}">Re-login needed</span>` : ""}
            <label class="toggle" title="Include in auto-switching">
              <input type="checkbox" class="enabled-toggle" data-id="${acc.id}" ${acc.enabled?"checked":""} />
              <span class="track"></span>
              <span>Auto switch</span>
            </label>
          </div>
        </div>
        <div class="card-controls">
          <button class="btn icon star set-default-btn ${isDefault?"active":""}" data-id="${acc.id}" ${isDefault?"disabled":""} title="${isDefault?"Currently the starting account on Enable":"Set as starting account on Enable"}">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="${isDefault?"currentColor":"none"}" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
          </button>
          <button class="btn icon danger delete-btn" data-id="${acc.id}" ${isActive?"disabled":""} title="${isActive?"Cannot delete the active account":"Delete account"}">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14H6L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M9 6V4h6v2"></path></svg>
          </button>
        </div>
      </div>
      ${staleBanner}
      ${usageBlockHtml(acc)}
      <div class="card-footer">
        <div class="threshold-row">
          <span class="label">Switch threshold</span>
          <div class="slider-wrap">
            <input type="range" class="threshold-slider" data-id="${acc.id}" min="0" max="100" step="1" value="${threshold}" draggable="false" onpointerdown="event.stopPropagation()" />
            <div class="slider-ticks">${Array.from({length:21},(_,i)=>`<span class="tick ${i%2===0?"major":"minor"}"></span>`).join("")}</div>
          </div>
          <span class="threshold-value" id="tval-${acc.id}">${threshold.toFixed(0)}%</span>
        </div>
        <button class="btn primary ${isActive?"outlined":""} switch-btn" data-id="${acc.id}" ${(isActive||isStale)?"disabled":""} title="${isStale?"Credentials invalid — re-login this account first":(!isActive && disabled ? "Manual switch — available even when excluded from auto-switching":"")}">
          ${isActive ? "Currently active" : (isStale ? "Credentials invalid" : "Switch to")}
        </button>
      </div>
    </article>`;
}

function attachCardEvents() {
  qsa(".account-card").forEach(card => attachDragHandlers(card));

  qsa(".enabled-toggle").forEach(cb => {
    cb.addEventListener("change", async () => {
      const id = cb.dataset.id; const enabled = cb.checked;
      cb.disabled = true;
      try {
        await api(`/api/accounts/${id}`, {method:"PATCH", body:{enabled}});
        const acc = state.accounts.find(a => String(a.id)===id);
        if (acc) acc.enabled = enabled;
        const cardEl = cb.closest(".account-card");
        cardEl.classList.toggle("disabled", !enabled);
        // Keep switch-btn tooltip accurate after toggle — no full re-render needed
        const switchBtn = cardEl.querySelector(".switch-btn");
        if (switchBtn && !switchBtn.disabled) {
          switchBtn.title = !enabled
            ? "Manual switch — available even when excluded from auto-switching" : "";
        }
        toast(enabled ? "Enabled" : "Disabled", null, "success", 2000);
      } catch(e) { cb.checked = !enabled; toast("Update failed", e.message, "error"); }
      finally { cb.disabled = false; }
    });
  });

  qsa(".threshold-slider").forEach(slider => {
    const id = slider.dataset.id;
    const valEl = qs(`#tval-${id}`);
    slider.addEventListener("input", () => {
      if (valEl) valEl.textContent = slider.value + "%";
      updateSliderFill(slider);
    });
    updateSliderFill(slider); // initialise fill on render
    slider.addEventListener("change", () => {
      const val = Number(slider.value);
      const prev = _sliderDebounce.get(id);
      if (prev) clearTimeout(prev);
      _sliderDebounce.set(id, setTimeout(async () => {
        _sliderDebounce.delete(id);
        try {
          await api(`/api/accounts/${id}`, {method:"PATCH", body:{threshold_pct:val}});
          const acc = state.accounts.find(a => String(a.id)===id);
          if (acc) acc.threshold_pct = val;
          toast("Threshold saved", `${val}%`, "success", 1800);
        } catch(e) { toast("Update failed", e.message, "error"); }
      }, SLIDER_DEBOUNCE_MS));
    });
  });

  qsa(".set-default-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      const id = Number(btn.dataset.id);
      if (Number(state.service.default_account_id) === id) return;
      let changed = false;
      await withLoading(btn, async () => {
        try {
          const r = await api(`/api/service/default-account?account_id=${id}`, {method:"PATCH"});
          state.service.default_account_id = id;
          toast("Default set", r.email, "success", 2500);
          changed = true;
        } catch(e) { toast("Failed", e.message, "error"); }
      });
      // Re-render outside withLoading so the new DOM replaces the correct node
      if (changed) renderAccounts();
    });
  });

  qsa(".switch-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (btn.disabled) return;
      if (isSwitching) return;
      isSwitching = true;
      const id = btn.dataset.id;
      let ok = false;
      try {
        await withLoading(btn, async () => {
          try {
            await api(`/api/accounts/${id}/switch`, {method:"POST"});
            toast("Switch requested", "Waiting for confirmation…", "success");
            ok = true;
          } catch(e) { toast("Switch failed", e.message, "error"); }
        });
        // Load outside withLoading to avoid orphaned-button cleanup on detached node
        if (ok) { await loadAccounts(); await loadServiceStatus(); }
      } finally {
        isSwitching = false;
      }
    });
  });

  qsa(".delete-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.id;
      const acc = state.accounts.find(a => String(a.id)===id);
      if (!acc || !confirm(`Delete account ${acc.email}?\n\nThe isolated config directory will remain on disk.`)) return;
      let ok = false;
      await withLoading(btn, async () => {
        try {
          await api(`/api/accounts/${id}`, {method:"DELETE"});
          toast("Deleted", acc.email, "success");
          ok = true;
        } catch(e) { toast("Delete failed", e.message, "error"); }
      });
      if (ok) await loadAccounts();
    });
  });
}

// =============================================================
// Drag & drop reorder
// =============================================================
let dragSrc = null;
let isDragging = false;
let isSavingPriorities = false;
let isSwitching = false;

function attachDragHandlers(card) {
  let fromHandle = false;
  card.addEventListener("mousedown", e => { fromHandle = !!e.target.closest(".drag-handle"); });
  card.addEventListener("dragstart", e => {
    if (!fromHandle) { e.preventDefault(); return; }
    dragSrc = card; isDragging = true; card.classList.add("dragging"); e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", card.dataset.id); } catch {}
  });
  card.addEventListener("dragend", () => {
    card.classList.remove("dragging");
    qsa(".account-card").forEach(c => c.classList.remove("drop-above","drop-below"));
    dragSrc = null; isDragging = false;
    // Flush any render that was deferred because isDragging was true
    if (pendingRenderAfterDrag) renderAccounts();
  });
  card.addEventListener("dragover", e => {
    if (!dragSrc || dragSrc === card) return; e.preventDefault();
    const rect = card.getBoundingClientRect();
    const isAbove = e.clientY < rect.top + rect.height / 2;
    qsa(".account-card").forEach(c => c.classList.remove("drop-above","drop-below"));
    card.classList.add(isAbove ? "drop-above" : "drop-below");
  });
  // Only clear drop indicator when the pointer truly leaves this card
  card.addEventListener("dragleave", e => {
    if (!card.contains(e.relatedTarget)) card.classList.remove("drop-above","drop-below");
  });
  card.addEventListener("drop", async e => {
    e.preventDefault();
    if (!dragSrc || dragSrc === card) return;
    const rect = card.getBoundingClientRect();
    const dropBefore = e.clientY < rect.top + rect.height / 2;
    const grid = qs("#accounts-grid");
    if (dropBefore) grid.insertBefore(dragSrc, card);
    else grid.insertBefore(dragSrc, card.nextSibling);
    qsa(".account-card").forEach(c => c.classList.remove("drop-above","drop-below"));
    const newOrder = qsa(".account-card").map(c => Number(c.dataset.id));
    const promises = newOrder.map((id, i) => {
      const acc = state.accounts.find(a => a.id === id);
      if (acc && acc.priority !== i) return api(`/api/accounts/${id}`, {method:"PATCH", body:{priority:i}}).then(() => { acc.priority = i; });
      return null;
    }).filter(Boolean);
    isSavingPriorities = true;
    try {
      await Promise.all(promises);
      state.accounts.sort((a, b) => a.priority - b.priority);
      toast("Reordered", "Priority saved", "success", 1800);
    } catch(err) {
      toast("Reorder failed", err.message, "error");
      await loadAccounts();
    } finally {
      isSavingPriorities = false;
      if (pendingRenderAfterDrag) {
        pendingRenderAfterDrag = false;
        renderAccounts();
      }
    }
  });
}

// =============================================================
// Data loaders
// =============================================================
async function loadAccounts() {
  const loadingEl = qs("#accounts-loading");
  const errorEl = qs("#accounts-error");
  try {
    const data = await api("/api/accounts");
    state.accounts = data;
    if (errorEl) errorEl.style.display = "none";
    renderAccounts();
  } catch(e) {
    toast("Load accounts failed", e.message, "error");
    if (errorEl) {
      errorEl.textContent = `Failed to load accounts: ${e.message}`;
      errorEl.style.display = "block";
    }
  } finally {
    if (loadingEl) loadingEl.style.display = "none";
  }
}


const LOG_PER_PAGE = 10;

async function loadSwitchLog(page) {
  if (page !== undefined) state.logPage = page;
  try {
    const [countData, data] = await Promise.all([
      api("/api/accounts/log/count"),
      api(`/api/accounts/log?limit=${LOG_PER_PAGE}&offset=${state.logPage * LOG_PER_PAGE}`)
    ]);
    state.logTotal = countData.total;
    state.switchLog = data;
    renderSwitchLog();
  } catch(e) { console.warn("loadSwitchLog error", e); toast("Failed to load switch log", e.message, "error"); }
}

// ── Switch Log helpers ─────────────────────────────────────────────────────

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
    empty.style.display = "flex";
    pagination.style.display = "none";
    badge.style.display = "none";
    lastEl.style.display = "none";
    return;
  }

  badge.textContent = state.logTotal;
  badge.style.display = "";
  if (state.switchLog.length > 0) {
    lastEl.textContent = `last · ${relTime(state.switchLog[0].triggered_at)}`;
    lastEl.style.display = "";
  }

  empty.style.display = "none";
  const totalPages = Math.ceil(state.logTotal / LOG_PER_PAGE);
  pagination.style.display = totalPages > 1 ? "flex" : "none";
  qs("#log-page-info").textContent = `${state.logPage + 1} / ${totalPages}`;
  qs("#log-prev-btn").disabled = state.logPage === 0;
  qs("#log-next-btn").disabled = state.logPage >= totalPages - 1;

  const byId = new Map(state.accounts.map(a => [a.id, a.email]));

  list.innerHTML = state.switchLog.map((row, i) => {
    const fromEmail = row.from_account_id ? (byId.get(row.from_account_id) || `#${row.from_account_id}`) : null;
    const toEmail = byId.get(row.to_account_id) || `#${row.to_account_id}`;
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
        <span class="sl-route-sep">›</span>
        <span class="sl-email sl-email--to" title="${escapeHtml(toEmail)}">${escapeHtml(toEmail)}</span>
      </div>
    </div>`;
  }).join("");
}

function prependSwitchLogRow() {
  loadSwitchLog(0);
}

// =============================================================
// Settings bindings
// =============================================================


qs("#log-prev-btn").addEventListener("click", () => loadSwitchLog(state.logPage - 1));

// Refresh relative timestamps every 30s without a full re-render
const _tsInterval = setInterval(() => {
  if (!state.switchLog.length) return;
  const lastEl = qs("#sl-last");
  if (lastEl) lastEl.textContent = `last · ${relTime(state.switchLog[0].triggered_at)}`;
  const times = qsa(".sl-time");
  state.switchLog.forEach((row, i) => {
    if (times[i]) times[i].textContent = relTime(row.triggered_at);
  });
}, LOG_REFRESH_MS);
qs("#log-next-btn").addEventListener("click", () => loadSwitchLog(state.logPage + 1));

// =============================================================
// Add Account modal (multi-step login flow)
// =============================================================
const addModal = qs("#add-modal");
let loginSession = null;
let addTermInterval = null;

function openAddModal() {
  addModal.classList.add("open");
  // Reset to step 1
  qs("#add-step-1").style.display = "";
  qs("#add-step-2").style.display = "none";
  qs("#add-step-3").style.display = "none";
  // Reset stale state from previous session
  const termInput = qs("#add-term-input");
  if (termInput) termInput.value = "";
  const termOutput = qs("#add-term-output");
  if (termOutput) termOutput.textContent = "";
  const termTarget = qs("#add-term-target");
  if (termTarget) termTarget.textContent = "—";
}

function closeAddModal() {
  addModal.classList.remove("open");
  if (addTermInterval) { clearInterval(addTermInterval); addTermInterval = null; }
  // Cancel dangling session
  if (loginSession) {
    api(`/api/accounts/cancel-login?session_id=${loginSession.session_id}`, {method:"DELETE"}).catch(e => console.warn("cancel-login:", e));
    loginSession = null;
  }
}

qs("#add-account-btn").addEventListener("click", openAddModal);
qs("#add-modal-close").addEventListener("click", closeAddModal);
addModal.addEventListener("click", e => { if (e.target===addModal) closeAddModal(); });

qs("#open-terminal-btn").addEventListener("click", async () => {
  const btn = qs("#open-terminal-btn");
  await withLoading(btn, async () => {
    try {
      const result = await api("/api/accounts/start-login", {method:"POST"});
      loginSession = result;
      qs("#add-term-target").textContent = result.pane_target;
      qs("#add-step-1").style.display = "none";
      qs("#add-step-2").style.display = "";
      // Poll terminal output
      refreshAddTerminal();
      if (addTermInterval) { clearInterval(addTermInterval); addTermInterval = null; }
      addTermInterval = setInterval(refreshAddTerminal, TERMINAL_REFRESH_MS);
    } catch(e) { toast("Failed to open terminal", e.message, "error"); }
  });
});

async function fetchTerminalCapture(target, outputEl, liveEl) {
  if (!target) return;
  try {
    const d = await api(`/api/tmux/capture?target=${encodeURIComponent(target)}&lines=100`);
    const atBottom = outputEl.scrollHeight - outputEl.scrollTop <= outputEl.clientHeight + 5;
    outputEl.textContent = d.output || "";
    if (atBottom) outputEl.scrollTop = outputEl.scrollHeight;
    if (liveEl) { liveEl.className = "terminal-live"; liveEl.title = "Live"; liveEl.textContent = "live"; }
  } catch (e) {
    outputEl.textContent = "Error fetching terminal output: " + String(e);
    if (liveEl) { liveEl.className = "terminal-live error"; liveEl.title = String(e); liveEl.textContent = "error"; }
  }
}

async function refreshAddTerminal() {
  if (!loginSession) return;
  const out = qs("#add-term-output");
  await fetchTerminalCapture(loginSession.pane_target, out, null);
  // Always scroll to bottom for the add-account terminal
  out.scrollTop = out.scrollHeight;
}

qs("#add-term-send-form").addEventListener("submit", async e => {
  e.preventDefault();
  const input = qs("#add-term-input");
  const text = input.value; if (!loginSession) return;
  input.value = "";
  try {
    await api("/api/tmux/send", {method:"POST", body:{target:loginSession.pane_target, text}});
    setTimeout(refreshAddTerminal, 300);
  } catch(err) { toast("Send failed", err.message, "error"); }
});

qs("#add-cancel-btn").addEventListener("click", closeAddModal);

qs("#add-verify-btn").addEventListener("click", async () => {
  if (!loginSession) return;
  const btn = qs("#add-verify-btn");
  await withLoading(btn, async () => {
    try {
      const result = await api(`/api/accounts/verify-login?session_id=${encodeURIComponent(loginSession.session_id)}`, {method:"POST"});
      if (result.success) {
        clearInterval(addTermInterval); addTermInterval = null;
        qs("#add-result-email").textContent = result.email || "Unknown email";
        qs("#add-step-2").style.display = "none";
        qs("#add-step-3").style.display = "";
        loginSession = null;
        await loadAccounts();
        await loadServiceStatus();
      } else {
        toast("Verification failed", result.error, "error", 6000);
      }
    } catch(e) { toast("Verification failed", e.message, "error"); }
  });
});

qs("#add-done-btn").addEventListener("click", () => { addModal.classList.remove("open"); });

// =============================================================
// tmux
// =============================================================
async function loadTmuxData() { await Promise.all([loadSessions(), loadMonitors()]); }

async function loadSessions() {
  qs("#sessions-count").textContent = "…";
  qs("#sessions-list").innerHTML = `<div class="empty-state" style="margin:10px;color:var(--text-mute)">Loading…</div>`;
  try {
    const data = await api("/api/tmux/sessions");
    state.sessions = data || [];
    renderSessions();
  } catch(e) {
    qs("#sessions-count").textContent = "—";
    qs("#sessions-list").innerHTML = `<div class="empty-state" style="margin:10px;color:var(--danger)">Failed to load sessions.</div>`;
    toast("Load sessions failed",e.message,"error");
  }
}

function renderSessions() {
  const list = qs("#sessions-list");
  qs("#sessions-count").textContent = state.sessions.length;
  if (!state.sessions.length) { list.innerHTML = `<div class="empty-state" style="margin:10px;">No tmux sessions discovered.</div>`; return; }
  list.innerHTML = state.sessions.map(p => `
    <div class="pane-item${terminalTarget===p.target?" selected":""}" data-target="${escapeHtml(p.target)}" title="Click to preview">
      <div class="pane-meta">
        <div class="pane-target">${escapeHtml(p.target)}</div>
        <div class="pane-cmd" title="${escapeHtml(p.command||"")}">${escapeHtml(p.command||"—")}</div>
      </div>
      <button type="button" class="pane-monitor-btn" data-target="${escapeHtml(p.target)}" data-cmd="${escapeHtml(p.command||"")}" title="Add monitor for this pane">+ Monitor</button>
    </div>`).join("");
}

// ── Bottom terminal panel ──────────────────────────────────────────────────
let terminalTarget = null;
let captureInterval = null;

function openTerminal(target) {
  terminalTarget = target;
  qs("#terminal-target").textContent = target;
  const session = target.split(":")[0];
  qs("#terminal-attach-cmd").textContent = `tmux attach -t ${session}`;
  const panel = qs("#terminal-panel");
  panel.classList.add("visible");
  const panelH = parseInt(panel.style.height) || panel.offsetHeight;
  qs("main").style.paddingBottom = (panelH + 20) + "px";
  qsa(".pane-item").forEach(el => el.classList.toggle("selected", el.dataset.target===target));
  qs("#terminal-input").focus();
  refreshCapture();
  if (captureInterval) clearInterval(captureInterval);
  captureInterval = setInterval(refreshCapture, TERMINAL_REFRESH_MS);
}

function closeTerminal() {
  terminalTarget = null;
  if (captureInterval) { clearInterval(captureInterval); captureInterval = null; }
  qs("#terminal-panel").classList.remove("visible");
  qs("main").style.paddingBottom = "";
  qsa(".pane-item.selected").forEach(el => el.classList.remove("selected"));
}

window.addEventListener("beforeunload", () => {
  if (captureInterval) { clearInterval(captureInterval); captureInterval = null; }
  if (addTermInterval) { clearInterval(addTermInterval); addTermInterval = null; }
  clearInterval(_tsInterval);
});

function prefillMonitorForm(target, cmd) {
  const nameInput = qs('[name="name"]', qs("#add-monitor-form"));
  const typeSelect = qs("#pattern-type-select");
  const patternInput = qs('[name="pattern"]', qs("#add-monitor-form"));
  typeSelect.value = "manual";
  patternInput.value = target;
  patternInput.setCustomValidity("");
  updatePatternTypeHint();
  if (!nameInput.value.trim()) {
    const label = cmd && cmd !== "—" ? cmd.replace(/^[-\s]+/, "").slice(0, 28) : target;
    nameInput.value = label;
  }
  qs("#add-monitor-form").scrollIntoView({ behavior: "smooth", block: "nearest" });
  setTimeout(() => nameInput.focus(), 400);
}

function updatePatternTypeHint() {
  const sel = qs("#pattern-type-select");
  const hint = qs("#pattern-type-hint");
  if (!sel || !hint) return;
  hint.textContent = sel.value === "manual"
    ? "Exact tmux pane target (e.g. main:0.0)"
    : "Regex matched against pane target strings";
}

async function refreshCapture() {
  await fetchTerminalCapture(terminalTarget, qs("#terminal-output"), qs("#terminal-live"));
}

qs("#terminal-close-btn").addEventListener("click", closeTerminal);

// ── Terminal panel drag-to-resize ─────────────────────────────────────────
(function() {
  const panel = qs("#terminal-panel");
  const handle = qs("#terminal-resize-handle");
  let dragging = false, startY = 0, startH = 0;
  handle.addEventListener("mousedown", e => {
    e.preventDefault();
    dragging = true; startY = e.clientY; startH = panel.offsetHeight;
    handle.classList.add("dragging");
    document.body.style.userSelect = "none";
    document.body.style.cursor = "ns-resize";
    qs("main").style.transition = "none";

    function onMove(e) {
      if (!dragging) return;
      const newH = Math.min(Math.max(startH + (startY - e.clientY), 120), window.innerHeight * 0.85);
      panel.style.height = newH + "px";
      qs("main").style.paddingBottom = (newH + 20) + "px";
    }
    function onUp() {
      dragging = false;
      handle.classList.remove("dragging");
      document.body.style.userSelect = "";
      document.body.style.cursor = "";
      qs("main").style.transition = "";
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
})();

qs("#terminal-send-form").addEventListener("submit", async e => {
  e.preventDefault();
  const input = qs("#terminal-input");
  const text = input.value; if (!terminalTarget) return;
  input.value = "";
  try { await api("/api/tmux/send",{method:"POST",body:{target:terminalTarget,text}}); setTimeout(refreshCapture,300); }
  catch(err) { toast("Send failed",err.message,"error"); }
});

async function loadMonitors() {
  qs("#monitors-list").innerHTML = `<div class="empty-state" style="margin:10px;color:var(--text-mute)">Loading…</div>`;
  try {
    const data = await api("/api/tmux/monitors");
    state.monitors = data || [];
    renderMonitors();
  } catch(e) {
    qs("#monitors-list").innerHTML = `<div class="empty-state" style="margin:10px;color:var(--danger)">Failed to load monitors.</div>`;
    toast("Load monitors failed", e.message, "error");
  }
}

function renderMonitors() {
  const list = qs("#monitors-list");
  qs("#monitors-count").textContent = state.monitors.length;
  if (!state.monitors.length) { list.innerHTML = `<div class="empty-state" style="margin:10px;">No monitors configured.</div>`; return; }
  list.innerHTML = state.monitors.map(m => `
    <div class="monitor-row${m.enabled ? "" : " disabled"}" data-id="${m.id}">
      <div class="monitor-info">
        <div class="monitor-name">${escapeHtml(m.name)}</div>
        <div class="monitor-pattern" title="${escapeHtml(m.pattern)}">${escapeHtml(m.pattern)}</div>
      </div>
      <span class="badge ${m.pattern_type}">${escapeHtml(m.pattern_type)}</span>
      <label class="toggle"><input type="checkbox" class="monitor-enabled" data-id="${m.id}" ${m.enabled?"checked":""}/><span class="track"></span></label>
      <button type="button" class="btn icon monitor-edit" data-id="${m.id}" title="Edit monitor">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
      </button>
      <div class="monitor-del-wrap">
        <button type="button" class="btn icon danger monitor-del" data-id="${m.id}" title="Delete monitor">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14H6L5 6"></path></svg>
        </button>
        <div class="monitor-del-confirm" style="display:none">
          <button type="button" class="btn danger sm monitor-del-ok" data-id="${m.id}">Delete?</button>
          <button type="button" class="btn sm monitor-del-cancel">Cancel</button>
        </div>
      </div>
    </div>`).join("");
}

// Delegated listener for #sessions-list — attached ONCE, handles all renders.
qs("#sessions-list").addEventListener("click", e => {
    const monitorBtn = e.target.closest(".pane-monitor-btn");
    if (monitorBtn) {
        e.stopPropagation();
        prefillMonitorForm(monitorBtn.dataset.target, monitorBtn.dataset.cmd);
        return;
    }
    const paneItem = e.target.closest(".pane-item[data-target]");
    if (paneItem) openTerminal(paneItem.dataset.target);
});

// Delegated listeners for #monitors-list — attached ONCE, handle all renders.
// Per-row state (delete confirm timer) is stored in a WeakMap keyed on the wrap element.
const _monitorDelState = new WeakMap(); // wrap → { timer }

qs("#monitors-list").addEventListener("change", async e => {
  const cb = e.target.closest(".monitor-enabled");
  if (!cb) return;
  const id = cb.dataset.id;
  const m = state.monitors.find(x => String(x.id) === id);
  if (!m) return;
  try {
    await api(`/api/tmux/monitors/${id}`, {method:"PATCH", body:{name:m.name, pattern_type:m.pattern_type, pattern:m.pattern, enabled:cb.checked}});
    m.enabled = cb.checked;
    cb.closest(".monitor-row").classList.toggle("disabled", !cb.checked);
    toast(cb.checked ? "Monitor enabled" : "Monitor disabled", m.name, "success", 2000);
  } catch(e) { cb.checked = !cb.checked; toast("Update failed", e.message, "error"); }
});

qs("#monitors-list").addEventListener("click", async e => {
  // ── Edit button ───────────────────────────────────────────────
  const editBtn = e.target.closest(".monitor-edit");
  if (editBtn) {
    if (document.querySelector(".monitor-row.editing")) return;
    const id = editBtn.dataset.id;
    const m = state.monitors.find(x => String(x.id) === id);
    if (!m) return;
    const row = editBtn.closest(".monitor-row");
    row.innerHTML = `
      <div class="monitor-edit-fields">
        <input class="input monitor-edit-name" value="${escapeHtml(m.name)}" placeholder="Name" aria-label="Monitor name" />
        <select class="select monitor-edit-type" aria-label="Pattern type">
          <option value="manual"${m.pattern_type==="manual"?" selected":""}>manual</option>
          <option value="regex"${m.pattern_type==="regex"?" selected":""}>regex</option>
        </select>
        <input class="input monitor-edit-pattern" value="${escapeHtml(m.pattern)}" placeholder="Pattern" aria-label="Monitor pattern" />
      </div>
      <button type="button" class="btn primary icon monitor-save" title="Save">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>
      </button>
      <button type="button" class="btn icon monitor-cancel" title="Cancel">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>`;
    row.classList.add("editing");
    row.querySelector(".monitor-edit-name").focus();

    row.querySelectorAll(".monitor-edit-name,.monitor-edit-pattern,.monitor-edit-type").forEach(el => {
      el.addEventListener("keydown", async ev => {
        if (ev.key === "Enter") { ev.preventDefault(); row.querySelector(".monitor-save").click(); }
        if (ev.key === "Escape") { await loadMonitors(); }
      });
    });

    row.querySelector(".monitor-cancel").addEventListener("click", async () => { await loadMonitors(); });

    const saveBtn = row.querySelector(".monitor-save");
    saveBtn.addEventListener("click", async () => {
      const name = row.querySelector(".monitor-edit-name").value.trim();
      const pattern_type = row.querySelector(".monitor-edit-type").value;
      const pattern = row.querySelector(".monitor-edit-pattern").value.trim();
      if (!name || !pattern) {
        if (!name) row.querySelector(".monitor-edit-name").classList.add("error");
        if (!pattern) row.querySelector(".monitor-edit-pattern").classList.add("error");
        return;
      }
      await withLoading(saveBtn, async () => {
        try {
          await api(`/api/tmux/monitors/${id}`, {method:"PATCH", body:{name, pattern_type, pattern, enabled:m.enabled}});
          toast("Saved", name, "success", 2000);
          await loadMonitors();
        } catch(e) { toast("Save failed", e.message, "error"); }
      });
    });

    row.querySelector(".monitor-edit-name").addEventListener("input", function() { this.classList.remove("error"); });
    row.querySelector(".monitor-edit-pattern").addEventListener("input", function() { this.classList.remove("error"); });
    return;
  }

  // ── Delete (trash) button — show confirm strip ────────────────
  const trashBtn = e.target.closest(".monitor-del:not(.monitor-del-ok)");
  if (trashBtn && !trashBtn.classList.contains("monitor-del-ok")) {
    if (document.querySelector(".monitor-row.editing")) return;
    const wrap = trashBtn.closest(".monitor-del-wrap");
    const confirmStrip = wrap.querySelector(".monitor-del-confirm");
    let state_ = _monitorDelState.get(wrap);
    if (!state_) { state_ = { timer: null }; _monitorDelState.set(wrap, state_); }
    function hideConfirm() {
      clearTimeout(state_.timer);
      trashBtn.style.display = "";
      confirmStrip.style.display = "none";
    }
    trashBtn.style.display = "none";
    confirmStrip.style.display = "flex";
    clearTimeout(state_.timer);
    state_.timer = setTimeout(hideConfirm, 4000);
    // Store hideConfirm so cancel can reach it
    state_.hideConfirm = hideConfirm;
    return;
  }

  // ── Delete cancel ─────────────────────────────────────────────
  const cancelBtn = e.target.closest(".monitor-del-cancel");
  if (cancelBtn) {
    const wrap = cancelBtn.closest(".monitor-del-wrap");
    const s = _monitorDelState.get(wrap);
    if (s && s.hideConfirm) s.hideConfirm();
    return;
  }

  // ── Delete confirm (ok) ───────────────────────────────────────
  const okBtn = e.target.closest(".monitor-del-ok");
  if (okBtn) {
    const wrap = okBtn.closest(".monitor-del-wrap");
    const id = okBtn.dataset.id;
    const m = state.monitors.find(x => String(x.id) === id);
    const s = _monitorDelState.get(wrap);
    if (s) clearTimeout(s.timer);
    const trashBtnInner = wrap.querySelector(".monitor-del");
    if (trashBtnInner) trashBtnInner.disabled = true;
    await withLoading(okBtn, async () => {
      try {
        await api(`/api/tmux/monitors/${id}`, {method:"DELETE"});
        toast("Deleted", m ? m.name : id, "success", 2000);
        await loadMonitors();
      } catch(e) {
        if (s && s.hideConfirm) s.hideConfirm();
        if (trashBtnInner) trashBtnInner.disabled = false;
        toast("Delete failed", e.message, "error");
      }
    });
    return;
  }
});

qs("#refresh-sessions-btn").addEventListener("click", async () => await withLoading(qs("#refresh-sessions-btn"), loadSessions));

// Init hint and update on change
updatePatternTypeHint();
qs("#pattern-type-select").addEventListener("change", updatePatternTypeHint);
qs('#add-monitor-form [name="pattern"]').addEventListener("input", function() { this.setCustomValidity(""); });

qs("#add-monitor-form").addEventListener("submit", async e => {
  e.preventDefault();
  const form = e.currentTarget; const fd = new FormData(form);
  const payload = { name:fd.get("name").trim(), pattern_type:fd.get("pattern_type"), pattern:fd.get("pattern").trim(), enabled:true };
  if (!payload.name||!payload.pattern) return;
  const patternInput = form.querySelector('[name="pattern"]');
  if (payload.pattern_type === "regex") {
    try { new RegExp(payload.pattern); }
    catch(err) {
      patternInput.setCustomValidity("Invalid regex: " + err.message);
      patternInput.reportValidity();
      return;
    }
  }
  patternInput.setCustomValidity("");
  if (payload.pattern_type === "manual" && state.sessions.length > 0) {
    if (!state.sessions.some(p => p.target === payload.pattern)) {
      patternInput.setCustomValidity("No known session with this target. Known: " + state.sessions.map(p=>p.target).join(", "));
      patternInput.reportValidity();
      return;
    }
  }
  const btn = form.querySelector("button[type=submit]");
  await withLoading(btn, async () => {
    try { await api("/api/tmux/monitors",{method:"POST",body:payload}); form.reset(); updatePatternTypeHint(); toast("Monitor added",payload.name,"success"); await loadMonitors(); }
    catch(err) { toast("Add failed",err.message,"error"); }
  });
});

// =============================================================
// Event feed
// =============================================================
function prependEvent(msg) {
  const feed = qs("#event-feed");
  qs("#event-feed-empty").style.display = "none";
  const status = String(msg.status||"").toLowerCase();
  const badgeClass = ["success","failed","uncertain"].includes(status)?status:"uncertain";
  const monitor = (state.monitors || []).find(m => String(m.id) === String(msg.monitor_id));
  const monitorLabel = monitor ? monitor.name : (msg.monitor_id ? String(msg.monitor_id) : null);
  const card = document.createElement("div");
  card.className = "event-card";
  const capId = "cap-" + Math.random().toString(36).slice(2,9);
  card.innerHTML = `
    <div class="event-head">
      ${monitorLabel ? `<span class="event-monitor">${escapeHtml(monitorLabel)}</span>` : ""}
      <span class="event-target">${escapeHtml(msg.target||"—")}</span>
      <span class="badge ${badgeClass}">${escapeHtml(msg.status||"—")}</span>
      <span class="event-time">${escapeHtml(fmtTime(new Date().toISOString()))}</span>
    </div>
    <div class="event-explanation">${escapeHtml(msg.explanation||"")}</div>
    <button class="event-toggle" data-target="${capId}">Show raw capture ▾</button>
    <pre class="event-capture" id="${capId}">${escapeHtml(msg.capture||"")}</pre>`;
  feed.prepend(card);
  card.querySelector(".event-toggle").addEventListener("click", e => {
    const pre = card.querySelector(".event-capture");
    const open = pre.classList.toggle("open");
    e.currentTarget.textContent = open?"Hide raw capture ▴":"Show raw capture ▾";
  });
  while (feed.children.length > MAX_EVENT_FEED) feed.removeChild(feed.lastChild);
  const countEl = qs("#event-feed-count");
  countEl.textContent = feed.children.length;
  countEl.style.display = "";
}

        // ── Shell setup warning ─────────────────────────────────────────────────
        async function checkShellStatus() {
            try {
                const { active_file_exists, shell_configured } = await api('/api/settings/shell-status');
                const warn = document.getElementById('shell-warn');
                const desc = document.getElementById('shell-warn-desc');
                if (active_file_exists && shell_configured) {
                    warn.style.display = 'none';
                } else {
                    desc.textContent = shell_configured
                        ? 'The active account file (~/.claude-multi/active) has not been created yet. Switch to an account or restart the server to create it.'
                        : 'Your shell is not configured to use CLAUDE_CONFIG_DIR. Without this, new terminal sessions may open with the wrong Claude account.';
                    warn.style.display = '';
                }
            } catch { /* ignore */ }
        }
        checkShellStatus();

        // ── Shell tip: apply to shell rc files ─────────────────────────────────
        document.getElementById('shell-tip-apply-btn').addEventListener('click', async () => {
            const btn = document.getElementById('shell-tip-apply-btn');
            const resultEl = document.getElementById('shell-tip-apply-result');
            btn.disabled = true;
            btn.textContent = 'Applying…';
            resultEl.textContent = '';
            resultEl.style.color = '';
            try {
                const data = await api('/api/settings/setup-shell', { method: 'POST' });
                const labels = { applied: '✓ applied', already_configured: 'already configured' };
                const parts = Object.entries(data.results)
                    .filter(([, v]) => v !== 'not_found')
                    .map(([k, v]) => `~/${k}: ${labels[v] ?? v}`);
                resultEl.textContent = parts.length ? parts.join(' • ') : 'No .zshrc or .bashrc found.';
                const values = Object.values(data.results);
                const anyError = values.some(v => String(v).startsWith('error:'));
                const allMissing = values.every(v => v === 'not_found');
                if (anyError || allMissing) {
                    if (anyError) resultEl.style.color = 'var(--danger)';
                    btn.textContent = 'Apply to .zshrc / .bashrc automatically';
                    btn.disabled = false;
                } else {
                    const anyApplied = values.includes('applied');
                    btn.textContent = anyApplied ? 'Applied!' : 'Already applied ✓';
                    btn.disabled = true;
                    checkShellStatus(); // re-check — may hide the warning now
                }
            } catch (e) {
                resultEl.textContent = 'Error: ' + e.message;
                resultEl.style.color = 'var(--danger)';
                btn.textContent = 'Apply to .zshrc / .bashrc automatically';
                btn.disabled = false;
            }
        });

        // ── Shell tip copy button ───────────────────────────────────────────────
        document.getElementById('shell-tip-copy-btn').addEventListener('click', async () => {
            const btn = document.getElementById('shell-tip-copy-btn');
            const text = document.getElementById('shell-tip-cmd').textContent.trim();
            try {
                await navigator.clipboard.writeText(text);
                btn.textContent = 'Copied!';
                btn.classList.add('copied');
                setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
            } catch {
                const range = document.createRange();
                range.selectNodeContents(document.getElementById('shell-tip-cmd'));
                window.getSelection().removeAllRanges();
                window.getSelection().addRange(range);
            }
        });

// =============================================================
// WebSocket
// =============================================================
let ws = null;
let wsReconnectAttempts = 0;
let _lastSeq = 0; // last server seq received; sent as ?since= on reconnect

function connectWs() {
  if (_wsPingInterval) { clearInterval(_wsPingInterval); _wsPingInterval = null; }
  const proto = location.protocol==="https:"?"wss:":"ws:";
  const url = `${proto}//${location.host}/ws?since=${_lastSeq}`;
  try { ws = new WebSocket(url); } catch(e) { scheduleReconnect(); return; }
  ws.onopen = () => {
    qs("#ws-dot").classList.remove("disconnected");
    wsReconnectAttempts = 0;
    // Restart ping on every (re)connect
    if (_wsPingInterval) { clearInterval(_wsPingInterval); _wsPingInterval = null; }
    _startWsPing();
  };
  ws.onclose = () => { qs("#ws-dot").classList.add("disconnected"); scheduleReconnect(); };
  ws.onerror = () => qs("#ws-dot").classList.add("disconnected");
  ws.onmessage = evt => {
    let msg; try { msg = JSON.parse(evt.data); } catch (err) {
      console.warn("WS: invalid JSON received", err);
      return;
    }
    if (msg.seq) _lastSeq = Math.max(_lastSeq, msg.seq);
    switch(msg.type) {
      case "account_switched":
        prependSwitchLogRow(msg);
        // Refresh both accounts and full service state (default_account_id may have changed)
        (async () => {
          await Promise.all([loadAccounts(), loadServiceStatus()]);
          updateServiceUI(state.service);
        })();
        toast("Account switched", `→ ${msg.to} (${msg.reason})`, "success");
        break;
      case "account_deleted":
        // Cancel drag if the deleted account is being dragged
        if (typeof dragSrc !== "undefined" && dragSrc && dragSrc.dataset.id === String(msg.id)) {
            isDragging = false;
            dragSrc = null;
        }
        state.accounts = state.accounts.filter(a => a.id !== Number(msg.id));
        if (typeof isDragging !== "undefined" && isDragging) {
            pendingRenderAfterDrag = true;
        } else {
            renderAccounts();
        }
        break;
      case "usage_updated": updateUsageLive(msg.accounts||[]); break;
      case "tmux_result": prependEvent(msg); break;
      case "error": toast("Server error", msg.message, "error", 6000); break;
      default: console.warn("WS: unknown message type", msg.type); break;
    }
  };
}

// Keep WebSocket alive with a periodic ping
let _wsPingInterval = null;
function _startWsPing() {
  if (_wsPingInterval) return;
  _wsPingInterval = setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({type:"ping"})); } catch(_) {}
    }
  }, WS_PING_INTERVAL_MS);
}

function scheduleReconnect() {
  if (_wsPingInterval) { clearInterval(_wsPingInterval); _wsPingInterval = null; }
  wsReconnectAttempts++;
  setTimeout(connectWs, Math.min(MAX_RECONNECT_MS, 1000 * Math.pow(1.5, wsReconnectAttempts)));
}

function updateUsageLive(updates) {
  for (const u of updates) {
    const acc = state.accounts.find(a => Number(a.id) === Number(u.id) || a.email === u.email);
    if (!acc) continue;
    if (u.usage) {
      acc.usage = u.usage;
    }
    const card = qs(`.account-card[data-id="${acc.id}"]`);
    if (card) {
      const usageBlock = card.querySelector(".usage-block");
      if (usageBlock) {
        const tmp = document.createElement("div");
        tmp.innerHTML = usageBlockHtml(acc);
        const newUsage = tmp.firstElementChild;
        if (newUsage) usageBlock.replaceWith(newUsage);
      }
    }
  }
}

// =============================================================
// Theme
// =============================================================
function applyTheme(theme) {
  if (theme === "light") document.documentElement.setAttribute("data-theme", "light");
  else document.documentElement.removeAttribute("data-theme");
  localStorage.setItem("theme", theme);
  syncThemeBtn();
}

function syncThemeBtn() {
  const btn = qs("#theme-btn");
  if (!btn) return;
  const isLight = document.documentElement.getAttribute("data-theme") === "light";
  btn.title = isLight ? "Switch to dark theme" : "Switch to light theme";
  btn.innerHTML = isLight
    ? `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>`
    : `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="4.22" y1="4.22" x2="7.05" y2="7.05"/><line x1="16.95" y1="16.95" x2="19.78" y2="19.78"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/><line x1="4.22" y1="19.78" x2="7.05" y2="16.95"/><line x1="16.95" y1="7.05" x2="19.78" y2="4.22"/></svg>`;
}

qs("#theme-btn").addEventListener("click", () => {
  const isLight = document.documentElement.getAttribute("data-theme") === "light";
  applyTheme(isLight ? "dark" : "light");
});

syncThemeBtn();

// ── Keyboard shortcuts ──────────────────────────────────────────────────────
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    if (addModal.classList.contains("open")) { closeAddModal(); return; }
    if (terminalTarget) closeTerminal();
  }
});

// =============================================================
// Init
// =============================================================
(async () => {
  await Promise.all([loadAccounts(), loadServiceStatus(), loadAutoSwitchSetting(), loadSwitchLog(), loadTmuxData()]);
  connectWs();
})();
