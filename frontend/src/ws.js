import { WS_PING_INTERVAL_MS, MAX_RECONNECT_MS } from "./constants.js";
import { state } from "./state.js";
import { qs } from "./utils.js";
import { toast } from "./toast.js";
import { renderAccounts, updateUsageLive } from "./ui/accounts.js";
import { loadSwitchLog } from "./ui/log.js";

let ws = null;
let wsReconnectAttempts = 0;
let _replayBoundary = 0;
// Persisted so a page reload does not replay the full backlog from seq=0,
// which would miss any switch / delete / usage events buffered since the
// previous tab was open.
let _lastSeq = (() => {
  try { return Number(sessionStorage.getItem("wsLastSeq") || 0) || 0; }
  catch { return 0; }
})();
let _wsPingInterval = null;

function _startWsPing() {
  if (_wsPingInterval) return;
  _wsPingInterval = setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({type:"ping"})); } catch(_) {}
    }
  }, WS_PING_INTERVAL_MS);
}

function scheduleReconnect() {
  if (_wsPingInterval) { clearInterval(_wsPingInterval); _wsPingInterval = null; }
  wsReconnectAttempts++;
  setTimeout(connectWs, Math.min(MAX_RECONNECT_MS, 1000 * Math.pow(1.5, wsReconnectAttempts)));
}

export function connectWs() {
  if (_wsPingInterval) { clearInterval(_wsPingInterval); _wsPingInterval = null; }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws?since=${_lastSeq}`;
  try { ws = new WebSocket(url); } catch(e) { scheduleReconnect(); return; }

  ws.onopen = () => {
    _replayBoundary = _lastSeq;
    wsReconnectAttempts = 0;
    if (_wsPingInterval) { clearInterval(_wsPingInterval); _wsPingInterval = null; }
    _startWsPing();
  };

  ws.onclose = () => scheduleReconnect();
  ws.onerror = () => {};

  ws.onmessage = evt => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch { return; }

    const isReplay = msg.seq != null && msg.seq <= _replayBoundary;

    if (msg.seq != null) {
      _lastSeq = Math.max(_lastSeq, msg.seq);
      try { sessionStorage.setItem("wsLastSeq", String(_lastSeq)); } catch {}
    }

    switch(msg.type) {
      case "account_switched":
        loadSwitchLog(0);
        // Eagerly reset local is_active + waiting_for_cli on every card so
        // any usage_updated frame that arrives before loadAccounts()
        // completes (the HTTP round-trip is async) cannot render a waiting
        // banner on what is no longer the active card.  Backend's
        // list_accounts gates waiting by is_active, but state.accounts is
        // only reconciled when the GET response lands.
        for (const acc of state.accounts) {
          acc.is_active = (acc.email === msg.to);
          if (!acc.is_active) acc.waiting_for_cli = false;
        }
        // Use custom events to reload accounts + service without importing those modules
        // (avoids circular dependency account↔service).
        document.dispatchEvent(new CustomEvent("app:reload-accounts"));
        document.dispatchEvent(new CustomEvent("app:reload-service"));
        if (!isReplay) {
          toast("Account switched", `→ ${msg.to} (${msg.reason})`, "success");
        }
        break;
      case "account_deleted":
        state.accounts = state.accounts.filter(a => a.id !== Number(msg.id));
        renderAccounts();
        document.dispatchEvent(new CustomEvent("app:reload-service"));
        break;
      case "usage_updated": updateUsageLive(msg.accounts||[]); break;
      case "error": toast("Server error", msg.message, "error", 6000); break;
      default: break;
    }
  };
}
