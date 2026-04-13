import { TOAST_TTL_MS, TOAST_FADEOUT_MS, MAX_TOASTS } from "./constants.js";
import { qs, escapeHtml } from "./utils.js";

export function toast(title, body = "", kind = "info", ttl = TOAST_TTL_MS) {
  const container = qs("#toast-container");
  // Cap at MAX_TOASTS concurrent toasts — evict the oldest if at limit
  const existing = container.querySelectorAll(".toast");
  if (existing.length >= MAX_TOASTS) existing[0].remove();
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.innerHTML = `<div class="toast-title">${escapeHtml(title)}</div>${body ? `<div class="toast-body">${escapeHtml(body)}</div>` : ""}`;
  container.appendChild(el);
  setTimeout(() => { el.classList.add("fade-out"); setTimeout(() => el.remove(), TOAST_FADEOUT_MS); }, ttl);
}
