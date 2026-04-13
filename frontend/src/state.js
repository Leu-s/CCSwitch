// Shared mutable application state — a single object so all modules see
// the same reference when it is mutated in place.
export const state = {
  accounts: [],
  service: { enabled: false, active_email: null, default_account_id: null },
  switchLog: [], logPage: 0, logTotal: 0,
  sessions: [],
  monitors: [],
  currentTab: "accounts",
};

// Per-account slider debounce timers: accountId → timer handle
export const _sliderDebounce = new Map();
