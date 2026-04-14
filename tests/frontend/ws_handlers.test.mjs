/*
 * Frontend ws.js handler tests — executed under Node.js with a stubbed
 * DOM / WebSocket so the audit-round-3 fixes that live purely in
 * frontend/src/ws.js can be validated without a real browser.
 *
 * Fixes under test:
 *
 *   Fix #5 (ws.js account_switched) — after eagerly mutating
 *   ``state.accounts`` to flip is_active + clear waiting_for_cli on the
 *   old active card, the handler must call ``renderAccounts()``
 *   IMMEDIATELY (before dispatching ``app:reload-accounts``) so the DOM
 *   reflects the new state without the ~300ms HTTP-round-trip flicker.
 *
 *   Fix #3 (ws.js account_added) — the new case must dispatch
 *   ``app:reload-accounts`` so sibling tabs reload /api/accounts and
 *   pick up a freshly-enrolled slot that ``updateUsageLive`` alone
 *   cannot surface.
 *
 * How this works:
 *
 *   Node.js ESM cannot directly import modules with relative paths the
 *   way a browser does, so instead of trying to resolve ws.js's imports
 *   (which would pull in constants.js / state.js / utils.js / toast.js /
 *   ui/accounts.js / ui/log.js), we read ws.js as TEXT, strip its
 *   ``import`` and ``export`` statements, and run the resulting source
 *   in a ``node:vm`` context populated with stubs for every identifier
 *   ws.js references.  The stub for ``renderAccounts`` increments a
 *   counter we later assert on; the stub ``document.dispatchEvent``
 *   appends the event type to a log so we can verify ORDERING.
 *
 *   We also stub ``WebSocket`` to capture the ``ws`` instance
 *   ``connectWs`` creates so the test can directly invoke the handlers
 *   ``ws.onopen`` / ``ws.onmessage`` that ws.js assigns on open.
 *
 *   Output: TAP-lite — one ``ok …`` / ``not ok …`` line per assertion
 *   plus a final summary line, echoed to stdout.  Exit code 0 on all
 *   pass, 1 on any failure.  The pytest wrapper in
 *   ``tests/test_frontend_handlers.py`` runs this script and asserts
 *   exit code 0.
 */
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import url from "node:url";

const here = path.dirname(url.fileURLToPath(import.meta.url));
const wsJsPath = path.resolve(here, "..", "..", "frontend", "src", "ws.js");

const rawSrc = fs.readFileSync(wsJsPath, "utf8");

// ─── Transform ws.js so it can run outside an ES-module loader ──────────────
//
// ws.js starts with a handful of ``import … from "./…";`` statements and
// exports ``connectWs``.  Strip them — every identifier those imports bind
// is injected into the vm context below, and we don't need the export.
//
// Regex is deliberately conservative: only matches lines that BEGIN with
// ``import`` or ``export`` so a stray ``import`` used in a future template
// literal would not be touched.
const transformed = rawSrc
  .split("\n")
  .filter(line => !/^\s*import\s/.test(line))
  .join("\n")
  .replace(/^\s*export\s+function\s+/gm, "function ")
  .replace(/^\s*export\s+/gm, "");

// ─── Harness state ──────────────────────────────────────────────────────────
let renderCallCount = 0;
const dispatchLog = [];   // order of document.dispatchEvent event types
const actionLog = [];     // chronological events for ordering assertions
let capturedWs = null;    // captured WebSocket instance from connectWs

// Stub renderAccounts — tracks call count + records ordering in actionLog.
function renderAccounts() {
  renderCallCount++;
  actionLog.push("renderAccounts");
}

function updateUsageLive(_accounts) {
  actionLog.push("updateUsageLive");
}

function loadSwitchLog(_offset) {
  /* noop */
}

function toast(_title, _detail, _type, _ms) {
  /* noop */
}

function qs(_sel) {
  return null;
}

// Stub document with enough surface for ws.js (dispatchEvent only).
const fakeDocument = {
  dispatchEvent(event) {
    dispatchLog.push(event.type);
    actionLog.push(`dispatch:${event.type}`);
  },
  addEventListener() {},
};

// CustomEvent shim — ws.js does ``new CustomEvent("app:reload-accounts")``.
class CustomEventShim {
  constructor(type, init) {
    this.type = type;
    this.detail = (init && init.detail) || null;
  }
}

// WebSocket shim — captures the instance so we can drive onopen / onmessage
// from the test body after ``connectWs()`` wires them up.
class WebSocketShim {
  constructor(url) {
    this.url = url;
    this.readyState = 0; // CONNECTING
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    capturedWs = this;
  }
  send(_msg) {}
  close() {}
}
WebSocketShim.CONNECTING = 0;
WebSocketShim.OPEN = 1;
WebSocketShim.CLOSING = 2;
WebSocketShim.CLOSED = 3;

// Storage + timers — ws.js uses sessionStorage, setInterval, setTimeout.
const fakeStorage = {
  _data: {},
  getItem(k) { return this._data[k] ?? null; },
  setItem(k, v) { this._data[k] = String(v); },
};

// Mutable state object — ws.js's account_switched handler iterates
// ``state.accounts``, so we seed two cards with one active + waiting.
const state = {
  accounts: [
    { id: 1, email: "a@x.com", is_active: true, waiting_for_cli: true },
    { id: 2, email: "b@x.com", is_active: false, waiting_for_cli: false },
  ],
};

// ─── Build the vm context ───────────────────────────────────────────────────
// Everything ws.js references must be on this object because we stripped
// the ``import`` lines.  The constants module normally exports these two:
const context = {
  // ws.js imports
  WS_PING_INTERVAL_MS: 99999999,
  MAX_RECONNECT_MS: 99999999,
  state,
  qs,
  toast,
  renderAccounts,
  updateUsageLive,
  loadSwitchLog,

  // DOM / host globals ws.js touches
  document: fakeDocument,
  CustomEvent: CustomEventShim,
  WebSocket: WebSocketShim,
  sessionStorage: fakeStorage,
  location: { protocol: "http:", host: "localhost" },
  setInterval: () => 0,
  clearInterval: () => {},
  setTimeout: () => 0,
  JSON,
  Math,
  console,
  Number,
  String,
  Object,
};

vm.createContext(context);
vm.runInContext(transformed, context);

// ─── Test runner ────────────────────────────────────────────────────────────
let passed = 0;
let failed = 0;
function ok(msg) { passed++; console.log(`ok ${passed + failed} ${msg}`); }
function fail(msg, detail) {
  failed++;
  console.log(`not ok ${passed + failed} ${msg}`);
  if (detail) console.log(`  ${detail}`);
}

function assertEq(actual, expected, msg) {
  if (actual === expected) ok(msg);
  else fail(msg, `expected=${JSON.stringify(expected)} actual=${JSON.stringify(actual)}`);
}

function assertTrue(cond, msg, detail) {
  if (cond) ok(msg);
  else fail(msg, detail);
}

// Open the socket so onopen / onmessage are wired.
context.connectWs();
assertTrue(
  capturedWs !== null,
  "connectWs instantiates a WebSocket"
);

// Fire onopen so the internal _replayBoundary / reconnect counter reset.
if (typeof capturedWs.onopen === "function") {
  capturedWs.onopen();
}

// ─── Fix #5: account_switched handler calls renderAccounts ──────────────────
renderCallCount = 0;
dispatchLog.length = 0;
actionLog.length = 0;

const switchedMsg = {
  seq: 42,
  type: "account_switched",
  from: "a@x.com",
  to: "b@x.com",
  reason: "manual",
};
capturedWs.onmessage({ data: JSON.stringify(switchedMsg) });

// Must have mutated state eagerly.
assertEq(state.accounts[0].is_active, false, "fix#5: old active card is_active flipped to false");
assertEq(state.accounts[0].waiting_for_cli, false, "fix#5: old active card waiting_for_cli cleared");
assertEq(state.accounts[1].is_active, true, "fix#5: new target card is_active flipped to true");

// Must have called renderAccounts exactly once.
assertEq(renderCallCount, 1, "fix#5: renderAccounts called after account_switched mutation");

// Must have dispatched app:reload-accounts + app:reload-service (both custom events).
assertTrue(
  dispatchLog.includes("app:reload-accounts"),
  "fix#5: app:reload-accounts dispatched"
);
assertTrue(
  dispatchLog.includes("app:reload-service"),
  "fix#5: app:reload-service dispatched"
);

// Ordering: renderAccounts() MUST run BEFORE the app:reload-accounts dispatch.
// This is the core of fix#5 — without the correct order, there is still a
// flicker window even if both calls happen.
const renderIdx = actionLog.indexOf("renderAccounts");
const reloadIdx = actionLog.indexOf("dispatch:app:reload-accounts");
assertTrue(
  renderIdx !== -1 && reloadIdx !== -1 && renderIdx < reloadIdx,
  "fix#5: renderAccounts runs BEFORE app:reload-accounts dispatch",
  `renderIdx=${renderIdx} reloadIdx=${reloadIdx} actionLog=${JSON.stringify(actionLog)}`,
);

// ─── Fix #3: account_added handler dispatches app:reload-accounts ──────────
renderCallCount = 0;
dispatchLog.length = 0;
actionLog.length = 0;

const addedMsg = {
  seq: 43,
  type: "account_added",
  id: 99,
  email: "new@x.com",
};
capturedWs.onmessage({ data: JSON.stringify(addedMsg) });

// account_added must dispatch app:reload-accounts so main.js reloads /api/accounts.
assertTrue(
  dispatchLog.includes("app:reload-accounts"),
  "fix#3: account_added dispatches app:reload-accounts"
);
// It does NOT need to call renderAccounts directly (there is no card yet to
// render — the reload brings in the full account list).
assertEq(
  renderCallCount, 0,
  "fix#3: account_added does NOT call renderAccounts directly (reload handles it)",
);

// ─── Sanity: account_switched ordering bug regression guard ────────────────
// Make sure the fix did not accidentally call renderAccounts FROM INSIDE the
// mutation loop per-account — that would be O(n) renders on a switch and
// tank the UI under load.
renderCallCount = 0;
state.accounts.push({ id: 3, email: "c@x.com", is_active: false, waiting_for_cli: false });
state.accounts.push({ id: 4, email: "d@x.com", is_active: false, waiting_for_cli: false });
state.accounts.push({ id: 5, email: "e@x.com", is_active: false, waiting_for_cli: false });
capturedWs.onmessage({
  data: JSON.stringify({
    seq: 44, type: "account_switched", from: "b@x.com", to: "c@x.com", reason: "manual",
  }),
});
assertEq(
  renderCallCount, 1,
  "fix#5: renderAccounts called EXACTLY ONCE per account_switched regardless of account count",
);

// ─── Summary ────────────────────────────────────────────────────────────────
console.log(`\n1..${passed + failed}`);
console.log(`# passed: ${passed}`);
console.log(`# failed: ${failed}`);
if (failed > 0) process.exit(1);
