import { SLIDER_DEBOUNCE_MS, TOKEN_EXPIRY_SOON_MS } from "../constants.js";
import { state, _sliderDebounce } from "../state.js";
import { qs, qsa, escapeHtml, fmtReset, fmtRelative, tsToMs, usageClass } from "../utils.js";
import { api, withLoading } from "../api.js";
import { toast } from "../toast.js";

// Signals to main.js that accounts or service data should be reloaded.
// Custom events break the circular dep between accounts↔service.
function reloadAccounts() { document.dispatchEvent(new CustomEvent("app:reload-accounts")); }

function updateSliderFill(slider) {
  const min = Number(slider.min) || 0;
  const max = Number(slider.max) || 100;
  const pct = Math.round(((Number(slider.value) - min) / (max - min)) * 100);
  slider.style.background =
    `linear-gradient(to right, var(--accent) ${pct}%, var(--slider-bg) ${pct}%)`;
}

let _accountsGen = 0;
let pendingRenderAfterDrag = false;
let isDragging = false;
let isSavingPriorities = false;
let isSwitching = false;
let dragSrc = null;

export function renderAccounts() {
  if (isDragging || isSavingPriorities) { pendingRenderAfterDrag = true; return; }
  pendingRenderAfterDrag = false;
  // Only cancel timers for accounts that are leaving the DOM; preserve
  // in-flight saves for accounts that are still present.
  const currentIds = new Set(state.accounts.map(a => String(a.id)));
  for (const [id, t] of [..._sliderDebounce]) {
    if (!currentIds.has(String(id))) { clearTimeout(t); _sliderDebounce.delete(id); }
  }
  const grid = qs("#accounts-grid");
  const empty = qs("#accounts-empty");
  const countEl = qs("#accounts-count");
  if (!state.accounts.length) {
    grid.innerHTML = "";
    empty.classList.remove("hidden");
    if (countEl) countEl.textContent = "";
    updateAllExhaustedBanner();
    return;
  }
  empty.classList.add("hidden");
  if (countEl) countEl.textContent = state.accounts.length;
  grid.innerHTML = state.accounts.map((acc, i) => accountCardHtml(acc, i)).join("");
  attachCardEvents();
  updateAllExhaustedBanner();
}

// Surface a banner when auto-switch is ON and every eligible account has
// crossed its own threshold or is currently rate-limited — i.e. the
// switcher has nowhere to move to and will stall on the next trigger.
//
// "Eligible" mirrors get_next_account() on the backend: enabled, not stale,
// and only counted once it has usage data we can judge (accounts with no
// cached probe yet are treated as available — benefit of the doubt).
export function updateAllExhaustedBanner() {
  const banner = qs("#all-exhausted-warn");
  if (!banner) return;
  if (!state.service?.enabled) { banner.classList.add("hidden"); return; }

  const eligible = state.accounts.filter(a => a.enabled && !a.stale_reason);
  if (eligible.length === 0) { banner.classList.add("hidden"); return; }

  const allExhausted = eligible.every(acc => {
    const usage = acc.usage || {};
    if (usage.rate_limited) return true;
    const pct = usage.five_hour_pct;
    if (pct == null) return false; // no data yet — assume still available
    const threshold = acc.threshold_pct ?? 95;
    return pct >= threshold;
  });

  banner.classList.toggle("hidden", !allExhausted);
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
        <div class="usage-pct-row"><span class="usage-pct ${cls}">${pct.toFixed(0)}%</span></div>
      </div>`;
  }

  const fiveBlock  = fiveH  != null ? usageWindow("5 h window", fiveH,  usage.five_hour_resets_at) : "";
  const sevenBlock = sevenD != null ? usageWindow("7 d window", sevenD, usage.seven_day_resets_at)  : "";
  const hasBars    = fiveBlock || sevenBlock;
  const divider    = hasBars ? `<div class="usage-divider"></div>` : "";
  const errBlock   = err ? `<div class="usage-error">⚠ ${escapeHtml(err)}</div>` : "";
  const rateLimitedBadge = rateLimited
    ? `<span class="usage-rate-limited">⚡ Rate limited${(fiveH != null || sevenD != null) ? " (stale data)" : ""}</span>`
    : "";
  const usageEmpty = (!hasBars && !err && !rateLimited) ? `<div class="usage-empty">No usage data yet</div>` : "";

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
  const isActive  = !!acc.is_active;
  const disabled  = !acc.enabled;
  const isStale   = !!acc.stale_reason;
  // Stale always wins over waiting — the account needs re-login, not a refresh.
  const isWaiting = !isStale && !!acc.waiting_for_cli;
  const isDefault = Number(acc.id) === Number(state.service.default_account_id);
  const threshold = acc.threshold_pct ?? 95;

  const staleBanner = isStale ? `
    <div class="stale-banner" title="${escapeHtml(acc.stale_reason)}">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
      <span>${escapeHtml(acc.stale_reason)} — click <strong>Re-login</strong> below to re-authenticate.</span>
    </div>` : "";

  const waitingBanner = isWaiting ? `
    <div class="waiting-banner" title="Active-ownership refresh model: CCSwitch defers token refresh to Claude Code CLI while this account is active.">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
      <span>Access token expired — waiting for Claude Code to refresh.  If no CLI is running, click <strong>Force refresh</strong> below.</span>
    </div>` : "";

  return `
    <article class="account-card ${isActive?"active":""} ${disabled?"disabled":""} ${isStale?"stale":""} ${isWaiting?"waiting":""}"
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
            ${isWaiting ? `<span class="waiting-pill" title="Waiting for Claude Code to refresh the OAuth token">Waiting</span>` : ""}
            <label class="toggle" title="Include in auto-switching">
              <input type="checkbox" class="enabled-toggle" data-id="${acc.id}" ${acc.enabled?"checked":""} aria-label="Include ${escapeHtml(acc.email)} in auto-switching" />
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
      ${waitingBanner}
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
        ${isStale
          ? `<button class="btn primary relogin-btn" data-id="${acc.id}" data-email="${escapeHtml(acc.email)}" title="Open a terminal and re-authenticate this account">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path></svg>
              Re-login
            </button>`
          : isWaiting
          ? `<button class="btn primary force-refresh-btn" data-id="${acc.id}" title="Refresh the OAuth token using the stored refresh_token — only click if no Claude Code CLI is running for this account">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>
              Force refresh
            </button>`
          : `<button class="btn primary ${isActive?"outlined":""} switch-btn" data-id="${acc.id}" ${isActive?"disabled":""} title="${(!isActive && disabled) ? "Manual switch — available even when excluded from auto-switching" : ""}">
              ${isActive ? "Currently active" : "Switch to"}
            </button>`
        }
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
        const switchBtn = cardEl.querySelector(".switch-btn");
        if (switchBtn && !switchBtn.disabled) {
          switchBtn.title = !enabled ? "Manual switch — available even when excluded from auto-switching" : "";
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
    updateSliderFill(slider);
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
      if (changed) renderAccounts();
    });
  });

  qsa(".switch-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (btn.disabled || isSwitching) return;
      isSwitching = true;
      const id = btn.dataset.id;
      let ok = false;
      try {
        await withLoading(btn, async () => {
          try {
            const r = await api(`/api/accounts/${id}/switch`, {method:"POST"});
            if (r.already_active) {
              toast("Already active", "This account is already the current one", "success", 2000);
            } else {
              toast("Switched", "Credentials updated", "success", 2000);
            }
            ok = true;
          } catch(e) { toast("Switch failed", e.message, "error"); }
        });
        // WS account_switched event will trigger reloadAccounts + reloadService
      } finally { isSwitching = false; }
    });
  });

  qsa(".relogin-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      // Custom event keeps accounts.js → login.js decoupled (mirrors the
      // app:reload-accounts pattern used elsewhere).  login.js owns the
      // modal state machine and listens for this event in initLoginListeners.
      document.dispatchEvent(new CustomEvent("app:relogin-account", {
        detail: { accountId: Number(btn.dataset.id), email: btn.dataset.email },
      }));
    });
  });

  qsa(".force-refresh-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (btn.disabled) return;
      const id = btn.dataset.id;
      await withLoading(btn, async () => {
        try {
          await api(`/api/accounts/${id}/force-refresh`, { method: "POST" });
          toast("Refreshed", "OAuth token refreshed", "success", 2500);
          // The endpoint already fired a single-account WS broadcast,
          // so updateUsageLive will flip this card out of waiting state
          // before this toast finishes animating in.
        } catch (e) {
          // 409 = stale (re-login needed) or already-stale; 502 = upstream error.
          // The backend already flipped the card to "stale" via WS for the 409
          // case, so we just surface the reason as a toast either way.
          toast("Force refresh failed", e.message, "error", 4500);
        }
      });
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
      if (ok) reloadAccounts();
    });
  });
}

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
    if (pendingRenderAfterDrag) renderAccounts();
  });
  card.addEventListener("dragenter", e => {
    if (!dragSrc || dragSrc === card) return; e.preventDefault();
  });
  card.addEventListener("dragover", e => {
    if (!dragSrc || dragSrc === card) return; e.preventDefault(); e.dataTransfer.dropEffect = "move";
    const rect = card.getBoundingClientRect();
    const isAbove = e.clientY < rect.top + rect.height / 2;
    qsa(".account-card").forEach(c => c.classList.remove("drop-above","drop-below"));
    card.classList.add(isAbove ? "drop-above" : "drop-below");
  });
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
      reloadAccounts();
    } finally {
      isSavingPriorities = false;
      if (pendingRenderAfterDrag) { pendingRenderAfterDrag = false; renderAccounts(); }
    }
  });
}

export async function loadAccounts() {
  const loadingEl = qs("#accounts-loading");
  const errorEl   = qs("#accounts-error");
  const gen = ++_accountsGen;
  try {
    const data = await api("/api/accounts");
    if (gen !== _accountsGen) return;
    state.accounts = data;
    if (errorEl) errorEl.classList.add("hidden");
    renderAccounts();
  } catch(e) {
    toast("Load accounts failed", e.message, "error");
    if (errorEl) { errorEl.textContent = `Failed to load accounts: ${e.message}`; errorEl.classList.remove("hidden"); }
  } finally {
    if (loadingEl) loadingEl.classList.add("hidden");
  }
}

export function updateUsageLive(updates) {
  // Transitions that swap the footer button or add/remove a banner —
  // waiting_for_cli flipping, or stale_reason flipping — require a full
  // card DOM rebuild.  The in-place usage-block patch path below only
  // works when both of those flags stay unchanged.  We still patch each
  // account's in-place DOM eagerly (so unrelated cards in the same batch
  // update immediately) and fall through to a full renderAccounts() at
  // the end if any account in the batch needed a rebuild.
  let needsFullRender = false;
  for (const u of updates) {
    const acc = state.accounts.find(a => Number(a.id) === Number(u.id) || a.email === u.email);
    if (!acc) continue;
    if (u.usage) acc.usage = u.usage;
    const prevWaiting = !!acc.waiting_for_cli;
    const nextWaiting = !!u.waiting_for_cli;
    acc.waiting_for_cli = nextWaiting;
    // stale_reason can arrive as null (cleared) or a string (newly stale).
    // Only adopt when the key is present in the payload so older broadcast
    // shapes (no stale_reason key) do not accidentally wipe the field.
    const prevStale = acc.stale_reason || null;
    let nextStale = prevStale;
    if (Object.prototype.hasOwnProperty.call(u, "stale_reason")) {
      nextStale = u.stale_reason || null;
      acc.stale_reason = nextStale;
    }
    if (prevWaiting !== nextWaiting || prevStale !== nextStale) {
      needsFullRender = true;
    }
    const card = qs(`.account-card[data-id="${acc.id}"]`);
    if (card) {
      const usageBlock = card.querySelector(".usage-block");
      if (usageBlock) {
        const tmp = document.createElement("div");
        tmp.innerHTML = usageBlockHtml(acc);
        const newUsage = tmp.firstElementChild;
        if (newUsage) {
          // Preserve the existing element (keeps any parent event delegation intact);
          // only replace its content and classes.
          usageBlock.innerHTML = newUsage.innerHTML;
          usageBlock.className = newUsage.className;
        }
      }
    }
  }
  if (needsFullRender) renderAccounts();
  updateAllExhaustedBanner();
}
