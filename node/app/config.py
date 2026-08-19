"""Configuration settings loaded from environment variables"""

from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings from .env file"""

    # HAProxy (native systemd service on host)
    haproxy_config_path: str = "/etc/haproxy/haproxy.cfg"
    haproxy_certs_dir: str = "/etc/letsencrypt/live"
    
    # Node identity
    node_name: str = "node-01"

    # Порт mTLS-nginx, на который подключается панель. Меняется в паре с
    # NODE_API_PORT в .env (его же читает compose для listen nginx) — guard
    # файрвола и валидация DNAT защищают именно этот порт
    node_api_port: int = 9100

    # Что из API доступно панели. Пусто — всё, как и было до появления настройки.
    # Строкой, а не списком: pydantic-settings разбирает «сложные» типы как JSON
    # и упал бы при импорте на обычном перечислении через запятую.
    node_capabilities: str = ""

    # Traffic
    # История трафика теперь живёт в панели; файл БД остаётся только для
    # разового экспорта легаси-данных, накопленных прежними версиями ноды
    traffic_db_path: str = "/var/lib/monitoring/traffic.db"
    port_sample_interval: int = 30  # seconds between iptables port counter samples

    # Host proc path (mounted from host)
    host_proc: str = "/host/proc"

    @property
    def haproxy_config(self) -> Path:
        return Path(self.haproxy_config_path)
    
    @property
    def haproxy_certs(self) -> Path:
        return Path(self.haproxy_certs_dir)
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance"""
    return Settings()
