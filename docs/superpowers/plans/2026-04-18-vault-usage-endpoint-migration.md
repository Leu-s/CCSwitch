# Implementation Plan: Vault Usage Endpoint Migration

**Date:** 2026-04-18
**Status:** Planning
**Branch:** TBD

## Overview

Migrate vault account monitoring from POST /v1/messages (triggers 5-hour windows) to GET /api/oauth/usage (read-only, no window trigger). Active account continues using POST /v1/messages. Persist vault usage data in DB for startup cache seeding and window-expired optimization.

## Architecture

| Account Type | Endpoint | Interval | Data Source |
|---|---|---|---|
| Active | POST `/v1/messages` | 15s | Response headers (as now) |
| Vault (window open) | GET `/api/oauth/usage` | 10-15 min | JSON body, DB + cache |
| Vault (window expired) | None | Never | DB, cache (0% synthesized) |
| Vault (pre-switch) | GET `/api/oauth/usage` | On-demand | Fresh JSON body |

## Phases

### Phase 1: API Layer (anthropic_api.py)
- Add `fetch_usage(access_token)` -- GET /api/oauth/usage
- Parse JSON body, convert resets_at ISO to epoch
- Same return shape as probe_usage
- Dependencies: None

### Phase 2: Data Model (models.py + migration)
- Add 5 columns to Account
- Alembic migration with batch_alter_table
- Dependencies: None

### Phase 3: Configuration (config.py)
- poll_interval_vault, poll_interval_vault_min, anthropic_usage_url
- Dependencies: None

### Phase 4: Cache Layer (cache.py)
- Add seed_usage() helper for startup seeding
- Dependencies: None

### Phase 5: Background Loop (background.py)
- Split _process_single_account into active + vault paths
- Vault: fetch_usage + DB write + window-expired optimization
- Dependencies: Phase 1, 2, 3

### Phase 6: Switcher (switcher.py)
- get_next_account: Tier-0 window-expired fast path
- On-demand fresh check before switch
- Dependencies: Phase 5

### Phase 7: Startup (main.py)
- Seed cache from DB after init_db()
- Dependencies: Phase 2, 4

### Phase 8: Schemas (schemas.py)
- Optional: add window_expired computed field
- Dependencies: None

### Phase 9: Frontend (accounts.js)
- Window-expired Idle display
- Dependencies: None

### Phase 10: Tests
- Dependencies: All

### Phase 11: Cleanup + CLAUDE.md
- Dependencies: All

## Dependency Graph

```
Phase 1 (API) -------+
Phase 2 (Model) -----+
Phase 3 (Config) ----+--> Phase 5 (Background) --> Phase 6 (Switcher)
Phase 4 (Cache) -----+                              |
Phase 8 (Schemas) ---+                              v
Phase 9 (Frontend) [independent]              Phase 7 (Startup)
                                                   |
                                                   v
                                              Phase 10 (Tests)
                                                   |
                                                   v
                                              Phase 11 (Cleanup)
```
