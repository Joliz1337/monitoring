"""Configuration settings loaded from environment variables"""

from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings from .env file"""
    
    # API
    api_key: str = "change-me"
    api_host: str = "127.0.0.1"  # Only localhost - nginx proxies to this
    api_port: int = 7500
    
    # SSL
    ssl_cert_path: str = "/etc/nginx/ssl/cert.pem"
    ssl_key_path: str = "/etc/nginx/ssl/key.pem"
    domain: str = "monitoring.example.com"
    
    # HAProxy (native systemd service on host)
    haproxy_config_path: str = "/etc/haproxy/haproxy.cfg"
    haproxy_certs_dir: str = "/etc/letsencrypt/live"
    
    # Node identity
    node_name: str = "node-01"
    
    # Security
    security_max_failed_attempts: int = 10
    security_ban_duration: int = 3600  # 1 hour in seconds
    
    # Traffic tracking
    traffic_db_path: str = "/var/lib/monitoring/traffic.db"
    traffic_collect_interval: int = 60  # seconds between collections
    traffic_retention_days: int = 90  # how long to keep detailed data
    
    # Host proc/sys paths (mounted from host)
    host_proc: str = "/host/proc"
    host_sys: str = "/host/sys"
    
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
