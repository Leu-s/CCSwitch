# Master Switch Redesign

**Date:** 2026-04-14
**Status:** Approved (autonomous mode)

## Problem

Two controls govern auto-switching, and they look/feel like duplicates:

1. **Header pill** (`#service-toggle-btn`) — the *real* master, backed by
   `service_enabled`. Styled like a status badge; users do not read it as
   clickable.
2. **Sidebar toggle** (`#auto-switch-cb` in `.sl-auto-row`) — backed by a
   separate `auto_switch_enabled` flag. It existed so the service could
   poll-and-monitor without auto-switching, but that mode is rare and
   confusing alongside the master switch.

The primary entry point to the app's core functionality is currently neither
obvious nor unique.

## Decision

Collapse the two controls into a single, obvious master switch.

### Backend

- `auto_switch_enabled` is removed as a user-facing setting:
  - Dropped from `SETTING_DEFAULTS` in `settings_service.py`.
  - Dropped from `ALLOWED_KEYS` in `routers/settings.py` — PATCH returns 403.
  - `maybe_auto_switch()` no longer reads the flag. It runs whenever
    `service_enabled=true` (which `poll_usage_and_switch` already gates).
- No Alembic migration — the stale DB row is harmless; reads stop.

### Frontend

- `#sl-auto-row` removed from `index.html`. Related CSS
  (`.sl-auto-row`, `.sl-auto-label`) deleted.
- `loadAutoSwitchSetting()` and `#auto-switch-cb` listener removed from
  `service.js`; `updateServiceUI()` stops touching `autoRow`.
- `main.js` no longer imports or calls `loadAutoSwitchSetting`.

### Header button redesign (the primary UI change)

Replace the small status-pill look with a large, explicit master-switch card.

**Anatomy:**

```
┌───────────────────────────────────────────────┐
│ [ ◯━━━ ]  Auto-switch              OFF        │  ← off
└───────────────────────────────────────────────┘
┌───────────────────────────────────────────────┐
│ [ ━━━● ]  Auto-switch              user@x.com │  ← on
└───────────────────────────────────────────────┘
```

- A large toggle track (≈46×26, bigger than the existing `.toggle` at 36×20).
- Bold **Auto-switch** label.
- Right-side slot:
  - When OFF: muted "OFF" text.
  - When ON: active account email in a pill.
- Whole card is one button:
  - `cursor: pointer`, solid elevated background, subtle shadow.
  - Hover: background lightens, slight lift.
  - Focus: accent ring (keyboard a11y).
  - `role="switch"`, `aria-checked` toggled on click.
  - Disabled state during API call (loading spinner).
- Two visual states:
  - OFF: muted card tint, grey track, red-ish dot (matches existing danger hue).
  - ON: card tint picks up accent colour, green track (`--success`), pulsing dot.

### Test updates

- Remove `auto_switch_enabled` references from `test_settings_router.py`,
  `test_settings_service.py`, `test_e2e_smoke.py`.
- Delete `test_auto_switch_disabled_skips_check` in `test_background.py`
  (behaviour removed).
- Rewrite `_make_db_for_one_account` / `_make_db_for_auto_switch` /
  `test_integration_auto_switch._make_db` so call-count mappings drop the
  auto_switch_enabled query (mapping shifts by one).

## Out of scope

- No changes to manual-switch buttons on cards.
- No changes to backend polling cadence or cache.
- No visual change to Switch Log body / pagination.
