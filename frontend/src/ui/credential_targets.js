// Credential-targets panel — auto-discovered .claude.json mirror list.
//
// By default *nothing* is enabled: switches only update the dashboard's own
// active pointer.  The user ticks boxes to opt each discovered file into
// being rewritten on every switch, so a fresh `claude` reads the right
// oauthAccount no matter which entrypoint the user launches from.

import { qs, escapeHtml } from "../utils.js";
import { api } from "../api.js";
import { toast } from "../toast.js";

let _targets = [];
let _busy = false;

export async function loadCredentialTargets() {
  const listEl = qs("#ct-list");
  const loadingEl = qs("#ct-loading");
  const emptyEl = qs("#ct-empty");
  const countEl = qs("#ct-count");
  if (!listEl) return;
  try {
    const data = await api("/api/credential-targets");
    _targets = data || [];
    render();
    if (loadingEl) loadingEl.style.display = "none";
    if (countEl) {
      const enabled = _targets.filter(t => t.enabled).length;
      const total = _targets.length;
      countEl.textContent = `${enabled}/${total} enabled`;
    }
    if (!_targets.length) {
      if (emptyEl) emptyEl.style.display = "";
      if (listEl) listEl.style.display = "none";
    } else {
      if (emptyEl) emptyEl.style.display = "none";
      if (listEl) listEl.style.display = "";
    }
  } catch (e) {
    if (loadingEl) loadingEl.style.display = "none";
    if (listEl) {
      listEl.style.display = "";
      listEl.innerHTML = `<div class="ct-error">Failed to load: ${escapeHtml(e.message || String(e))}</div>`;
    }
  }
}

function render() {
  const listEl = qs("#ct-list");
  const bannerEl = qs("#ct-warn");
  if (!listEl) return;
  listEl.innerHTML = _targets.map(renderItem).join("");
  attachItemHandlers();

  // Show the "no targets enabled" warning if the user has not opted in to
  // any file yet — this is the most common way someone ends up with "UI
  // shows X but `claude` still uses Y".
  if (bannerEl) {
    const anyEnabled = _targets.some(t => t.enabled);
    const anyExisting = _targets.some(t => t.exists);
    bannerEl.style.display = !anyEnabled && anyExisting ? "" : "none";
  }
}

function renderItem(t) {
  const exists = !!t.exists;
  const canonical = t.canonical || t.path || "";
  const labelText = t.label || canonical;
  const emailText = exists
    ? (t.current_email ? `Currently: ${escapeHtml(t.current_email)}` : `<span class="ct-dim">No oauthAccount</span>`)
    : `<span class="ct-dim">File missing on disk</span>`;
  const disabledAttr = exists ? "" : "disabled";
  const checkedAttr = t.enabled ? "checked" : "";
  return `
    <div class="ct-item ${exists ? "" : "missing"}" data-canonical="${escapeHtml(canonical)}">
      <label class="toggle ct-toggle" title="${exists ? "Mirror oauthAccount on every switch" : "File missing — cannot enable"}">
        <input type="checkbox" class="ct-cb" ${checkedAttr} ${disabledAttr} />
        <span class="track"></span>
      </label>
      <div class="ct-body">
        <div class="ct-path" title="${escapeHtml(canonical)}">${escapeHtml(labelText)}</div>
        <div class="ct-meta">${emailText}</div>
      </div>
    </div>`;
}

function attachItemHandlers() {
  const listEl = qs("#ct-list");
  if (!listEl) return;
  listEl.querySelectorAll(".ct-item").forEach(item => {
    const cb = item.querySelector(".ct-cb");
    if (!cb) return;
    cb.addEventListener("change", async () => {
      if (_busy) { cb.checked = !cb.checked; return; }
      const canonical = item.dataset.canonical;
      const enabled = cb.checked;
      cb.disabled = true;
      _busy = true;
      try {
        const updated = await api("/api/credential-targets", {
          method: "PATCH",
          body: { canonical, enabled },
        });
        _targets = updated || [];
        render();
        // Update counter.
        const countEl = qs("#ct-count");
        if (countEl) {
          const en = _targets.filter(t => t.enabled).length;
          countEl.textContent = `${en}/${_targets.length} enabled`;
        }
        toast(
          enabled ? "Target enabled" : "Target disabled",
          canonical.replace(/^.*\/\.?([^/]+)$/, "…/$1"),
          "success",
          1800,
        );
      } catch (e) {
        cb.checked = !enabled;
        toast("Update failed", e.message || String(e), "error");
      } finally {
        cb.disabled = false;
        _busy = false;
      }
    });
  });
}

function refreshCount() {
  const countEl = qs("#ct-count");
  if (!countEl) return;
  const en = _targets.filter(t => t.enabled).length;
  countEl.textContent = `${en}/${_targets.length} enabled`;
}

export function initCredentialTargetsListeners() {
  const rescan = qs("#ct-rescan-btn");
  if (rescan) {
    rescan.addEventListener("click", async () => {
      if (_busy) return;
      rescan.disabled = true;
      try {
        const data = await api("/api/credential-targets/rescan", { method: "POST" });
        _targets = data || [];
        render();
        refreshCount();
        toast("Rescanned", `${_targets.length} target(s)`, "success", 1800);
      } catch (e) {
        toast("Rescan failed", e.message || String(e), "error");
      } finally {
        rescan.disabled = false;
      }
    });
  }

  const sync = qs("#ct-sync-btn");
  if (sync) {
    sync.addEventListener("click", async () => {
      if (_busy) return;
      sync.disabled = true;
      const original = sync.innerHTML;
      sync.innerHTML = "Syncing…";
      try {
        const r = await api("/api/credential-targets/sync", { method: "POST" });
        const summary = (r && r.summary) || {};
        const mirror = summary.mirror || {};
        const written = mirror.written || [];
        const errors = mirror.errors || [];
        const skipped = mirror.skipped || [];

        if (Array.isArray(r.targets)) {
          _targets = r.targets;
          render();
          refreshCount();
        }

        if (errors.length) {
          toast("Sync had errors", errors[0], "error");
        } else if (written.length === 0) {
          // skipped[0] is "no targets enabled — …" when the user hasn't ticked anything.
          toast(
            "Nothing to sync",
            skipped[0] || "Tick at least one target first",
            "info",
          );
        } else {
          toast(
            "Synced",
            `${written.length} target file${written.length === 1 ? "" : "s"} updated`,
            "success",
            2400,
          );
        }
      } catch (e) {
        toast("Sync failed", e.message || String(e), "error");
      } finally {
        sync.innerHTML = original;
        sync.disabled = false;
      }
    });
  }
}
