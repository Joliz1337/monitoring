from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    panel_uid: str = "changeme"
    panel_password: str = "changeme"
    jwt_secret: str = "jwt-secret-key-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440
    
    max_failed_attempts: int = 5
    ban_duration_seconds: int = 900
    
    database_url: str = "sqlite+aiosqlite:///./data/panel.db"
    
    # Domain for CORS (optional, defaults to same-origin only)
    domain: str = ""
    
    ext_key: str = ""
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
