import { WS_PING_INTERVAL_MS, MAX_RECONNECT_MS } from "./constants.js";
import { state } from "./state.js";
import { qs } from "./utils.js";
import { toast } from "./toast.js";
import { renderAccounts, updateUsageLive, loadAccounts } from "./ui/accounts.js";
import { loadSwitchLog } from "./ui/log.js";
import { loadServiceStatus, updateServiceUI } from "./ui/service.js";

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
        // Eagerly flip is_active on every card so the Active pill and
        // footer button update in place before the loadAccounts() HTTP
        // round-trip lands.  A reload is still dispatched below as
        // defense-in-depth for freshly-added cards that may not be in
        // state.accounts yet.
        for (const acc of state.accounts) {
          acc.is_active = (acc.email === msg.to);
        }
        renderAccounts();
        loadAccounts();
        loadServiceStatus().then(() => updateServiceUI(state.service));
        if (!isReplay) {
          toast("Account switched", `→ ${msg.to} (${msg.reason})`, "success");
        }
        break;
      case "account_updated":
        loadAccounts();
        break;
      case "account_added":
        // Tab B picks up a new slot enrolled in Tab A.  The existing
        // updateUsageLive patch path only mutates known cards, so an
        // account_added event must trigger a full /api/accounts reload —
        // otherwise tab B never sees the new card until a switch/delete
        // broadcast forces a reload (multi-tab "ghost slot" bug).
        loadAccounts();
        break;
      case "account_deleted":
        state.accounts = state.accounts.filter(a => a.id !== Number(msg.id));
        renderAccounts();
        loadServiceStatus().then(() => updateServiceUI(state.service));
        break;
      case "usage_updated": updateUsageLive(msg.accounts||[]); break;
      case "error": toast("Server error", msg.message, "error", 6000); break;
      default: break;
    }
  };
}
