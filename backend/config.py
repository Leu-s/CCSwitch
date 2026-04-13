from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    database_url: str = "mysql+aiomysql://claude:claude@localhost:3306/claude_multi_account"
    server_port: int = 8765
    claude_config_dir: str = "~/.claude"
    haiku_model: str = "claude-haiku-4-5-20251001"

settings = Settings()
