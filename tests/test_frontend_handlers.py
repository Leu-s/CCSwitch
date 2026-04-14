"""
Pytest wrapper around the Node.js-based frontend handler tests.

The real test lives in ``tests/frontend/ws_handlers.test.mjs`` — it reads
the production ``frontend/src/ws.js`` source, executes it inside a
``node:vm`` context with stubbed DOM / WebSocket / state globals, and
asserts the audit-round-3 multi-tab fixes:

    Fix #5 — ``account_switched`` handler calls ``renderAccounts()``
    BEFORE dispatching ``app:reload-accounts``, so the Active pill +
    waiting banner flip in place without the ~300ms HTTP-round-trip
    flicker.

    Fix #3 — the new ``account_added`` case dispatches
    ``app:reload-accounts`` so sibling tabs discover freshly-enrolled
    slots that ``updateUsageLive`` alone cannot surface.

Why Node + vm instead of a real browser (Playwright / jsdom):

  * No new dependencies — ``node`` is already installed.  Playwright +
    Chromium would add ~200 MB; jsdom would add a package.json and a
    node_modules tree neither of which exist today.
  * Runs in <100 ms versus several seconds for a real browser launch.
  * Exercises the REAL ws.js source (not a mirror) by stripping its
    ``import`` / ``export`` lines and feeding every identifier via a
    stub-populated vm context.  A revert of the fix line in ws.js is
    caught by this harness just as it would be by a browser test.

If ``node`` is not on PATH (e.g. a fresh CI runner without Node), the
test is SKIPPED rather than failed so the Python-only test suite stays
green on minimal environments.  Production CI should ensure Node is
installed so the skip does not mask regressions.
"""
import shutil
import subprocess
from pathlib import Path

import pytest


NODE_TEST_SCRIPT = (
    Path(__file__).parent / "frontend" / "ws_handlers.test.mjs"
)


def test_frontend_ws_handlers():
    """Run the Node-based frontend handler tests and surface any failure."""
    node_bin = shutil.which("node")
    if node_bin is None:
        pytest.skip("node executable not found on PATH — skipping frontend handler tests")

    assert NODE_TEST_SCRIPT.is_file(), (
        f"expected frontend test script at {NODE_TEST_SCRIPT} but it is missing — "
        "the pytest wrapper and the Node script must ship together"
    )

    result = subprocess.run(
        [node_bin, str(NODE_TEST_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    # Always surface stdout so a failing assertion is visible in pytest -v.
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print("--- stderr ---")
        print(result.stderr)

    assert result.returncode == 0, (
        f"frontend ws_handlers.test.mjs failed (exit={result.returncode}).\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
