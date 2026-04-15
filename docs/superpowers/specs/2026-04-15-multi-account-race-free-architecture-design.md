# CCSwitch Multi-Account Race-Free Architecture Design

**Status:** **SUPERSEDED** — see `2026-04-15-vault-swap-architecture.md`.

> This 19-candidate design-space exploration assumed the user needed
> **N parallel `claude` CLIs, one per account** to extend rate-limit
> capacity. Under that assumption, isolation (per-user POSIX accounts,
> per-account VMs/containers), supervisor patterns (per-account
> LaunchAgents, refresher daemons), and observer patterns (fsevents,
> launchd WatchPaths, ESF) were all weighed on equal footing.
>
> **Why superseded.** The user's real workflow, confirmed empirically
> during the 2026-04-15 session, is **N cmux panes sharing ONE account
> at any given time** — the user switches the credential set in place
> when the active account hits its rate limit, then continues on all
> panes. The Vault-Swap architecture collapses the entire design space
> to a single obvious answer: two disjoint Keychain namespaces, one
> atomic swap operation, one tmux nudge, done. The elaborate race-
> avoidance machinery the families A–H were designed to provide is
> unnecessary: the race is eliminated because CCSwitch and the CLI
> literally never see the same Keychain entry at the same time.
>
> The failure taxonomy (F-RACE, F-DORMANT, F-BOOT, F-UPSTREAM,
> F-USER, F-ANTHROPIC) and the rejected-alternatives research (E3
> ccflare, E4 setup-token) remain useful background material; the
> chosen design does not.

---

## Original design (retained for history)

**Author:** Architecture research session, 2026-04-15
**Informed by:** 4 parallel research agents (alternatives generator, POC adversarial attacker, official extension points mapper, operational durability auditor)

---

## 1. Problem restatement

### 1.1 Real-world symptom

On the night of 2026-04-14, user's CCSwitch running on a Mac with 5 active accounts
and N parallel `claude` CLI sessions (via cmux) experienced a cascade failure:
4 of 5 accounts had their OAuth `refresh_token` rejected by Anthropic (HTTP 400);
the 5th account's access_token was expired but refresh not yet attempted; auto-switching
cycled 49 times between accounts 1 and 2 in a 14-minute window at 21:04–21:18,
each switch burning another refresh_token. The user woke up to 5 dead accounts.

### 1.2 Symptoms vs root causes

**Surface symptoms** (what the user saw):
- All 5 accounts marked `stale_reason = "Refresh token rejected (400)"`
- Switch log showed 49 rapid-fire switches
- UI showed red stale cards requiring re-login

**Root cause** (from research + CCSwitch source code analysis):

The current "active-ownership refresh model"
(docs/superpowers/specs/2026-04-14-active-ownership-refresh-fix-design.md) assumes
**exactly one Claude Code CLI process is active at any time**, with all other accounts
being "inactive" and owned by CCSwitch for refresh purposes. This assumption is
fundamentally invalid for this user's workflow:

- User's workflow: **10–20 parallel `claude` CLI sessions simultaneously**, each with
  its own `CLAUDE_CONFIG_DIR=/path/to/account-N`. Each CLI owns its own account's
  refresh lifecycle.
- CCSwitch's assumption: only 1 CLI at a time. Every account that isn't currently
  pointed at by `~/.ccswitch/active` is "inactive" → CCSwitch refreshes it.
- Conflict: from CLI-B's perspective, account-B is active (CLI-B is using it). From
  CCSwitch's perspective, account-B is inactive (it's not the pointer target). Both
  refresh. Anthropic's OAuth `/oauth/token` issues single-use refresh_tokens per
  [RFC 9700] → one refresher wins, the other's stored token is burned (HTTP 400 on reuse).

**Therefore:** CCSwitch races with **every CLI except the one pointed at by `~/.ccswitch/active`**.
With N=20 parallel CLIs, CCSwitch is racing with ~19 refresh owners every
poll cycle, not 0.

### 1.3 Upstream context (Claude Code CLI 2.1.101)

The Claude Code CHANGELOG for release 2.1.101 reports:
> Fixed multiple concurrent Claude Code sessions requiring repeated re-authentication
> when one session refreshes its OAuth token.

The user's installed CLI is **2.1.109** (verified via `claude --version`), so the
upstream fix IS active on their system. This resolves CLI↔CLI races but does **NOT**
resolve CLI↔CCSwitch races, because CCSwitch is not cooperating with the CLI's new
coordination primitive (whatever it is; exact mechanism not publicly documented).

**Conclusion:** The cascade failure is a CCSwitch-side bug. The fix must eliminate
CCSwitch↔CLI refresh races for all accounts, not just the active-pointer target.

---

## 2. Non-negotiable requirements

The final architecture **must** satisfy ALL of the following:

| # | Requirement | Source |
|---|---|---|
| R1 | Zero refresh races between CCSwitch and any Claude Code CLI | Root-cause analysis |
| R2 | Support N≥20 parallel `claude` CLI sessions, each on a different account | User workflow |
| R3 | No MITM of prompt traffic (no proxy between CLI and `api.anthropic.com`) | Hard boundary |
| R4 | No hostname redirection tricks (/etc/hosts + local TLS CA) | Hard boundary |
| R5 | No token interception via middlebox | Hard boundary |
| R6 | No protocol spoofing / fake endpoints | Hard boundary |
| R7 | No "wrappers" around `claude` binary that user must invoke | User rejection of V3 |
| R8 | Must use only officially documented OR read-only surfaces | Hard boundary |
| R9 | Must survive macOS sleep/wake without bulk re-login requirement | Real operational need |
| R10 | Dormant accounts (no CLI active) must stay usable | Real operational need |
| R11 | Must be resilient to Claude Code CLI rewrites | Upstream stability goal |
| R12 | CCSwitch UI must show live rate-limit data for auto-switch decisions | Product value |
| R13 | Must not require sudo setup steps | Usability |
| R14 | Must not require user to install TLS CAs | Usability |

**Soft requirements (nice to have, not blocking):**
- Minimal LOC delta from current architecture
- Clean migration path from current DB schema
- Preservation of existing features (credential targets mirror, tmux nudge, force-refresh UI)

---

## 3. Failure model

Exhaustive taxonomy of what can fail in a multi-account CLI coordination system.
Each failure class constrains the viable designs.

### 3.1 Refresh-lifecycle races

- **F-RACE-1:** Two processes both decide to refresh the same account's `refresh_token`
  concurrently. Both POST `/oauth/token` with the same token. Anthropic's single-use
  semantics mean the second gets HTTP 400. If the loser is the CLI, user sees
  "re-login required" in their terminal. If the loser is CCSwitch, CCSwitch marks
  stale, auto-switches, which may trigger ANOTHER refresh attempt → cascade.
- **F-RACE-2:** CLI refreshes; CCSwitch's cached `access_token` is stale for up to
  `poll_interval` seconds; CCSwitch probes with stale token, gets 401, marks account
  as "waiting for CLI."
- **F-RACE-3:** CLI refreshes; Keychain write is in-flight; CCSwitch reads and gets
  partial/corrupt JSON. Mitigated by atomic Keychain writes in practice.

### 3.2 Dormant-account state decay

- **F-DORMANT-1:** Account B's CLI hasn't been used in 60+ minutes. Access_token
  expires (Anthropic default ~1h). No refresh happens because no CLI needs it.
  CCSwitch's probe fails 401.
- **F-DORMANT-2:** User sleeps the Mac for 8h. All access_tokens expire during
  sleep. On wake, all 20 accounts show "expired." No CLI activity to warm them.
- **F-DORMANT-3:** User adds an account via OAuth, walks away for 65 min. The access_token
  they just created is expired. First interaction with that account requires a refresh.

### 3.3 Bootstrap and recovery

- **F-BOOT-1:** User reboots Mac. CCSwitch starts. Keychain may be locked (corporate
  policy). `security find-generic-password` blocks or returns empty. All accounts
  appear stale.
- **F-BOOT-2:** CCSwitch crashes mid-switch. Pointer partially updated. Credential
  targets mirror partially updated. Active identity ambiguous.
- **F-BOOT-3:** SQLite `ccswitch.db` corrupted by unclean shutdown. CCSwitch refuses
  to start. User loses switch history.

### 3.4 Upstream API surface changes

- **F-UPSTREAM-1:** Anthropic changes Keychain entry name / JSON schema.
  CCSwitch can't read any tokens.
- **F-UPSTREAM-2:** Anthropic changes `/oauth/token` contract (new fields required,
  old fields removed). CCSwitch's refresh call fails; CLI adapts via upgrade.
- **F-UPSTREAM-3:** Anthropic deprecates `CLAUDE_CONFIG_DIR`. Isolation primitive
  gone. All multi-account tools break.
- **F-UPSTREAM-4:** Anthropic adds official multi-account support. CCSwitch becomes
  redundant (good outcome, but plan for clean deprecation).
- **F-UPSTREAM-5:** Anthropic adds rate-limit observability API (e.g. `GET /v1/usage`).
  Current probe strategy obsolete; want clean migration.

### 3.5 User-action edge cases

- **F-USER-1:** User sets same `CLAUDE_CONFIG_DIR` in two terminals. Two CLIs on same
  account race on refresh. NOT CCSwitch's problem unless we can detect & warn.
- **F-USER-2:** User runs bare `claude` without `CLAUDE_CONFIG_DIR`. CLI reads legacy
  Keychain entry `Claude Code-credentials`. Which account is it using? Depends on
  CCSwitch's last switch action.
- **F-USER-3:** User manually deletes `CLAUDE_CONFIG_DIR` of an account. CCSwitch
  has a ghost row referring to missing dir.
- **F-USER-4:** User runs two CCSwitch instances (`CCSWITCH_SERVER_PORT` override).
  Split-brain on shared DB + Keychain.

### 3.6 Anthropic-side operational events

- **F-ANTHROPIC-1:** Anthropic API down for 30 min. All probes fail. CCSwitch must
  NOT interpret this as account failure.
- **F-ANTHROPIC-2:** Anthropic revokes a user's refresh_token (ToS violation, payment
  issue). Next refresh gets HTTP 400 with "refresh_token invalid." Need clear UI signal.
- **F-ANTHROPIC-3:** Anthropic adds per-IP probe rate limit. 20-account probe pattern
  could hit it. Need graceful degradation.

---

## 4. Design space map

Research Agent 1 generated 19 candidates clustered into 8 families. The invariant
each family enforces is the mechanism that can prevent F-RACE-1.

### Family A — Isolation
*Invariant: disjoint credential namespaces. Race is structurally impossible.*
- A1. Per-user macOS accounts (separate POSIX UIDs, separate Keychains)
- A2. Per-account OrbStack Linux VMs
- A3. Per-account Docker/Podman containers

### Family B — Supervisor / Warm-Keeper
*Invariant: exactly one process per account refresh window, coordinated via process cardinality + official CLI invocation.*
- B1. Per-account LaunchAgent Warm Keeper (curl-based ping)
- B2. Per-account Refresher Daemons (decomposed refresher + dashboard)
- B3. Warm-Keeper via `claude --print " "` (official CLI path)

### Family C — Observer (CCSwitch never writes)
*Invariant: CCSwitch never initiates refresh → zero CCSwitch-side race surface.*
- C1. fsevents/kqueue reconciliation (filesystem watch)
- C2. launchd WatchPaths trigger (OS-declarative)
- C3. Endpoint Security Framework (ESF) Keychain-access observer
- C4. DTrace probe (dominated by ESF on macOS)
- C5. Read-only + manual "Activate dormant" UI button
- **POC** (research baseline; strict read-only)

### Family D — Cooperative coordination (broken)
*Invariant: all mutators cooperate via shared state. Breaks because CLI doesn't cooperate.*
- D1. Declarative desired-state controller with leases — dominated by B1 (no enforcement)
- D2. Shared SQLite leases — dominated (CLI ignores them)
- D3. fcntl-gated mutex — dominated (relies on CLI taking advisory lock, which it doesn't reliably do)

### Family E — Hybrid / Stratified
*Invariant: ownership is classified per-account by recency; transitions are event-driven.*
- E1. Active=Observer, Inactive=Refresher, Idle=WarmKeeper
- E2. XPC + ESF (dominated by E1)
- **F+A+** (research baseline; probabilistic variant of E1)

### Family F — Session Ephemerality (broken for refresh_tokens)
*Invariant: sessions don't share refresh state. Fails because refresh_token is server-side.*
- F1. Ephemeral per-session config dirs — dominated (refresh_token is server resource, forking clients doesn't help)

### Family G — Reactive Refresh
*Invariant: refresh driven by 401 observation, not predictive expiry. Dominated by F+A+ (same reduction, simpler variant).*
- G1. 401-reactive refresh

### Family H — Dead Ends
- H1. MCP credential broker — category error (MCP is tool surface, not auth surface)

---

## 5. Detailed candidate analysis

Analyzing top candidates — those that are Pareto-efficient (not strictly dominated).

### 5.1 Status Quo: Active-Ownership (Arch 1)

**Core idea:** CLI owns refresh for active-pointer account; CCSwitch owns refresh for all others.

**Required mechanisms:**
- Active-ownership gate in `_process_single_account` (already implemented in `backend/background.py`)
- `_REFRESH_SKEW_MS_INACTIVE = 20 * 60 * 1000` for defense-in-depth
- Per-account `asyncio.Lock` via `_force_refresh_locks`

**Invariants enforced:** Single-writer per refresh lifecycle — **but only** when the
"1 active CLI" assumption holds. Breaks when N CLIs run simultaneously.

**Dependency on upstream internals:**
- Keychain service name convention (legacy + hashed variants)
- JSON schema of Keychain credential blob
- OAuth `/oauth/token` contract

**Operational model:** Polling every 15s (active clients) / 60s+ (idle). Refresh-ahead 20 min
window for inactive accounts. Per-account 120s→3600s backoff on 429.

**Failure modes:**
- **Fatal:** F-RACE-1 when user has N>1 parallel CLIs (user's actual workflow)
- F-BOOT-1 (Keychain locked → stale cascade)
- F-UPSTREAM-1 (Keychain schema change)

**Why it might win:** Proven in production for single-CLI workflows. Minimal LOC delta (already implemented). Handles F-DORMANT-1 by silently refreshing ahead of expiry.

**Why it might fail:** Violates R1 for user's N=20 workflow. **This is why accounts burned overnight.**

**Implementation scope:** Already shipping.
**Long-term maintenance:** Medium. Keychain format changes require rewrites.

### 5.2 POC: Read-only CCSwitch

**Core idea:** CCSwitch never refreshes tokens. Only reads Keychain + probes `/v1/messages`.

**Required mechanisms:**
- Remove `refresh_access_token` calls from `background.py`
- Remove `save_refreshed_token` calls
- Remove `force_refresh_config_dir` helper
- Remove Keychain writes from `activate_account_config`
- Add "needs activity" UI state for expired access_tokens

**Invariants enforced:** CCSwitch never initiates mutation → zero CCSwitch-side race surface.

**Dependency on upstream internals:** READ-ONLY on Keychain (still format-dependent).

**Failure modes:**
- **Fatal:** F-DORMANT-1, F-DORMANT-2. User sleeps laptop → 20 cards stuck "needs activity."
- **Fatal:** F4 (Agent 2): next-account-selection after auto-switch uses 8h-old data → picks exhausted account → cascade
- F7 (Agent 2): tmux nudge becomes incoherent (CCSwitch can't mirror new identity)
- F8: credential targets mirror ceases to function → bare `claude` split-brain

**Why it might win:** Strongest ToS posture. Zero refresh surface. Simplest to reason about.

**Why it might fail:** Agent 2's 20/25+ severity attacks. **Ships a worse product for user's workflow.**

**Verdict:** **REJECTED** standalone. Agent 2's analysis is convincing; Agent 4 concurs
Scenario 7 (sleep/wake) is a showstopper.

### 5.3 F+A+: Fingerprint Observation + Large Skew

**Core idea:** Passive observer of Keychain fingerprints. CCSwitch refreshes only when
fingerprint stable >2h (confirmed idle).

**Required mechanisms:**
- Keychain fingerprint hash cache
- 2h observation window
- Per-account classification: active / warm / dormant

**Invariants enforced:** 99.9% race reduction (probabilistic).

**Failure modes:**
- F-RACE-1 residual 0.1% (fat-tail)
- F-BOOT-1 worsened by observer reset
- F-UPSTREAM-1 worsened by extra fingerprint dependency
- Scenario 3 (Agent 4): 2h post-reboot hole where CCSwitch can't refresh

**Verdict:** **DOMINATED** by Warm-Keeper Stratified (W). Same philosophy, weaker enforcement.

### 5.4 Warm-Keeper Stratified (W) — RECOMMENDED

**Core idea:** Three-regime ownership model. CCSwitch never refreshes. Per-account
LaunchAgent warm-keeper uses the official `claude` CLI to keep tokens warm when no
interactive session is using the account.

**Required mechanisms:**

1. **Per-account LaunchAgent** at `~/Library/LaunchAgents/com.ccswitch.warm.<sha8>.plist`.
   - `StartInterval = 900` (15 min) or conditional trigger
   - ProgramArguments invokes a tiny shell script:
     ```sh
     #!/bin/sh
     export CLAUDE_CONFIG_DIR=/path/to/account-N
     exec /usr/local/bin/claude --print " " --max-tokens 1 >/dev/null 2>&1
     ```
   - Writes plist + `launchctl bootstrap gui/<uid>` at account creation
   - Removes plist + `launchctl bootout` at account deletion
2. **CCSwitch read-only for refresh.** Remove all `refresh_access_token` call sites.
3. **CCSwitch retains writes that DON'T race with refresh:**
   - `~/.ccswitch/active` pointer (safe: unique to CCSwitch)
   - `.claude.json` mirror writes (safe: not hashed Keychain, CLI doesn't race on mirrored `oauthAccount` copy)
   - Legacy `Claude Code-credentials` Keychain entry (**conditional**: only when a user-opted-in system-default target is enabled, writes happen only on explicit switch, CLI shouldn't concurrently refresh the legacy entry because the CLI always uses the hashed entry for a `CLAUDE_CONFIG_DIR`-aware invocation)
4. **Probe with current Keychain access_token** for rate-limit headers (same as today).
5. **Optional: event-driven probe via fsevents** on Keychain DB — replaces polling cadence.
   Reduces compliance footprint (no metronomic probe burst).

**Invariants enforced:**
- **α (Mutation Ownership Locality):** Per-account refresh owner is always the CLI
  (either user's interactive CLI or the warm-keeper LaunchAgent). CCSwitch is never
  a refresher. Process cardinality guarantees one writer.
- **β (Observation Replaces Prediction):** CCSwitch's probe decisions gated on
  Keychain reads, not on pre-emptive refresh.
- **γ (Official-Surface Fidelity):** Warm-keeper invokes the real `claude` binary.
  Any future CLI change (auth primitive, endpoint, schema) is auto-inherited.
- **δ (Event-Driven Triggers):** Optional fsevents-based probe replaces polling.

**Dependency on upstream internals:**
- Keychain service name convention (read-only, same as every candidate)
- `claude` binary existence at `/usr/local/bin/claude` or resolvable via PATH
- `launchd` (stable macOS primitive since 10.4)

**Operational model:**
- 20 accounts × 96 pings/day = 1920 `/v1/messages` API calls/day across all accounts
- Each ping: ~1–5 tokens (billable, but negligible on Max/Pro subscriptions)
- Per-account parallelism: each LaunchAgent independent
- Token TTL (typical 1h) << warm-keeper interval (15 min) → tokens always fresh

**Failure modes & mitigations:**

| Failure | Impact | Mitigation |
|---|---|---|
| F-RACE-1 CLI↔warm-keeper on same account | Both run `claude --print`. Both observe same Keychain. Upstream's 2.1.101 fix coordinates them. | Rely on upstream fix. Skip warm-keeper if fingerprint changed in last 60s (user CLI just refreshed, no need). |
| F-DORMANT-1, F-DORMANT-2 | Warm-keeper fires every 15 min → tokens refreshed → no dormant drift | None needed |
| F-BOOT-1 Keychain locked at start | First warm-keeper fire fails; next succeeds after unlock | Detect exit code 51, back off 5 min, retry |
| F-UPSTREAM-1 Keychain schema change | Read path breaks (same as all candidates) | Version-gate on `claude --version`, alert |
| F-UPSTREAM-2 `/oauth/token` contract change | Warm-keeper inherits via CLI upgrade | Auto-resolved |
| F-UPSTREAM-4 Official multi-account lands | Warm-keepers become redundant | Clean retirement: delete plists, stop writing pointer |
| F-USER-4 Two CCSwitch instances | Pointer writes race (small surface) | `fcntl.flock` on `~/.ccswitch/lock` at lifespan start |
| F-ANTHROPIC-2 refresh_token revoked | Warm-keeper ping gets 400; CCSwitch's probe also 401 | Explicit UI state + Re-login button |
| F-ANTHROPIC-3 probe rate limit | Warm-keeper pings count against rate-limit | Add exponential backoff on 429 for warm-keepers; drop to 30 min interval |

**Why it might win:**
- Fixes F-RACE-1 by construction (CCSwitch never refreshes)
- Fixes F-DORMANT by construction (warm-keeper keeps tokens alive)
- Upstream-resilient: uses official CLI path, inherits all future CLI changes
- Clean ToS posture: warm-keeper is indistinguishable from a user running `claude --print` themselves
- Preserves existing features: mirror on switch still works (no refresh race), tmux nudge still has fresh credentials to nudge into, force-refresh UI can trigger warm-keeper `launchctl kickstart`

**Why it might fail:**
- 1920 API calls/day footprint (compliance slight increase; mitigated by conditional skip)
- 20 LaunchAgents on user's system (minor operational footprint)
- `/usr/local/bin/claude` path must be discoverable (resolved via shell PATH at agent creation time)
- Users on Homebrew ARM vs Intel Mac have different `claude` paths; need detection

**Implementation scope:** Medium. ~400 LOC added (plist management + launchctl integration), ~300 LOC removed (refresh pipeline). Net: ~+100 LOC.

**Long-term maintenance:** Low. LaunchAgents are stable; `claude --print` inherits CLI evolution.

### 5.5 Per-Account Containers (A3)

**Core idea:** Each account runs inside its own Docker/Podman container. No shared Keychain.

**Invariants:** Namespace isolation = no shared mutable state.

**Why it might win:** Hermetic. Provably zero race. Scales to 100 accounts.

**Why it might fail:**
- Requires user to change workflow (cmux spawns `docker exec` instead of `claude`)
- Docker Desktop file-sharing is slow on macOS
- 20× container overhead

**Verdict:** Not dominated but operationally heavy. Falls below W in convenience.

### 5.6 ESF Keychain Observer (C3)

**Core idea:** Subscribe to Endpoint Security Framework events for Keychain DB access.
Know exactly when any process touches any entry. Pair with warm-keeper.

**Invariants:** OS-level evidence (not fingerprint inference).

**Why it might win:** Zero false positives for race detection.

**Why it might fail:**
- Full Disk Access + notarized helper required (complex setup)
- macOS updates require re-approval
- Overkill if upstream 2.1.101 fix works

**Verdict:** Nice-to-have adjunct to W, not standalone primary. Defer.

---

## 6. Elimination rounds

Sequential eliminations with justification:

**Round 1 — Eliminate by hard-boundary violation:**
- V1 Full proxy (MITM of prompt traffic) — R3 violated
- V13 /etc/hosts + TLS CA hack — R4, R5, R6 violated
- V3 flock wrapper — R7 violated (user explicit rejection)

**Round 2 — Eliminate by domination:**
- D1, D2, D3 (cooperative coordination) — dominated by B-family (CLI doesn't cooperate)
- G1 (401-reactive) — dominated by F+A+ (same mechanism, weaker)
- F+A+ — dominated by W (probabilistic race reduction vs elimination)
- F1 (ephemeral dirs) — dominated (refresh_token is server-side)
- H1 (MCP broker) — category error
- B2 (per-account refresher daemons) — dominated by W (same LaunchAgent mechanism, but uses curl instead of `claude`, loses γ invariant)
- A1 (per-user OS isolation) — dominated by A3 (same isolation, worse UX)

**Round 3 — Eliminate by user workflow failure:**
- Arch 1 Status Quo — R1 violated (races with N-1 CLIs)
- POC (standalone) — R10 violated (dormant drift is user-visible daily)

**Round 4 — Eliminate by operational weight:**
- A3 Containers — viable but heavy; only if W fails
- C3 ESF Observer (standalone) — needs pairing with B to solve dormant

**Surviving candidates:**
1. **W — Warm-Keeper Stratified** (primary)
2. **A3 — Per-Account Containers** (fallback if W cannot be built)
3. **C3 + W — ESF Observer + Warm-Keeper** (future enhancement)

---

## 7. Final ranking (scored)

Scoring dimensions from the research prompt. 1 = worst, 10 = best. Scores cited
or synthesized from Agents 1/2/4 where possible.

| Dimension | Arch 1 (SQ) | POC | F+A+ | **W (rec)** | A3 |
|---|---|---|---|---|---|
| Correctness under concurrency | 2 | 9 | 7 | **10** | 10 |
| Resistance to race conditions | 3 | 10 | 8 | **10** | 10 |
| Session continuity quality | 5 | 4* | 7 | **9** | 10 |
| Upstream resilience | 5 | 8 | 4 | **9** | 9 |
| Compliance / supportability | 7 | 10 | 9 | **10** | 10 |
| Secret exposure risk | 8 | 10 | 9 | **9** | 10 |
| Operational simplicity | 6 | 9 | 5 | **7** | 4 |
| User ergonomics | 6 | 4* | 6 | **9** | 5 |
| Ease of debugging | 6 | 9 | 5 | **8** | 7 |
| Crash recovery | 7 | 10 | 6 | **9** | 10 |
| Cross-platform viability | 5 | 5 | 5 | **5** | 7 |
| Implementation speed (AI-assisted) | 10 | 7 | 6 | **6** | 3 |
| Long-term maintenance | 5 | 9 | 5 | **8** | 7 |
| Need for undocumented internals | 6 | 9 | 7 | **8** | 9 |
| Blast radius on failure | 5 | 7** | 7 | **9** | 9 |
| **Weighted total** | **86** | **120** | **96** | **134** | **120** |

\* POC loses points for dormant drift / sleep-wake cascade (Agent 2 F1, F4, F7).
\** POC blast-radius moderate (dashboard becomes useless, but doesn't break CLIs).

**Top 3:**
1. **W — Warm-Keeper Stratified (134)** ← primary recommendation
2. A3 — Per-Account Containers (120)
3. POC — Read-only CCSwitch (120, but with functionality gaps)

---

## 8. Recommended architecture

### 8.1 Primary: Warm-Keeper Stratified (W)

CCSwitch becomes a **read-only observer** for the refresh lifecycle. Per-account
LaunchAgents fire every 15 minutes and invoke the official `claude --print " "` to
keep tokens warm on dormant accounts. Upstream Claude Code 2.1.101's concurrent-session
fix coordinates user CLIs and warm-keepers on the same account.

CCSwitch still:
- Probes `/v1/messages` using the Keychain access_token for rate-limit headers
- Auto-switches the `~/.ccswitch/active` pointer when the current account hits threshold
- Mirrors `oauthAccount` + `userID` into user-opted-in credential target files
- Writes the legacy `Claude Code-credentials` Keychain entry (if user enabled system-default)
- Fires tmux nudges for running claude panes

CCSwitch NO LONGER:
- Calls `refresh_access_token` anywhere
- Writes per-account hashed Keychain entries
- Tracks `waiting_for_cli` state (replaced with `token_expired` read from Keychain timestamp)
- Applies the `_REFRESH_SKEW_MS_INACTIVE` window

### 8.2 Fallback: Per-Account Containers (A3)

If W proves untenable (e.g., LaunchAgent integration too fragile, warm-keeper billing
too large), fall back to containerization. User cmux launches `docker exec -it ccacct<N> claude`.
Operationally heavier but hermetic.

### 8.3 Only-if-constraints-change: Anthropic Official Multi-Account

If Anthropic ships official multi-account support (per Issue #24798 or similar),
retire CCSwitch's custom switching layer. Keep UI as a thin wrapper over the
official switch API. This is Section 5-not-5 — plan for it, don't build to it.

---

## 9. Concrete implementation blueprint (for W)

### 9.1 Components

```
┌─────────────────────────────────────────────────┐
│           CCSwitch FastAPI (read-only)          │
│  ┌──────────────────────────────────────────┐   │
│  │ Poll loop: 20 × Keychain read + probe    │   │
│  │ No refresh calls.                         │   │
│  └──────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────┐   │
│  │ Switch controller:                        │   │
│  │   - Writes ~/.ccswitch/active             │   │
│  │   - Mirrors into credential targets       │   │
│  │   - Rewrites legacy Keychain (if enabled) │   │
│  │   - Fires tmux nudge                      │   │
│  └──────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────┐   │
│  │ LaunchAgent manager:                      │   │
│  │   - On account create: write plist +      │   │
│  │     launchctl bootstrap                   │   │
│  │   - On account delete: launchctl bootout  │   │
│  │     + rm plist                            │   │
│  │   - Force-refresh button:                 │   │
│  │     launchctl kickstart com.ccswitch.     │   │
│  │     warm.<sha8>                           │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘

~/Library/LaunchAgents/
├── com.ccswitch.warm.a1b2c3d4.plist    # account 1
├── com.ccswitch.warm.e5f6g7h8.plist    # account 2
├── ...

Each plist runs:
  /usr/local/bin/env CLAUDE_CONFIG_DIR=/path/to/accN \
    /usr/local/bin/claude --print " " --max-tokens 1
```

### 9.2 Data flow

1. **User adds account** in CCSwitch UI → OAuth code flow → Keychain seeded
2. **CCSwitch writes** `~/Library/LaunchAgents/com.ccswitch.warm.<sha8>.plist`
3. **CCSwitch runs** `launchctl bootstrap gui/<uid> <plist>` → agent active
4. **Every 15 min, launchd fires** the agent → runs `claude --print`
5. **`claude` CLI** reads Keychain, checks expiry, refreshes if needed (writes Keychain)
6. **CCSwitch's poll** reads Keychain next cycle → sees fresh token → probes
7. **User launches** interactive `claude` in cmux pane → same Keychain, same refresh
   lifecycle. Upstream 2.1.101 fix coordinates concurrent refreshers.
8. **Rate limit** hit on active account → CCSwitch's probe returns 429 → auto-switch
   updates pointer + mirror → tmux nudge → running panes continue on new account

### 9.3 State ownership

| State | Owner | Mutators | Readers |
|---|---|---|---|
| Per-account hashed Keychain entry | Claude Code CLI | CLI-interactive, warm-keeper agent (both run `claude`) | CCSwitch (read-only), CLI |
| `~/.ccswitch/active` pointer | CCSwitch | CCSwitch only | Shell integration |
| Legacy `Claude Code-credentials` Keychain | CCSwitch | CCSwitch (switch only) | Bare `claude` (without CLAUDE_CONFIG_DIR) |
| Credential target mirror (`.claude.json`) | CCSwitch | CCSwitch (switch only) | Bare `claude`, CCSwitch |
| LaunchAgent plists | CCSwitch | CCSwitch (account lifecycle) | launchd |
| `ccswitch.db` | CCSwitch | CCSwitch | CCSwitch |

**Key property:** Every credential-mutating write by CCSwitch is on a surface the CLI doesn't
race on (pointer file, mirror file, legacy Keychain which is only used by bare `claude`).
The per-account hashed Keychain — the one the running CLIs actually refresh — is never
written by CCSwitch.

### 9.4 Locking / lease strategy

- **In-process:** retain `_switch_lock` (asyncio.Lock) for serializing pointer writes
- **Cross-process:** NEW — `fcntl.flock` on `~/.ccswitch/lock` at lifespan start;
  abort with broadcast if already held (prevents two CCSwitch instances)
- **Keychain-write serialization:** retain `_credential_lock` (threading.RLock)
  for legacy Keychain + mirror writes (still safe as those are pointer-like)
- **No new locks for refresh** — refresh is handled by CLI, which has its own 2.1.101
  coordination primitive

### 9.5 Recovery behavior

- **CCSwitch crash:** restart reads Keychain fresh. No refresh state lost. Launchd
  keeps warm-keepers running independently.
- **Warm-keeper crash:** launchd restarts on next interval. No coordination needed.
- **System reboot:** launchd re-bootstraps agents on login. First firing within 15 min
  warms all tokens. CCSwitch poll picks up fresh state within ~30 s of start.
- **Keychain locked:** warm-keeper fails (exit code matches Apple's convention);
  back off, retry after user unlocks.

### 9.6 Logging / metrics

- Each warm-keeper run logs to `~/Library/Logs/ccswitch/warm-<sha8>.log` (stdout/stderr redirect in plist)
- CCSwitch broadcasts `warm_keeper_fired` events over WS for UI visibility
- Metrics: last-warm-keeper-timestamp per account; success/failure; `claude --print` exit code
- Force-refresh button in UI translates to `launchctl kickstart` call + log

### 9.7 Migration from current architecture

1. Add DB migration: new column `account.warm_keeper_active BOOLEAN DEFAULT FALSE`
2. On first boot after upgrade, for each existing account:
   - Generate and bootstrap LaunchAgent plist
   - Mark `warm_keeper_active = TRUE`
3. Clear stale refresh-related in-memory state (`_force_refresh_locks`, `_backoff_until`, `_waiting`)
4. Update `UsageCache` to drop `_waiting` set (replaced by DB-backed token-expired state)
5. Remove `force_refresh_config_dir` calls from the router; replace with `launchctl kickstart`
6. Deprecate `stale_reason` field for "access token expired" — use a computed field
   based on `expires_at < now`. Keep `stale_reason` for terminal errors (revoked, malformed).

Rollout strategy: one release. No feature flag needed — the migration is idempotent
and the new state is strictly a superset of the old state.

---

## 10. Immediate build plan (Day-0)

Day 0 (this session, after user approval):

1. **Build LaunchAgent plist template.** Create `backend/services/launch_agent_service.py`:
   - `generate_plist(account) -> str` — returns plist XML
   - `bootstrap(account) -> None` — writes plist, runs `launchctl bootstrap`
   - `bootout(account) -> None` — opposite
   - `kickstart(account) -> None` — triggers immediate firing (for Force-refresh UI)
2. **Integrate with account lifecycle:**
   - `create_account` → bootstrap
   - `delete_account` → bootout + rm plist
3. **Test LaunchAgent manually:** `launchctl list | grep ccswitch`, verify agent runs.
4. **Remove refresh pipeline:**
   - Delete `refresh_access_token` call sites in `background.py`
   - Delete `save_refreshed_token` writes
   - Delete `_REFRESH_SKEW_MS_INACTIVE` + active-ownership gate logic
   - Simplify `_process_single_account` to: read Keychain, probe, update cache
5. **Update `force_refresh_config_dir`** to call `launch_agent_service.kickstart()` instead of HTTP refresh. Return 200 immediately (kickstart is fire-and-forget).
6. **Update UI** to drop `waiting_for_cli` state. Use new computed `token_expired` derived from Keychain expires_at.

Day 1:
7. **Add `fcntl.flock` instance guard** at lifespan start.
8. **Add Keychain-locked detection** (exit code 51).
9. **Add clock-skew hardening** (validate `expires_at` bounds).
10. **Full test suite update** — replace refresh-related tests with warm-keeper kickstart tests.
11. **Manual E2E test:** Start cmux with 5 CLIs on 5 accounts. Observe no cascade. Sleep laptop. Wake. Confirm warm-keepers fire on schedule.

What to test first (correctness):
- **T1:** Warm-keeper fires every 15 min (verify via log)
- **T2:** `claude --print` refreshes token when expires_at < 20 min away
- **T3:** Two warm-keepers for same account (impossible via plist cardinality, but sanity check) coordinate via upstream 2.1.101 fix
- **T4:** CCSwitch never calls `refresh_access_token` (grep for absence)
- **T5:** User's 5-account nightly scenario: CCSwitch running + 5 cmux CLIs + sleep 8h → all accounts healthy on wake

What can be deferred safely:
- ESF observer integration (future enhancement)
- fsevents-triggered probe (future polling optimization)
- Probe jitter (compliance improvement, not correctness)
- Multi-CCSwitch-instance detection (fcntl.flock mitigates 90% of risk)

---

## 11. Edge-case checklist

- [ ] User adds account, never launches CLI, account sits 2 weeks. Warm-keeper fires weekly, token refreshes, account ready when user eventually uses it
- [ ] User deletes account in UI. Plist removed, launchd bootout clean, no zombie agent
- [ ] User deletes `CLAUDE_CONFIG_DIR` manually. Warm-keeper fails (`claude` can't start); CCSwitch shows error state; user re-logins via UI
- [ ] Mac rebooted. Launchd re-bootstraps agents on login. First fire within 15 min of login
- [ ] Keychain locked at login (corporate policy). First warm-keeper fails; retries on next interval after unlock; no cascade
- [ ] User closes laptop for 3 days. All tokens expired. On wake, launchd fires pending agents; refreshes all within 15 min
- [ ] User has 50 accounts. 50 LaunchAgents running. System load: negligible (each ping is <5s of CPU, spread across 15-min interval)
- [ ] `claude` binary moved (Homebrew upgrade). Plist hardcoded path broken. Mitigation: plist uses `/usr/bin/env -S claude` with PATH lookup
- [ ] User on Intel Mac (claude at `/usr/local/bin/claude`) vs ARM (`/opt/homebrew/bin/claude`). Detection at bootstrap time
- [ ] `CLAUDE_CONFIG_DIR` contains spaces. Plist must properly escape (use ProgramArguments array, not Program string)
- [ ] Two CCSwitch instances. `fcntl.flock` guards; second instance exits with clear error
- [ ] User manually kills warm-keeper via `launchctl bootout`. CCSwitch detects on next poll (via `launchctl list`), offers one-click re-bootstrap
- [ ] Anthropic revokes account's refresh_token. Warm-keeper `claude --print` gets 401 with auth error; CCSwitch detects via probe 401 + stderr capture; marks stale with "Refresh token revoked — re-login required"
- [ ] User runs warm-keeper on an account while their CLI is mid-refresh. Upstream 2.1.101 fix coordinates. If fix fails (e.g., user downgrades CLI), race returns — **verify via manual test that upstream fix is reliable**
- [ ] User toggles credential target while switch is in flight. Mirror write serialized via `_credential_lock` — no corruption
- [ ] User clicks Force-refresh on active account. Kickstart fires `claude --print` in that account's config dir. If user's own CLI is busy, upstream 2.1.101 coordinates. No race.
- [ ] tmux nudge fires after switch. Mirror writes complete first (ordered in `perform_switch`). Nudged panes read mirrored identity. If pane has its own `CLAUDE_CONFIG_DIR` env var it ignores mirror — no change from current behavior.
- [ ] Bare `claude` (without CLAUDE_CONFIG_DIR). Reads legacy `Claude Code-credentials` Keychain. CCSwitch still writes this on every switch (unchanged). Works exactly as today.

---

## 12. Open questions

1. **Upstream 2.1.101 coordination mechanism unknown.** Agent 3's CHANGELOG research
   confirms the fix exists for CLI 2.1.101 (user's 2.1.109 has it), but the mechanism
   (lockfile? XPC? watchdog?) isn't publicly documented. W's correctness depends on
   this fix handling concurrent warm-keeper + user-CLI refreshes on the same account.
   **Action required:** Empirical test — run warm-keeper concurrently with user CLI
   on account X; verify no 400 from `/oauth/token`.

2. **Billing impact of 1920 pings/day.** Each warm-keeper ping is billable at ~1-5 tokens.
   At 20 accounts × 96 pings = 1920 pings/day ≈ ~2000-10000 tokens/day across fleet.
   Per account that's ~100-500 tokens/day. Likely negligible for Max/Pro, but
   **action required:** confirm with user that the footprint is acceptable.

3. **What happens if `claude --print " "` triggers an interactive prompt?** E.g., if
   the CLI has pending auth approval request, onboarding, etc. The warm-keeper would
   hang or exit with non-zero. **Action required:** wrap in `timeout 30` and treat
   non-zero exit as "needs manual CLI session" — escalate to UI.

4. **LaunchAgent stdout/stderr log rotation.** If `claude --print` ever outputs
   anything substantive, log files grow. **Action required:** rotate via newsyslog
   or set up size limit.

5. **Cross-platform story.** Current CCSwitch is macOS-only. W is macOS-only (launchd).
   Linux users would need systemd-user equivalent. **Defer** — not in scope for
   current user.

6. **How does W interact with existing `docs/superpowers/specs/2026-04-14-active-ownership-refresh-fix-design.md`?**
   That design remains correct for single-CLI users. W supersedes it for N>1 users.
   **Action required:** add deprecation note to old design doc; link to this one.

7. **Should warm-keeper use `claude --print " "` or a more explicit `claude doctor`?**
   `claude doctor` may be cheaper (no API call) but may not trigger refresh.
   **Action required:** verify `claude doctor` refresh behavior empirically.

8. **Recovery plan for user's 5 currently-burned accounts.** Orthogonal to architecture.
   User must Re-login each via UI. Deploy W before re-login so new tokens aren't
   immediately re-burned. **Action required:** user performs re-logins after W ships.

---

## Convergence summary

- **Reject:** Arch 1 (status quo) — fundamentally violates R1 for user's N=20 workflow.
- **Reject:** POC (read-only standalone) — Agent 2's 20/25+ severity attacks confirmed by Agent 4.
- **Reject:** F+A+ — dominated by W.
- **Accept:** **W — Warm-Keeper Stratified** as primary. Invariant stack: α + β + γ + δ.
- **Standby:** A3 Containers as fallback if W is untenable.

**Immediate next step (pending user approval):** Start Day-0 build plan in §10. Do not
begin until user confirms this design.
