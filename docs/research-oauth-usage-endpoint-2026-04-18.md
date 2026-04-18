# Research: /api/oauth/usage Endpoint for CCSwitch

**Date:** 2026-04-18
**Context:** Evaluating migration from POST /v1/messages probe to GET /api/oauth/usage for usage monitoring.

## Executive Summary

The `/api/oauth/usage` endpoint is **severely problematic for CCSwitch's multi-account polling use case**. It has aggressive per-access-token rate limits (~5 requests before persistent 429), requires the `User-Agent: claude-code/<version>` header to avoid an even stricter bucket, and the `retry-after: 0` header it returns is misleading. While it is read-only and does NOT trigger inference windows (which is the desired property), the rate-limiting characteristics make it unsuitable for polling N accounts every 15 seconds.

---

## 1. Rate Limiting on /api/oauth/usage (CRITICAL)

**The endpoint is aggressively rate-limited per access token.** Community investigation (anthropics/claude-code#30930, #31021, #31637, #47507) found:

- **~5 requests per access token** before hitting persistent 429.
- Once 429'd, the endpoint **never recovers** on the same access token -- no matter how long you wait (tested 5+ minutes, 30 minutes, even hours).
- `retry-after: 0` header is returned, which is misleading -- immediate retry still fails.
- Rate limits are **per-access-token, NOT per-account**. Refreshing the token gives a fresh rate limit window.

**Impact on CCSwitch:** With 15-second polling across N accounts, each account would exhaust its ~5-request budget within 75 seconds. After that, the endpoint becomes permanently useless for that token.

### User-Agent header requirement

A separate rate limit bucket exists based on the `User-Agent` header:
- `User-Agent: claude-code/<version>` --> generous rate limit, works at ~180-second intervals.
- Any other User-Agent (curl, custom app) --> **instant persistent 429**.

CCSwitch already sends `User-Agent: claude-code/2.1.104`, so it would land in the generous bucket -- but even that bucket is only safe at **180-second intervals per token**, not the 15-second polling CCSwitch needs.

---

## 2. Token Requirements and Expiration Behavior

### Expired tokens return 401
- If `access_token` is expired (which happens after ~8-24 hours depending on version), `/api/oauth/usage` returns `401 - OAuth token has expired`.
- The endpoint does NOT auto-refresh tokens.
- CCSwitch's vault accounts store tokens that may be hours or days old -- their access tokens will frequently be expired.

### Vault tokens work IF valid
- The endpoint accepts any valid Bearer access_token, regardless of whether it's the "active" session token. If CCSwitch's vault stores a non-expired access_token, the endpoint will return usage data for the account associated with that token.
- However, since CCSwitch never refreshes vault accounts proactively (reactive-only refresh on probe-401), vault access_tokens will typically be expired.

### Consequence for CCSwitch
Using `/api/oauth/usage` for vault accounts would force CCSwitch to refresh vault tokens proactively (to keep them non-expired) -- **exactly the behavior CCSwitch eliminated in the April 16 simplification** (reactive-only refresh). This would reintroduce the proactive refresh pattern and its associated problems (broken-chain triggers, thundering herd, etc.).

---

## 3. Does Token Refresh Trigger a New 5-Hour Window?

**No direct evidence that token refresh alone triggers a 5-hour inference window.** The 5-hour window is triggered by inference API calls (POST /v1/messages), not by the OAuth token refresh endpoint. Token refresh is a standard OAuth 2.0 flow that issues new credentials without performing inference.

However, there is an important subtlety: **refreshing a token to query /api/oauth/usage, then using that fresh token for inference, could still synchronize windows** if the inference call happens immediately after. The window starts on the first inference call with that token pair, not on refresh.

The current CCSwitch probe (POST /v1/messages with haiku/1-token) DOES trigger inference windows -- this is the core problem the migration aims to solve. But refreshing tokens just to read /api/oauth/usage introduces its own risks (single-use refresh tokens, race conditions, credential desync with Claude Code CLI).

---

## 4. Stale / Incorrect Data Reports

Multiple reports of stale data from `/api/oauth/usage`:

- **anthropics/claude-code#31637**: Dashboard showed 61% when actual usage was 42% -- data can be **hours stale**.
- **oh-my-claudecode#1472**: Stale usage data reported as a distinct bug.
- The endpoint appears to cache server-side and may not reflect real-time usage changes.

In contrast, the inference API response headers (`anthropic-ratelimit-unified-*`) provide **real-time, per-response** utilization data. CCSwitch currently gets fresh data on every probe because these headers are returned on every /v1/messages response (including 429 responses).

---

## 5. Deprecation / Instability Signals

### Undocumented endpoint
- `/api/oauth/usage` is **not in Anthropic's official API documentation**. It was discovered by reverse-engineering Claude Code's `/usage` command.
- Required headers include the undocumented `anthropic-beta: oauth-2025-04-20`.

### Community migration away from it
- As of Claude Code v2.1.80 (March 19, 2026), rate_limits data is exposed directly in statusLine JSON stdin -- **Anthropic's own solution to avoid calling /api/oauth/usage**.
- Multiple community tools (oh-my-claudecode, claude-hud, custom statuslines) have migrated to reading stdin rate_limits or inference headers instead of polling this endpoint.
- Issue #31637 has a prominent comment: "If you're using a custom statusline script, the real fix is to **stop calling /api/oauth/usage entirely**."

### Behavioral changes
- Around March 22-23, 2026, server-side rate limit changes caused the `rate_limits` field to temporarily disappear from statusLine JSON for some users (anthropics/claude-code#40094).
- The endpoint behavior is subject to change without notice since it's undocumented.

---

## 6. Response Format

When it works, the endpoint returns:
```json
{
  "five_hour": {
    "utilization": 33.0,
    "resets_at": "2026-04-11T07:00:00.528743+00:00"
  },
  "seven_day": {
    "utilization": 10.0,
    "resets_at": "2026-04-15T00:00:00+00:00"
  }
}
```

Note: `utilization` is 0-100 (percentage), `resets_at` is ISO 8601. This differs from CCSwitch's current format where `resets_at` is a Unix epoch integer from the inference headers.

---

## 7. Comparison Table: POST /v1/messages Probe vs GET /api/oauth/usage

| Dimension | POST /v1/messages (current) | GET /api/oauth/usage |
|---|---|---|
| Triggers 5h window? | YES (inference) | NO (read-only) |
| Rate limit budget | Per-model RPM (generous) | ~5 req/access_token, then permanent 429 |
| Expired token handling | 401, CCSwitch handles reactively | 401, would need proactive refresh |
| Data freshness | Real-time (per-response headers) | Potentially stale (hours lag reported) |
| Works with vault tokens? | Yes (any valid access_token) | Only if access_token is non-expired |
| Documented? | Yes (official API) | No (reverse-engineered, undocumented) |
| Deprecation risk | None (core inference API) | High (undocumented, community abandoning) |
| Per-account polling? | ~60 RPM per model | ~1 req / 180s (with correct User-Agent) |
| Response format | Headers on every response | JSON body |
| Data granularity | 5h, 7d, 7d_sonnet, status, fallback | 5h, 7d only |

---

## 8. Recommendations

### DO NOT migrate to /api/oauth/usage for periodic polling.

The endpoint's rate limits make it fundamentally unsuitable for CCSwitch's polling cadence (every 15s across N accounts). It would require:
1. Proactive token refresh for every vault account (reintroducing the broken-chain problem).
2. Dramatically reduced polling frequency (180s+ per account minimum).
3. Complex fallback/retry logic for the persistent 429 problem.
4. Acceptance of stale data (hours lag).
5. Risk of breaking changes to an undocumented endpoint.

### Alternative approaches worth investigating:

1. **Reduce probe cost**: The current haiku/1-token probe is already minimal. Consider whether it actually triggers a full 5-hour window or just a minimal RPM cost. The anthropic-ratelimit-unified headers show utilization -- verify whether the 1-token probe counts toward the 5-hour utilization at all (it should count minimally since it's haiku, not opus).

2. **Probe only on-demand**: Instead of polling all N accounts every 15s, only probe the active account periodically and probe vault accounts on-demand (before a potential switch). This reduces total window-triggering probes to 1 account.

3. **Hybrid approach**: Use /api/oauth/usage as a *supplementary* source with aggressive caching (5+ minute TTL), falling back to the inference probe when the usage endpoint is 429'd. Never use it as the primary polling mechanism.

4. **Parse inference headers from actual Claude Code usage**: If Claude Code is running on the active account, its inference calls already return rate-limit headers. CCSwitch could potentially read cached rate-limit data from Claude Code's runtime rather than probing independently.

---

## Sources

- anthropics/claude-code#30930: Persistent 429 for Max users (37 upvotes)
- anthropics/claude-code#31021: Persistent 429 rate limit (12 upvotes)
- anthropics/claude-code#31637: Aggressive rate limits making monitoring unusable
- anthropics/claude-code#32503: /usage command fails with rate_limit_error
- anthropics/claude-code#47507: /usage returns persistent rate_limit_error (April 2026)
- anthropics/claude-code#39198: Stale auth masquerading as rate limit
- anthropics/claude-code#27915: Feature request for statusLine rate_limits (shipped v2.1.80)
- anthropics/claude-code#40094: rate_limits field missing from statusLine JSON
- oh-my-claudecode#194: Expired OAuth tokens not refreshed for usage API
- oh-my-claudecode#1366: 429 handling fix
- oh-my-claudecode#1470: Persistent 429 after fix
- claude-hud#173: Persistent 429 error display
- Claude-Code-Usage-Monitor#202: Authoritative documentation of endpoint behavior
- claudeusage-mcp: MCP server using the endpoint with 60s cache
- claudecodecamp.com: Reverse engineering of Claude Code's usage limits
