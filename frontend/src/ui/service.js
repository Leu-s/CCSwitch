import { state } from "../state.js";
import { qs } from "../utils.js";
import { api, withLoading } from "../api.js";
import { toast } from "../toast.js";
import { updateAllExhaustedBanner, loadAccounts } from "./accounts.js";

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
  const stateLabel = qs("#master-switch-state");

  if (s.enabled) {
    btn.dataset.on = "true";
    btn.setAttribute("aria-checked", "true");
    stateLabel.textContent = "ON";
    btn.title = s.active_email
      ? `Auto-switch ON — active: ${s.active_email}. Click to turn off.`
      : "Auto-switch ON. Click to turn off.";
  } else {
    btn.dataset.on = "false";
    btn.setAttribute("aria-checked", "false");
    stateLabel.textContent = "OFF";
    btn.title = "Auto-switch OFF. Click to turn on.";
  }

  updateAllExhaustedBanner();
}

export function initServiceListeners() {
  qs("#service-toggle-btn").addEventListener("click", async () => {
    const btn = qs("#service-toggle-btn");
    const isOn = btn.dataset.on === "true";
    await withLoading(btn, async () => {
      try {
        if (isOn) {
          await api("/api/service/disable", { method: "POST" });
          toast("Auto-switch off", "Service stopped", "success");
        } else {
          const r = await api("/api/service/enable", { method: "POST" });
          toast("Auto-switch on", `Active: ${r.active_email}`, "success");
        }
        await loadServiceStatus();
        loadAccounts();
      } catch (e) {
        toast(isOn ? "Disable failed" : "Enable failed", e.message, "error");
      }
    });
  });
}
