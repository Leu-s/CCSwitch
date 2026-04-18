from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CCSWITCH_")

    database_url: str = "sqlite+aiosqlite:///./ccswitch.db"
    server_host: str = "127.0.0.1"
    server_port: int = 41924
    haiku_model: str = "claude-haiku-4-5-20251001"
    poll_interval_active: int = 15      # seconds between polls while clients are connected
    poll_interval_idle: int = 300       # seconds between polls while nobody is watching
    poll_interval_min: int = 120        # floor for the DB-overridden idle interval
    # Anthropic API URLs
    anthropic_messages_url: str = "https://api.anthropic.com/v1/messages"
    anthropic_refresh_url: str = "https://platform.claude.com/v1/oauth/token"
    # tmux session name created when no sessions exist
    tmux_session_name: str = "ccswitch"
    # Default threshold for new accounts (0–100 %)
    default_account_threshold_pct: float = 95.0
    # Optional API token — set CCSWITCH_API_TOKEN to require Bearer auth on
    # all HTTP requests and ?token=... on WebSocket connections.  Empty string
    # means no authentication required (safe for localhost-only use).
    api_token: str = ""
    # WebSocket replay buffer — how many recent events to keep for reconnecting clients.
    ws_replay_buffer_size: int = 100
    # Login session timeout (seconds) — how long an abandoned login
    # terminal (user walked away, closed the tab, browser crashed) stays
    # alive before the cleanup loop reaps it.  5 minutes is enough for a
    # patient typist to complete the OAuth redirect dance while keeping
    # orphan tmux windows from piling up in the user's workspace.
    login_session_timeout: int = 300
    # Cadence of the background cleanup loop that reaps expired login
    # sessions.  Kept tight (60 s) so stale sessions never live more
    # than ~timeout + cadence seconds in the wild.
    login_session_cleanup_cadence: int = 60
    # Rate-limit backoff — initial delay and cap (seconds) after Anthropic 429.
    rate_limit_backoff_initial: int = 120
    rate_limit_backoff_max: int = 3600
    # Vault account polling via GET /api/oauth/usage (read-only, no window trigger)
    poll_interval_vault: int = 600
    poll_interval_vault_min: int = 180
    anthropic_usage_url: str = "https://api.anthropic.com/api/oauth/usage"


settings = Settings()
