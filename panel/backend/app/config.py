from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


DEFAULT_PANEL_PASSWORD = "changeme"
DEFAULT_JWT_SECRET = "jwt-secret-key-change-me"
JWT_SECRET_MIN_LENGTH = 32


class InsecureSettingsError(RuntimeError):
    def __init__(self, variables: list[str]):
        self.variables = variables
        super().__init__(f"{', '.join(variables)} must be set in the panel .env")


class Settings(BaseSettings):
    panel_uid: str = "changeme"
    panel_password: str = DEFAULT_PANEL_PASSWORD
    jwt_secret: str = DEFAULT_JWT_SECRET
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
    # Внешний порт панели: ноды по нему забирают ядра прокси для проверок
    panel_port: int = 443
    
    ext_key: str = ""
    panel_enc_key: str = ""

    def unset_secrets(self) -> list[str]:
        """Переменные окружения, оставленные пустыми или со значением из кода:
        с таким паролем в панель войдёт кто угодно, с таким секретом — подпишет себе сессию."""
        secrets = (
            ("PANEL_PASSWORD", self.panel_password, DEFAULT_PANEL_PASSWORD),
            ("JWT_SECRET", self.jwt_secret, DEFAULT_JWT_SECRET),
        )
        return [name for name, value, default in secrets if not value.strip() or value == default]

    @property
    def database_url(self) -> str:
        """PostgreSQL connection URL"""
        return f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
    
    @property
    def sync_database_url(self) -> str:
        """Sync PostgreSQL URL for background workers (export, etc.)"""
        return f"postgresql://{self.postgres_user}:{self.postgres_password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
    
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
