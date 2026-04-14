import { TERMINAL_REFRESH_MS } from "../constants.js";
import { qs, escapeHtml } from "../utils.js";
import { api, withLoading } from "../api.js";
import { toast } from "../toast.js";

// The Add Account modal doubles as the Re-login modal: the markup is the
// same (open terminal → tmux → verify) but the endpoints, copy, and
// success behaviour all branch on whether _reloginTarget is set.
//
// _reloginTarget = null         → enrolment mode (POST /start-login + /verify-login)
// _reloginTarget = {accountId,  → re-login mode (POST /{id}/relogin + /{id}/relogin/verify)
//                   email}
let loginSession = null;
let addTermInterval = null;
let _reloginTarget = null;

function _setModalCopyAdd() {
  qs("#add-modal-title").textContent = "Add Account";
  qs("#add-step-1-text").textContent = "A new terminal will open with a fresh isolated Claude session.";
  qs("#add-step-1-hint").innerHTML = "Log in with your Claude account there, then click <strong>Verify &amp; Save</strong>.";
  qs("#open-terminal-btn-label").textContent = "Open Login Terminal";
  qs("#add-verify-btn-label").textContent = "Verify & Save";
  qs("#add-step-3-text").textContent = "Account added successfully";
}

function _setModalCopyRelogin(email) {
  qs("#add-modal-title").textContent = "Re-login";
  qs("#add-step-1-text").innerHTML =
    `A terminal will open inside the <strong>existing</strong> isolated session for <strong>${escapeHtml(email)}</strong>.`;
  qs("#add-step-1-hint").innerHTML =
    `Log in again as <strong>${escapeHtml(email)}</strong>. ` +
    `If you log in as a different account, the new credentials will be wiped and you'll need to retry.`;
  qs("#open-terminal-btn-label").textContent = "Open Re-login Terminal";
  qs("#add-verify-btn-label").textContent = "Verify & Re-login";
  qs("#add-step-3-text").textContent = "Re-login successful";
}

function _resetModalSteps() {
  qs("#add-step-1").classList.remove("hidden");
  qs("#add-step-2").classList.add("hidden");
  qs("#add-step-3").classList.add("hidden");
  const termInput = qs("#add-term-input");
  if (termInput) termInput.value = "";
  const termOutput = qs("#add-term-output");
  if (termOutput) termOutput.textContent = "";
}

export function openAddModal() {
  _reloginTarget = null;
  _setModalCopyAdd();
  _resetModalSteps();
  qs("#add-modal").classList.add("open");
}

export function openReloginModal(accountId, email) {
  _reloginTarget = { accountId, email };
  _setModalCopyRelogin(email);
  _resetModalSteps();
  qs("#add-modal").classList.add("open");
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
    //
    // The same /api/accounts/cancel-login endpoint serves both flows: the
    // backend's cleanup_login_session inspects the session's "kind" and
    // preserves the config dir for re-login sessions automatically.
    const sessionId = loginSession.session_id;
    loginSession = null;
    api(`/api/accounts/cancel-login?session_id=${encodeURIComponent(sessionId)}`, {method:"DELETE"})
      .catch(e => console.warn("cancel-login:", e));
  }
  // Reset the relogin target so the next open via the Add Account button
  // starts in enrolment mode without leaking state.
  _reloginTarget = null;
}

async function refreshAddTerminal() {
  if (!loginSession) return;
  const out = qs("#add-term-output");
  try {
    const sid = encodeURIComponent(loginSession.session_id);
    const d = await api(`/api/accounts/login-sessions/${sid}/capture?lines=100`);
    const atBottom = out.scrollHeight - out.scrollTop <= out.clientHeight + 5;
    out.textContent = d.output || "";
    if (atBottom) out.scrollTop = out.scrollHeight;
  } catch (e) {
    out.textContent = "Error fetching terminal output: " + String(e);
  }
}

export function initLoginListeners() {
  const addModal = qs("#add-modal");

  qs("#add-account-btn").addEventListener("click", openAddModal);
  qs("#add-modal-close").addEventListener("click", closeAddModal);
  addModal.addEventListener("click", e => { if (e.target === addModal) closeAddModal(); });

  // accounts.js dispatches this event when the user clicks Re-login on a
  // stale account card.  Custom event keeps the two modules decoupled.
  document.addEventListener("app:relogin-account", e => {
    const { accountId, email } = e.detail || {};
    if (accountId && email) openReloginModal(accountId, email);
  });

  qs("#open-terminal-btn").addEventListener("click", async () => {
    const btn = qs("#open-terminal-btn");
    await withLoading(btn, async () => {
      try {
        const endpoint = _reloginTarget
          ? `/api/accounts/${_reloginTarget.accountId}/relogin`
          : "/api/accounts/start-login";
        const result = await api(endpoint, {method:"POST"});
        loginSession = result;
        qs("#add-step-1").classList.add("hidden");
        qs("#add-step-2").classList.remove("hidden");
        qs("#add-term-input").focus();
        refreshAddTerminal();
        if (addTermInterval) { clearInterval(addTermInterval); addTermInterval = null; }
        addTermInterval = setInterval(refreshAddTerminal, TERMINAL_REFRESH_MS);
      } catch(e) {
        const action = _reloginTarget ? "re-login terminal" : "terminal";
        toast(`Failed to open ${action}`, e.message, "error");
      }
    });
  });

  qs("#add-term-send-form").addEventListener("submit", async e => {
    e.preventDefault();
    const input = qs("#add-term-input");
    const text = input.value; if (!loginSession) return;
    input.value = "";
    try {
      const sid = encodeURIComponent(loginSession.session_id);
      await api(`/api/accounts/login-sessions/${sid}/send`, {method:"POST", body:{text}});
      setTimeout(refreshAddTerminal, 300);
    } catch(err) { toast("Send failed", err.message, "error"); }
  });

  qs("#add-cancel-btn").addEventListener("click", closeAddModal);

  qs("#add-verify-btn").addEventListener("click", async () => {
    if (!loginSession) return;
    const btn = qs("#add-verify-btn");
    await withLoading(btn, async () => {
      try {
        const sid = encodeURIComponent(loginSession.session_id);
        const endpoint = _reloginTarget
          ? `/api/accounts/${_reloginTarget.accountId}/relogin/verify?session_id=${sid}`
          : `/api/accounts/verify-login?session_id=${sid}`;
        const result = await api(endpoint, {method:"POST"});
        if (result.success && result.already_exists) {
          // Backend accepted the login but the email already matches an
          // existing slot (UNIQUE email constraint).  DO NOT show the
          // "Account added successfully" screen — the user would think
          // they got a new slot when nothing was persisted.  Warn and
          // close the modal; reload so any state changes are reflected.
          clearInterval(addTermInterval); addTermInterval = null;
          loginSession = null;
          toast(
            "Already enrolled",
            `${result.email || "This account"} is already in the dashboard — no new slot was created.`,
            "warning",
            5000,
          );
          closeAddModal();
          document.dispatchEvent(new CustomEvent("app:reload-accounts"));
          document.dispatchEvent(new CustomEvent("app:reload-service"));
        } else if (result.success) {
          clearInterval(addTermInterval); addTermInterval = null;
          qs("#add-result-email").textContent = result.email || "Unknown email";
          qs("#add-step-2").classList.add("hidden");
          qs("#add-step-3").classList.remove("hidden");
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
