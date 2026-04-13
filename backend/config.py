from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str = "mysql+aiomysql://claude:claude@localhost:3306/claude_multi_account"
    server_port: int = 8765
    claude_config_dir: str = "~/.claude"
    haiku_model: str = "claude-haiku-4-5-20251001"

    class Config:
        env_file = ".env"

settings = Settings()
