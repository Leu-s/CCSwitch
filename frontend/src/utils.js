// Pure DOM/formatting helpers — no imports.

export function qs(sel, root = document)  { return root.querySelector(sel); }
export function qsa(sel, root = document) { return Array.from(root.querySelectorAll(sel)); }

export function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

export function fmtTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString([], { month:"short", day:"numeric", hour:"2-digit", minute:"2-digit" });
  } catch { return iso; }
}

// Parse any timestamp format: ISO string, ms number, or seconds number
export function tsToMs(ts) {
  if (!ts) return null;
  if (typeof ts === "string") { const d = new Date(ts); return isNaN(d) ? null : d.getTime(); }
  return ts > 1e12 ? ts : ts * 1000;
}

export function fmtReset(ts) {
  const ms = tsToMs(ts);
  if (!ms) return "—";
  try {
    const d = new Date(ms);
    if (isNaN(d)) return "—";
    const diffH = (d - Date.now()) / 3600000;
    if (diffH < 0) {
      if (diffH > -24) return d.toLocaleTimeString([], { hour:"2-digit", minute:"2-digit" });
      return d.toLocaleDateString([], { month:"short", day:"numeric" });
    }
    if (diffH < 24) return d.toLocaleTimeString([], { hour:"2-digit", minute:"2-digit" });
    return d.toLocaleDateString([], { month:"short", day:"numeric" }) + " " + d.toLocaleTimeString([], { hour:"2-digit", minute:"2-digit" });
  } catch { return "—"; }
}

export function fmtRelative(ts) {
  const ms = tsToMs(ts);
  if (!ms) return "";
  try {
    const diff = ms - Date.now();
    if (diff <= 0) return "expired";
    const h = Math.floor(diff / 3600000);
    const m = Math.floor((diff % 3600000) / 60000);
    if (h >= 24) { const d = Math.floor(h / 24); return `in ${d}d ${h % 24}h`; }
    if (h > 0) return `in ${h}h ${m}m`;
    return `in ${m}m`;
  } catch { return ""; }
}

export function usageClass(pct, threshold = 95) {
  if (pct == null) return "";
  if (pct >= threshold) return "crit";
  if (pct >= threshold * 0.75) return "warn";
  return "";
}
