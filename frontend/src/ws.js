import { WS_PING_INTERVAL_MS, MAX_RECONNECT_MS } from "./constants.js";
import { state } from "./state.js";
import { qs } from "./utils.js";
import { toast } from "./toast.js";
import { renderAccounts, updateUsageLive } from "./ui/accounts.js";
import { prependSwitchLogRow } from "./ui/log.js";
import { prependEvent } from "./ui/events.js";

let ws = null;
let wsReconnectAttempts = 0;
let _lastSeq = 0;
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
    qs("#ws-dot").classList.remove("disconnected");
    wsReconnectAttempts = 0;
    if (_wsPingInterval) { clearInterval(_wsPingInterval); _wsPingInterval = null; }
    _startWsPing();
  };

  ws.onclose = () => { qs("#ws-dot").classList.add("disconnected"); scheduleReconnect(); };
  ws.onerror = () => qs("#ws-dot").classList.add("disconnected");

  ws.onmessage = evt => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch (err) { console.warn("WS: invalid JSON received", err); return; }

    if (msg.seq) _lastSeq = Math.max(_lastSeq, msg.seq);

    switch(msg.type) {
      case "account_switched":
        prependSwitchLogRow(msg);
        // Use custom events to reload accounts + service without importing those modules
        // (avoids circular dependency account↔service).
        document.dispatchEvent(new CustomEvent("app:reload-accounts"));
        document.dispatchEvent(new CustomEvent("app:reload-service"));
        toast("Account switched", `→ ${msg.to} (${msg.reason})`, "success");
        break;
      case "account_deleted":
        state.accounts = state.accounts.filter(a => a.id !== Number(msg.id));
        renderAccounts();
        break;
      case "usage_updated": updateUsageLive(msg.accounts||[]); break;
      case "tmux_result": prependEvent(msg); break;
      case "error": toast("Server error", msg.message, "error", 6000); break;
      default: console.warn("WS: unknown message type", msg.type); break;
    }
  };
}
