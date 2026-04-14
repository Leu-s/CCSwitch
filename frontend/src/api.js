import { API_TIMEOUT_MS } from "./constants.js";

export async function api(path, opts = {}) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), API_TIMEOUT_MS);
  const init = { headers: { "Content-Type": "application/json" }, ...opts, signal: controller.signal };
  if (opts.body && typeof opts.body === "object") init.body = JSON.stringify(opts.body);
  try {
    const res = await fetch(path, init);
    clearTimeout(timeoutId);
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`;
      try { const j = await res.json(); if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
      throw new Error(msg);
    }
    if (res.status === 204) return null;
    const ct = res.headers.get("content-type") || "";
    return ct.includes("application/json") ? res.json() : res.text();
  } catch (e) {
    clearTimeout(timeoutId);
    if (e.name === "AbortError") throw new Error("Request timed out");
    throw e;
  }
}

export async function withLoading(btn, fn) {
  btn.classList.add("loading"); btn.disabled = true;
  try { return await fn(); }
  finally { btn.classList.remove("loading"); btn.disabled = false; }
}
