#!/usr/bin/env python3
"""
One-shot cleanup for phantom-stale accounts from the April 2026
OAuth refresh client_id bug.

Context
-------
Before commit 8a76118, ``refresh_access_token`` POSTed to Anthropic's
/v1/oauth/token endpoint without a ``client_id`` field.  Anthropic
returned HTTP 400 ``invalid_request_error``, and our classifier
incorrectly treated this as TERMINAL instead of "your request was
malformed".  Every swap step-0.5 refresh and every ``revalidate_account``
call that hit this error persisted a ``stale_reason`` to the DB,
presenting healthy accounts as dead.

Commit 8a76118 fixes the outbound body AND reclassifies
``invalid_request_error`` as transient.  No new phantom-stale writes
will happen.  But accounts ALREADY marked stale will stay marked
until something runs the refresh for them:

* Poll loop's reactive-refresh path (vault-probe 401) — self-heals.
* Poll loop's proactive-escalation path (transient ladder) — self-heals.
* Swap step 0.5 (accepted incoming vault refresh) — does NOT self-heal
  because ``stale_reason`` already blocks the account from being
  picked as a swap target.
* Revalidate endpoint (manual user action) — does NOT auto-heal.

This script walks every account whose ``stale_reason`` matches the
phantom-stale set, runs ONE refresh with the FIXED POST body, and:

* HTTP 200 → persist new vault tokens, clear ``stale_reason``.
* RFC 6749 §5.2 terminal (``invalid_grant`` et al.) → LEAVE ALONE
  (genuinely dead; user must re-login).
* Anything else (transient, network, 5xx, ``invalid_request_error``
  regression) → SKIP + log.

Usage
-----
::

    uv run python scripts/cleanup_phantom_stale_2026_04_16.py --dry-run
    uv run python scripts/cleanup_phantom_stale_2026_04_16.py
    uv run python scripts/cleanup_phantom_stale_2026_04_16.py --email alice@example.com

Dry-run semantics
-----------------
``--dry-run`` lists the accounts that MATCH the phantom-stale pattern
and would be attempted WITHOUT making any network calls.  This is
deliberate: every successful refresh_access_token call rotates the
refresh_token under OAuth 2.1 RTR.  If we probed in dry-run and
dropped the rotated token on the floor (no persist), we would
destroy the token chain of every healthy account we "probed".
Dry-run therefore reports only the candidate set; apply mode does
the refresh + persist + DB clear atomically per account.

Safe to run while the server is running.  The Keychain ``security``
CLI serialises writes at the OS level, and the script refuses to
touch the active-account credentials (the CLI owns those).
Recommended flow is ``--dry-run`` first to review the candidate
list, then apply.

See ``docs/superpowers/plans/2026-04-16-oauth-refresh-client-id-fix.md``.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from backend.database import AsyncSessionLocal  # noqa: E402
from backend.models import Account  # noqa: E402
from backend.services import anthropic_api, credential_provider as cp  # noqa: E402
from backend.services.account_service import get_active_email  # noqa: E402


logger = logging.getLogger("cleanup_phantom_stale")


# Every stale_reason string the refresh-terminal branch can persist
# contains at least one of these tokens.  See the call sites enumerated
# in the fix plan (PATH 1 step 0.5, PATH 2 revalidate, PATH 3 reactive
# refresh, PATH 4 proactive escalation).
_PHANTOM_STALE_SUBSTRINGS = ("rejected", "revoked", "re-login required")


def _is_phantom_stale(reason: str | None) -> bool:
    if not reason:
        return False
    lower = reason.lower()
    return any(s in lower for s in _PHANTOM_STALE_SUBSTRINGS)


async def _attempt_refresh(email: str) -> tuple[str, Any]:
    """Run ONE refresh for ``email``'s vault entry.

    Returns ``(status, payload)`` where status is one of:

    * ``"healed"``  — 200 OK; payload is the refreshed token dict.
    * ``"dead"``    — RFC 6749 terminal; payload is a human-readable reason.
    * ``"skipped"`` — transient / missing data; payload is a detail string.
    """
    credentials = cp.read_vault(email)
    if not credentials:
        return "skipped", "no vault entry"
    refresh_token = cp.refresh_token_of(credentials)
    if not refresh_token:
        return "skipped", "no refresh_token in vault"

    try:
        new_tokens = await anthropic_api.refresh_access_token(refresh_token)
    except httpx.HTTPStatusError as http_err:
        kind = anthropic_api.parse_oauth_error(http_err)
        status_code = http_err.response.status_code
        if kind in (
            anthropic_api.OAuthErrorKind.TERMINAL_REVOKED,
            anthropic_api.OAuthErrorKind.TERMINAL_REJECTED,
        ):
            return "dead", f"HTTP {status_code} terminal (RFC 6749 §5.2)"
        return "skipped", f"HTTP {status_code} transient"
    except httpx.RequestError as net_err:
        return "skipped", f"network error: {net_err}"
    except RuntimeError as rt_err:
        return "skipped", f"response parse error: {rt_err}"

    return "healed", new_tokens


def _persist_healed(email: str, new_tokens: dict) -> None:
    """Write the refreshed tokens back to the vault.

    Mirrors the expires_in → expires_at conversion in
    ``background._refresh_vault_token`` (milliseconds since epoch).
    """
    access_token = new_tokens.get("access_token")
    if not isinstance(access_token, str):
        raise RuntimeError(f"refresh response missing access_token: {new_tokens!r}")
    refresh_token = new_tokens.get("refresh_token")
    expires_in = new_tokens.get("expires_in")
    expires_at_ms: int | None = None
    if expires_in:
        try:
            expires_at_ms = int(time.time() * 1000) + int(expires_in) * 1000
        except (TypeError, ValueError):
            expires_at_ms = None
    cp.save_refreshed_vault_token(
        email,
        access_token,
        expires_at=expires_at_ms,
        refresh_token=refresh_token if isinstance(refresh_token, str) else None,
    )


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-shot cleanup for phantom-stale accounts (April 2026 OAuth fix).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Probe without mutating DB or vault.  Default: apply changes.",
    )
    parser.add_argument(
        "--email",
        default=None,
        help="Restrict cleanup to a single account email.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log DEBUG-level httpx + refresh details.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    mode = "DRY-RUN" if args.dry_run else "APPLY"
    print(f"=== Phantom-stale cleanup — {mode} ===\n")

    active_email = get_active_email()
    if active_email:
        print(f"Active account (will be refused): {active_email}\n")

    healed = 0
    dead = 0
    skipped = 0
    refused_active = 0

    async with AsyncSessionLocal() as db:
        query = select(Account).where(Account.stale_reason.is_not(None))
        if args.email:
            query = query.where(Account.email == args.email)
        result = await db.execute(query)
        all_stale = list(result.scalars().all())
        candidates = [a for a in all_stale if _is_phantom_stale(a.stale_reason)]

        if not candidates:
            print("No accounts match the phantom-stale pattern.  Nothing to do.")
            return 0

        print(f"Candidates ({len(candidates)}):")
        for account in candidates:
            print(f"  • {account.email}: {account.stale_reason!r}")
        print()

        if args.dry_run:
            # List-only mode.  Every refresh call rotates the refresh_token
            # under OAuth 2.1 RTR, so probing without persisting would
            # destroy the chain of healthy accounts.  Re-run without
            # --dry-run to actually refresh + persist + clear.
            for account in candidates:
                if account.email == active_email:
                    print(f"[{account.email}] WOULD REFUSE — active account")
                    refused_active += 1
                else:
                    credentials = cp.read_vault(account.email)
                    has_rt = bool(cp.refresh_token_of(credentials)) if credentials else False
                    if has_rt:
                        print(f"[{account.email}] WOULD ATTEMPT refresh (vault has refresh_token)")
                    else:
                        print(f"[{account.email}] WOULD SKIP — no refresh_token in vault")
                        skipped += 1

            print(f"\nSummary (DRY-RUN — no network calls made):")
            print(f"  phantom-stale candidates: {len(candidates)}")
            print(f"  would refuse (active):    {refused_active}")
            print(f"  would skip (no rt):       {skipped}")
            print(f"  would attempt refresh:    {len(candidates) - refused_active - skipped}")
            print("\nRe-run without --dry-run to apply.")
            return 0

        # Apply mode: refresh + persist + clear per account.
        for account in candidates:
            email = account.email
            if email == active_email:
                print(f"[{email}] REFUSED — active account (CLI owns refresh)")
                refused_active += 1
                continue

            status, payload = await _attempt_refresh(email)
            if status == "healed":
                try:
                    _persist_healed(email, payload)
                except Exception as persist_err:
                    print(f"[{email}] SKIPPED — persist failed: {persist_err}")
                    skipped += 1
                    continue
                account.stale_reason = None
                await db.commit()
                print(f"[{email}] HEALED — tokens persisted, stale_reason cleared")
                healed += 1
            elif status == "dead":
                print(f"[{email}] GENUINELY DEAD ({payload}) — leaving alone; user must re-login")
                dead += 1
            else:
                print(f"[{email}] SKIPPED — {payload}")
                skipped += 1

    print(f"\nSummary (APPLY):")
    print(f"  healed:          {healed}")
    print(f"  genuinely dead:  {dead}")
    print(f"  skipped:         {skipped}")
    print(f"  refused active:  {refused_active}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
