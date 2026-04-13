import { state } from "../state.js";
import { qs } from "../utils.js";
import { api, withLoading } from "../api.js";
import { toast } from "../toast.js";

export async function loadServiceStatus(silent = false) {
  try {
    const s = await api("/api/service");
    state.service = s;
    updateServiceUI(s);
  } catch (e) {
    if (!silent) {
      console.warn("loadServiceStatus error", e);
      toast("Service status failed", e.message || String(e), "error");
    }
  }
}

export function updateServiceUI(s) {
  const btn = qs("#service-toggle-btn");
  const stateLabel = qs("#service-btn-state");
  const flow = qs("#service-btn-flow");
  const currentEl = qs("#service-btn-current");
  const autoRow = qs("#sl-auto-row");

  if (s.enabled) {
    btn.dataset.on = "true";
    stateLabel.textContent = "Switch: ON";
    if (s.active_email) { currentEl.textContent = s.active_email; flow.hidden = false; }
    else { flow.hidden = true; }
    if (autoRow) autoRow.style.display = "";
  } else {
    btn.dataset.on = "false";
    stateLabel.textContent = "Switch: OFF";
    flow.hidden = true;
    if (autoRow) autoRow.style.display = "none";
  }
}

export async function loadAutoSwitchSetting() {
  try {
    const settings = await api("/api/settings");
    const entry = settings.find(s => s.key === "auto_switch_enabled");
    const enabled = entry ? entry.value !== "false" : false;
    const cb = qs("#auto-switch-cb");
    if (cb) cb.checked = enabled;
  } catch { /* initial load — ignore */ }
}

export function initServiceListeners() {
  qs("#auto-switch-cb").addEventListener("change", async (e) => {
    const val = e.target.checked ? "true" : "false";
    try {
      await api("/api/settings/auto_switch_enabled", { method: "PATCH", body: { value: val } });
      toast(e.target.checked ? "Auto-switch on" : "Auto-switch off", null, "success", 2000);
    } catch (err) {
      e.target.checked = !e.target.checked;
      toast("Update failed", err.message, "error");
    }
  });

  qs("#service-toggle-btn").addEventListener("click", async () => {
    const btn = qs("#service-toggle-btn");
    const isOn = btn.dataset.on === "true";
    await withLoading(btn, async () => {
      try {
        if (isOn) {
          await api("/api/service/disable", { method: "POST" });
          toast("Service disabled", "Auto-switching stopped", "success");
        } else {
          const r = await api("/api/service/enable", { method: "POST" });
          toast("Service enabled", `Active: ${r.active_email}`, "success");
        }
        await loadServiceStatus();
        document.dispatchEvent(new CustomEvent("app:reload-accounts"));
      } catch (e) {
        toast(isOn ? "Disable failed" : "Enable failed", e.message, "error");
      }
    });
  });
}
