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
    
    # PostgreSQL settings (primary database)
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "panel"
    postgres_password: str = "panel_secret"
    postgres_db: str = "panel"
    
    # Domain for CORS (optional, defaults to same-origin only)
    domain: str = ""
    
    ext_key: str = ""
    
    @property
    def database_url(self) -> str:
        """PostgreSQL connection URL"""
        return f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
    
    @property
    def sync_database_url(self) -> str:
        """Sync PostgreSQL URL for background workers (export, etc.)"""
        return f"postgresql://{self.postgres_user}:{self.postgres_password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
