// Entry point — imports all modules, registers event listeners, and
// fires the initial data load.

import { state } from "./state.js";
import { qs } from "./utils.js";
import { loadAccounts } from "./ui/accounts.js";
import { loadServiceStatus, updateServiceUI, initServiceListeners } from "./ui/service.js";
import { loadSwitchLog, initLogListeners } from "./ui/log.js";
import { closeAddModal, initLoginListeners, clearAddTermInterval, setLoginReloadCallbacks } from "./ui/login.js";
import { loadTmuxNudge, initTmuxNudgeListeners } from "./ui/tmux_nudge.js";
import { connectWs } from "./ws.js";

// Wire login.js reload callbacks to break the login↔accounts circular import.
setLoginReloadCallbacks(
  loadAccounts,
  () => loadServiceStatus().then(() => updateServiceUI(state.service)),
);

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
let _currentPage = "accounts";
function showPage(name) {
  const accounts = qs("#tab-accounts");
  const settings = qs("#tab-settings");
  const onSettings = name === "settings";
  accounts.hidden = onSettings;
  accounts.classList.toggle("active", !onSettings);
  settings.hidden = !onSettings;
  settings.classList.toggle("active", onSettings);

  const navBtn = qs("#nav-toggle-btn");
  qs(".nav-icon-settings").classList.toggle("hidden", onSettings);
  qs(".nav-icon-home").classList.toggle("hidden", !onSettings);
  navBtn.setAttribute("aria-label", onSettings ? "Back to accounts" : "Settings");
  navBtn.setAttribute("title",      onSettings ? "Back to accounts" : "Settings");
  _currentPage = name;
}
qs("#nav-toggle-btn").addEventListener("click", () => {
  showPage(_currentPage === "settings" ? "accounts" : "settings");
});

// ── Keyboard shortcuts ───────────────────────────────────────────────────────
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    const addModal = qs("#add-modal");
    if (addModal && addModal.classList.contains("open")) { closeAddModal(); return; }
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
initTmuxNudgeListeners();

// ── Initial data load ────────────────────────────────────────────────────────
(async () => {
  await Promise.all([
    loadAccounts(),
    loadServiceStatus(true),
    loadSwitchLog(),
    loadTmuxNudge(),
  ]);
  connectWs();
})();
