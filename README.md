# Claude Code Multi-Account Manager

FastAPI + SQLite web app for managing multiple Claude.ai subscription accounts.

## Quick Start

```bash
# 1. Install Python deps
pip install -r requirements.txt

# 2. Configure (optional)
cp .env.example .env

# 3. Run
uvicorn backend.main:app --port 8765 --reload

# 4. Open
open http://localhost:8765
```

The SQLite database (`claude_multi_account.db`) is created automatically on first run.

## Running Tests

```bash
pytest tests/ -v
```
