# Claude Code Multi-Account Manager

FastAPI + SQLite web app for managing multiple Claude.ai subscription accounts.
Each account lives in its own isolated `CLAUDE_CONFIG_DIR`; the service polls
rate-limit headers from `/v1/messages` and can auto-switch to the next account
when the current one approaches its 5-hour window limit.

## Requirements

- Python 3.12+
- macOS (the credential switching path reads/writes the system Keychain)
- tmux (used for the login flow and monitor features)
- [uv](https://github.com/astral-sh/uv) (recommended) or `pip`

## Quick Start

```bash
# 1. Install dependencies
uv sync             # or: pip install -e .

# 2. (optional) override defaults
cp .env.example .env     # all vars use the CLAUDE_MULTI_ prefix

# 3. Run the server
uv run uvicorn backend.main:app --port 8765 --reload

# 4. Open the dashboard
open http://localhost:8765
```

The SQLite database (`claude_multi_account.db`) is created automatically on
first run.  Per-account isolated config dirs are written under
`~/.claude-multi-accounts/`, and the currently active account is recorded in
`~/.claude-multi/active`.

## Running Tests

```bash
uv run pytest tests/ -q     # or: pytest tests/ -q
```

Test databases are written to a pytest tmp dir (see `tests/conftest.py`) so
they never pollute the repo root.

## Shell Integration

The dashboard's Settings tab can append a one-liner to `~/.zshrc` / `~/.bashrc`
that exports `CLAUDE_CONFIG_DIR` to whatever `~/.claude-multi/active` points
at.  This lets new terminals pick up the current account automatically.

## Architecture Overview

See `CLAUDE.md` for a short architecture tour aimed at future AI-assisted
sessions.
