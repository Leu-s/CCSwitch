from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CLAUDE_MULTI_")

    database_url: str = "sqlite+aiosqlite:///./claude_multi_account.db"
    server_port: int = 8765
    # System-wide Claude Code config directory (~/.claude by default).
    # Prefixed so the app never inherits the user's runtime CLAUDE_CONFIG_DIR,
    # which typically points at a per-account isolated dir.
    active_claude_dir: str = "~/.claude"
    # Base directory for per-account isolated Claude config dirs
    accounts_base_dir: str = "~/.claude-multi-accounts"
    haiku_model: str = "claude-haiku-4-5-20251001"
    poll_interval_active: int = 15      # seconds between polls while clients are connected
    poll_interval_idle: int = 300       # seconds between polls while nobody is watching
    poll_interval_min: int = 120        # floor for the DB-overridden idle interval
    # Anthropic API URLs
    anthropic_messages_url: str = "https://api.anthropic.com/v1/messages"
    anthropic_refresh_url: str = "https://platform.claude.com/v1/oauth/token"
    # State directory for cross-terminal account isolation
    state_dir: str = "~/.claude-multi"
    # tmux session name created when no sessions exist
    tmux_session_name: str = "claude-multi"
    # Default threshold for new accounts (0–100 %)
    default_account_threshold_pct: float = 95.0
    # Optional API token — set CLAUDE_MULTI_API_TOKEN to require Bearer auth on
    # all HTTP requests and ?token=... on WebSocket connections.  Empty string
    # means no authentication required (safe for localhost-only use).
    api_token: str = ""


settings = Settings()
