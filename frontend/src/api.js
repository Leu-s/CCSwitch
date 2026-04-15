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
      // Parse the body once so both the human-readable error message AND
      // the structured payload (needed by handlers that branch on 409
      // detail — e.g. the Revalidate button reading `active_refused`)
      // are available on the thrown error.
      let parsed = null;
      try { parsed = await res.json(); } catch {}
      let msg = `${res.status} ${res.statusText}`;
      if (parsed && parsed.detail) {
        if (typeof parsed.detail === "string") msg = parsed.detail;
        else if (typeof parsed.detail === "object" && parsed.detail.stale_reason) msg = parsed.detail.stale_reason;
        else if (typeof parsed.detail === "object" && parsed.detail.error) msg = parsed.detail.error;
        else msg = JSON.stringify(parsed.detail);
      }
      const err = new Error(msg);
      err.status = res.status;
      err.body = parsed;
      throw err;
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

// Ask the backend to refresh a stale vault account's tokens without a
// full tmux re-login.  Backend returns 200 on success; 409 with
// { detail: { success: false, active_refused, stale_reason } } on
// refusal or transient/terminal refresh failure.  Callers must read
// err.body.detail to distinguish the 409 sub-cases.
export async function revalidateAccount(accountId) {
  return api(`/api/accounts/${accountId}/revalidate`, { method: "POST" });
}

export async function withLoading(btn, fn) {
  btn.classList.add("loading"); btn.disabled = true;
  try { return await fn(); }
  finally { btn.classList.remove("loading"); btn.disabled = false; }
}
