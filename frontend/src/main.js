// Entry point — imports all modules, registers event listeners, and
// fires the initial data load.

import { state } from "./state.js";
import { qs } from "./utils.js";
import { api } from "./api.js";
import { loadAccounts, renderAccounts } from "./ui/accounts.js";
import { loadServiceStatus, updateServiceUI, loadAutoSwitchSetting, initServiceListeners } from "./ui/service.js";
import { loadSwitchLog, initLogListeners } from "./ui/log.js";
import { closeAddModal, initLoginListeners } from "./ui/login.js";
import { clearAddTermInterval } from "./ui/login.js";
import { loadCredentialTargets, initCredentialTargetsListeners } from "./ui/credential_targets.js";
import { loadTmuxNudge, initTmuxNudgeListeners } from "./ui/tmux_nudge.js";
import { connectWs } from "./ws.js";

// ── App-level reload events (break circular dep between accounts↔service) ──
document.addEventListener("app:reload-accounts", async () => {
  await loadAccounts();
});
document.addEventListener("app:reload-service", async () => {
  await loadServiceStatus();
  updateServiceUI(state.service);
});

// ── Theme ───────────────────────────────────────────────────────────────────
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

// Apply persisted theme immediately (before first render)
const _savedTheme = localStorage.getItem("theme");
if (_savedTheme) applyTheme(_savedTheme);
else syncThemeBtn();

// ── Settings page routing ───────────────────────────────────────────────────
function showPage(name) {
  const accounts = qs("#tab-accounts");
  const settings = qs("#tab-settings");
  if (name === "settings") {
    accounts.hidden = true;
    accounts.classList.remove("active");
    settings.hidden = false;
    settings.classList.add("active");
  } else {
    settings.hidden = true;
    settings.classList.remove("active");
    accounts.hidden = false;
    accounts.classList.add("active");
  }
}
qs("#settings-btn").addEventListener("click", () => showPage("settings"));
qs("#settings-back-btn").addEventListener("click", () => showPage("accounts"));

// ── Keyboard shortcuts ───────────────────────────────────────────────────────
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    const addModal = qs("#add-modal");
    if (addModal && addModal.classList.contains("open")) { closeAddModal(); return; }
  }
});

// ── Shell status ─────────────────────────────────────────────────────────────
async function checkShellStatus() {
  try {
    const { active_file_exists, shell_configured } = await api("/api/settings/shell-status");
    const warn = document.getElementById("shell-warn");
    const desc = document.getElementById("shell-warn-desc");
    if (active_file_exists && shell_configured) {
      warn.style.display = "none";
    } else {
      desc.textContent = shell_configured
        ? "The active account file (~/.claude-multi/active) has not been created yet. Switch to an account or restart the server to create it."
        : "Your shell is not configured to use CLAUDE_CONFIG_DIR. Without this, new terminal sessions may open with the wrong Claude account.";
      warn.style.display = "";
    }
  } catch { /* ignore */ }
}

document.getElementById("shell-tip-apply-btn").addEventListener("click", async () => {
  const btn = document.getElementById("shell-tip-apply-btn");
  const resultEl = document.getElementById("shell-tip-apply-result");
  btn.disabled = true;
  btn.textContent = "Applying…";
  resultEl.textContent = "";
  resultEl.style.color = "";
  try {
    const data = await api("/api/settings/setup-shell", { method: "POST" });
    const labels = { applied: "✓ applied", already_configured: "already configured" };
    const parts = Object.entries(data.results)
      .filter(([, v]) => v !== "not_found")
      .map(([k, v]) => `~/${k}: ${labels[v] ?? v}`);
    resultEl.textContent = parts.length ? parts.join(" • ") : "No .zshrc or .bashrc found.";
    const values = Object.values(data.results);
    const anyError = values.some(v => String(v).startsWith("error:"));
    const allMissing = values.every(v => v === "not_found");
    if (anyError || allMissing) {
      if (anyError) resultEl.style.color = "var(--danger)";
      btn.textContent = "Apply to .zshrc / .bashrc automatically";
      btn.disabled = false;
    } else {
      const anyApplied = values.includes("applied");
      btn.textContent = anyApplied ? "Applied!" : "Already applied ✓";
      btn.disabled = true;
      checkShellStatus();
    }
  } catch (e) {
    resultEl.textContent = "Error: " + e.message;
    resultEl.style.color = "var(--danger)";
    btn.textContent = "Apply to .zshrc / .bashrc automatically";
    btn.disabled = false;
  }
});

document.getElementById("shell-tip-copy-btn").addEventListener("click", async () => {
  const btn = document.getElementById("shell-tip-copy-btn");
  const text = document.getElementById("shell-tip-cmd").textContent.trim();
  try {
    await navigator.clipboard.writeText(text);
    btn.textContent = "Copied!";
    btn.classList.add("copied");
    setTimeout(() => { btn.textContent = "Copy"; btn.classList.remove("copied"); }, 2000);
  } catch {
    const range = document.createRange();
    range.selectNodeContents(document.getElementById("shell-tip-cmd"));
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(range);
  }
});

// ── Cleanup on unload ────────────────────────────────────────────────────────
window.addEventListener("beforeunload", () => {
  clearAddTermInterval();
});

// ── Wire up all sub-module listeners ─────────────────────────────────────────
initServiceListeners();
initLogListeners();
initLoginListeners();
initCredentialTargetsListeners();
initTmuxNudgeListeners();

// ── Reload credential targets whenever the active account changes, so the
//    "Currently: …" label under each target file stays in sync. ─────────────
document.addEventListener("app:reload-accounts", () => {
  loadCredentialTargets();
});

// ── Initial data load ────────────────────────────────────────────────────────
(async () => {
  await Promise.all([
    loadAccounts(),
    loadServiceStatus(true),
    loadAutoSwitchSetting(),
    loadSwitchLog(),
    loadCredentialTargets(),
    loadTmuxNudge(),
  ]);
  checkShellStatus();
  connectWs();
})();
