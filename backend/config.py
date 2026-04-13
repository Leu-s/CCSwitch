from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    database_url: str = "sqlite+aiosqlite:///./claude_multi_account.db"
    server_port: int = 8765
    # Directory where the active Claude Code config lives
    claude_config_dir: str = "~/.claude"
    # Base directory for per-account isolated Claude config dirs
    accounts_base_dir: str = "~/.claude-multi-accounts"
    haiku_model: str = "claude-haiku-4-5-20251001"


settings = Settings()
