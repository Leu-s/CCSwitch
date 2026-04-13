import { TERMINAL_REFRESH_MS } from "../constants.js";
import { state } from "../state.js";
import { qs, qsa, escapeHtml } from "../utils.js";
import { api, withLoading, fetchTerminalCapture } from "../api.js";
import { toast } from "../toast.js";

let terminalTarget = null;
let captureInterval = null;

export function openTerminal(target) {
  terminalTarget = target;
  qs("#terminal-target").textContent = target;
  const session = target.split(":")[0];
  qs("#terminal-attach-cmd").textContent = `tmux attach -t ${session}`;
  const panel = qs("#terminal-panel");
  panel.classList.add("visible");
  const panelH = parseInt(panel.style.height) || panel.offsetHeight;
  qs("main").style.paddingBottom = (panelH + 20) + "px";
  qsa(".pane-item").forEach(el => el.classList.toggle("selected", el.dataset.target === target));
  qs("#terminal-input").focus();
  refreshCapture();
  if (captureInterval) clearInterval(captureInterval);
  captureInterval = setInterval(refreshCapture, TERMINAL_REFRESH_MS);
}

export function closeTerminal() {
  terminalTarget = null;
  if (captureInterval) { clearInterval(captureInterval); captureInterval = null; }
  qs("#terminal-panel").classList.remove("visible");
  qs("main").style.paddingBottom = "";
  qsa(".pane-item.selected").forEach(el => el.classList.remove("selected"));
}

export function getCaptureInterval() { return captureInterval; }
export function clearCaptureInterval() {
  if (captureInterval) { clearInterval(captureInterval); captureInterval = null; }
}

async function refreshCapture() {
  await fetchTerminalCapture(terminalTarget, qs("#terminal-output"), qs("#terminal-live"));
}

function prefillMonitorForm(target, cmd) {
  const form = qs("#add-monitor-form");
  const nameInput = qs('[name="name"]', form);
  const typeSelect = qs("#pattern-type-select");
  const patternInput = qs('[name="pattern"]', form);
  typeSelect.value = "manual";
  patternInput.value = target;
  patternInput.setCustomValidity("");
  updatePatternTypeHint();
  if (!nameInput.value.trim()) {
    const label = cmd && cmd !== "—" ? cmd.replace(/^[-\s]+/, "").slice(0, 28) : target;
    nameInput.value = label;
  }
  form.scrollIntoView({ behavior: "smooth", block: "nearest" });
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

export async function loadTmuxData() { await Promise.all([loadSessions(), loadMonitors()]); }

export async function loadSessions() {
  qs("#sessions-count").textContent = "…";
  qs("#sessions-list").innerHTML = `<div class="empty-state" style="margin:10px;color:var(--text-mute)">Loading…</div>`;
  try {
    const data = await api("/api/tmux/sessions");
    state.sessions = data || [];
    renderSessions();
  } catch(e) {
    qs("#sessions-count").textContent = "—";
    qs("#sessions-list").innerHTML = `<div class="empty-state" style="margin:10px;color:var(--danger)">Failed to load sessions.</div>`;
    toast("Load sessions failed", e.message, "error");
  }
}

export function renderSessions() {
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

export async function loadMonitors() {
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

export function renderMonitors() {
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

const _monitorDelState = new WeakMap();

export function initTmuxListeners() {
  qs("#sessions-list").addEventListener("click", e => {
    const monitorBtn = e.target.closest(".pane-monitor-btn");
    if (monitorBtn) { e.stopPropagation(); prefillMonitorForm(monitorBtn.dataset.target, monitorBtn.dataset.cmd); return; }
    const paneItem = e.target.closest(".pane-item[data-target]");
    if (paneItem) openTerminal(paneItem.dataset.target);
  });

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

    const trashBtn = e.target.closest(".monitor-del:not(.monitor-del-ok)");
    if (trashBtn && !trashBtn.classList.contains("monitor-del-ok")) {
      if (document.querySelector(".monitor-row.editing")) return;
      const wrap = trashBtn.closest(".monitor-del-wrap");
      const confirmStrip = wrap.querySelector(".monitor-del-confirm");
      let st = _monitorDelState.get(wrap);
      if (!st) { st = { timer: null }; _monitorDelState.set(wrap, st); }
      function hideConfirm() { clearTimeout(st.timer); trashBtn.style.display = ""; confirmStrip.style.display = "none"; }
      trashBtn.style.display = "none"; confirmStrip.style.display = "flex";
      clearTimeout(st.timer);
      st.timer = setTimeout(hideConfirm, 4000);
      st.hideConfirm = hideConfirm;
      return;
    }

    const cancelBtn = e.target.closest(".monitor-del-cancel");
    if (cancelBtn) {
      const wrap = cancelBtn.closest(".monitor-del-wrap");
      const s = _monitorDelState.get(wrap);
      if (s && s.hideConfirm) s.hideConfirm();
      return;
    }

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
  qs("#terminal-close-btn").addEventListener("click", closeTerminal);

  qs("#terminal-send-form").addEventListener("submit", async e => {
    e.preventDefault();
    const input = qs("#terminal-input");
    const text = input.value; if (!terminalTarget) return;
    input.value = "";
    try { await api("/api/tmux/send",{method:"POST",body:{target:terminalTarget,text}}); setTimeout(refreshCapture,300); }
    catch(err) { toast("Send failed",err.message,"error"); }
  });

  // Drag-to-resize terminal panel
  // Uses pointer capture so releasing outside the browser window still fires
  // pointerup/pointercancel on the handle, preventing listener leaks.
  (function() {
    const panel = qs("#terminal-panel");
    const handle = qs("#terminal-resize-handle");
    let startY = 0, startH = 0;
    handle.addEventListener("pointerdown", e => {
      e.preventDefault();
      startY = e.clientY; startH = panel.offsetHeight;
      handle.setPointerCapture(e.pointerId);
      handle.classList.add("dragging");
      document.body.style.userSelect = "none";
      document.body.style.cursor = "ns-resize";
      qs("main").style.transition = "none";
      function onMove(e) {
        const newH = Math.min(Math.max(startH + (startY - e.clientY), 120), window.innerHeight * 0.85);
        panel.style.height = newH + "px";
        qs("main").style.paddingBottom = (newH + 20) + "px";
      }
      function onUp() {
        handle.classList.remove("dragging");
        document.body.style.userSelect = ""; document.body.style.cursor = "";
        qs("main").style.transition = "";
        handle.removeEventListener("pointermove", onMove);
        handle.removeEventListener("pointerup", onUp);
        handle.removeEventListener("pointercancel", onUp);
      }
      handle.addEventListener("pointermove", onMove);
      handle.addEventListener("pointerup", onUp);
      handle.addEventListener("pointercancel", onUp);
    });
  })();

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
      catch(err) { patternInput.setCustomValidity("Invalid regex: " + err.message); patternInput.reportValidity(); return; }
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
      try {
        await api("/api/tmux/monitors",{method:"POST",body:payload});
        form.reset(); updatePatternTypeHint();
        toast("Monitor added",payload.name,"success");
        await loadMonitors();
      } catch(err) { toast("Add failed",err.message,"error"); }
    });
  });
}
