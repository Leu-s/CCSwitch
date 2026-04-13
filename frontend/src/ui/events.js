import { MAX_EVENT_FEED } from "../constants.js";
import { state } from "../state.js";
import { qs, escapeHtml, fmtTime } from "../utils.js";

export function prependEvent(msg) {
  const feed = qs("#event-feed");
  qs("#event-feed-empty").style.display = "none";
  const status = String(msg.status||"").toLowerCase();
  const badgeClass = ["success","failed","uncertain"].includes(status) ? status : "uncertain";
  const monitor = (state.monitors || []).find(m => String(m.id) === String(msg.monitor_id));
  const monitorLabel = monitor ? monitor.name : (msg.monitor_id ? String(msg.monitor_id) : null);
  const card = document.createElement("div");
  card.className = "event-card";
  const capId = "cap-" + Math.random().toString(36).slice(2,9);
  card.innerHTML = `
    <div class="event-head">
      ${monitorLabel ? `<span class="event-monitor">${escapeHtml(monitorLabel)}</span>` : ""}
      <span class="event-target">${escapeHtml(msg.target||"—")}</span>
      <span class="badge ${badgeClass}">${escapeHtml(msg.status||"—")}</span>
      <span class="event-time">${escapeHtml(fmtTime(new Date().toISOString()))}</span>
    </div>
    <div class="event-explanation">${escapeHtml(msg.explanation||"")}</div>
    <button class="event-toggle" data-target="${capId}">Show raw capture ▾</button>
    <pre class="event-capture" id="${capId}">${escapeHtml(msg.capture||"")}</pre>`;
  feed.prepend(card);
  card.querySelector(".event-toggle").addEventListener("click", e => {
    const pre = card.querySelector(".event-capture");
    const open = pre.classList.toggle("open");
    e.currentTarget.textContent = open ? "Hide raw capture ▴" : "Show raw capture ▾";
  });
  while (feed.children.length > MAX_EVENT_FEED) feed.removeChild(feed.lastChild);
  const countEl = qs("#event-feed-count");
  countEl.textContent = feed.children.length;
  countEl.style.display = "";
}
