# Claude Code Multi-Account Manager

FastAPI + MySQL web app for managing multiple Claude.ai subscription accounts.

## Quick Start

```bash
# 1. Start MySQL
docker-compose up -d

# 2. Install Python deps
pip install -r requirements.txt

# 3. Configure
cp .env.example .env

# 4. Run
uvicorn backend.main:app --port 8765 --reload

# 5. Open
open http://localhost:8765
```

## Running Tests

```bash
pytest tests/ -v
```
