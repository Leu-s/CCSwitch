import { TERMINAL_REFRESH_MS } from "../constants.js";
import { qs } from "../utils.js";
import { api, withLoading, fetchTerminalCapture } from "../api.js";
import { toast } from "../toast.js";

let loginSession = null;
let addTermInterval = null;

export function openAddModal() {
  const addModal = qs("#add-modal");
  addModal.classList.add("open");
  qs("#add-step-1").style.display = "";
  qs("#add-step-2").style.display = "none";
  qs("#add-step-3").style.display = "none";
  const termInput = qs("#add-term-input");
  if (termInput) termInput.value = "";
  const termOutput = qs("#add-term-output");
  if (termOutput) termOutput.textContent = "";
  const termTarget = qs("#add-term-target");
  if (termTarget) termTarget.textContent = "—";
}

export function closeAddModal() {
  const addModal = qs("#add-modal");
  addModal.classList.remove("open");
  // Clear the polling interval FIRST so the UI is stable regardless of what
  // the API call below does. If the modal is re-opened before cancel-login
  // resolves, the old interval is already gone and the new session starts fresh.
  if (addTermInterval) { clearInterval(addTermInterval); addTermInterval = null; }
  if (loginSession) {
    // Capture the session reference and null it immediately so a rapid
    // re-open cannot observe a stale loginSession value while the DELETE
    // is still in flight. If the DELETE fails we intentionally do NOT restore
    // loginSession — the UI is closed and any tmux cleanup is best-effort.
    const sessionId = loginSession.session_id;
    loginSession = null;
    api(`/api/accounts/cancel-login?session_id=${sessionId}`, {method:"DELETE"})
      .catch(e => console.warn("cancel-login:", e));
  }
}

async function refreshAddTerminal() {
  if (!loginSession) return;
  const out = qs("#add-term-output");
  await fetchTerminalCapture(loginSession.pane_target, out, null);
  out.scrollTop = out.scrollHeight;
}

export function initLoginListeners() {
  const addModal = qs("#add-modal");

  qs("#add-account-btn").addEventListener("click", openAddModal);
  qs("#add-modal-close").addEventListener("click", closeAddModal);
  addModal.addEventListener("click", e => { if (e.target === addModal) closeAddModal(); });

  qs("#open-terminal-btn").addEventListener("click", async () => {
    const btn = qs("#open-terminal-btn");
    await withLoading(btn, async () => {
      try {
        const result = await api("/api/accounts/start-login", {method:"POST"});
        loginSession = result;
        qs("#add-term-target").textContent = result.pane_target;
        qs("#add-step-1").style.display = "none";
        qs("#add-step-2").style.display = "";
        refreshAddTerminal();
        if (addTermInterval) { clearInterval(addTermInterval); addTermInterval = null; }
        addTermInterval = setInterval(refreshAddTerminal, TERMINAL_REFRESH_MS);
      } catch(e) { toast("Failed to open terminal", e.message, "error"); }
    });
  });

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
          document.dispatchEvent(new CustomEvent("app:reload-accounts"));
          document.dispatchEvent(new CustomEvent("app:reload-service"));
        } else {
          toast("Verification failed", result.error, "error", 6000);
        }
      } catch(e) { toast("Verification failed", e.message, "error"); }
    });
  });

  qs("#add-done-btn").addEventListener("click", () => { qs("#add-modal").classList.remove("open"); });
}

export function clearAddTermInterval() {
  if (addTermInterval) { clearInterval(addTermInterval); addTermInterval = null; }
}
