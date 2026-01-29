from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, Float, BigInteger, Index, ForeignKey, UniqueConstraint
from sqlalchemy.sql import func
from app.database import Base


class Server(Base):
    __tablename__ = "servers"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    url = Column(String(500), nullable=False)
    api_key = Column(String(200), nullable=False)
    position = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Error tracking
    last_seen = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(String(500), nullable=True)
    error_code = Column(Integer, nullable=True)
    
    # Cached full metrics JSON (updated by background collector)
    last_metrics = Column(Text, nullable=True)
    
    # Cached HAProxy data (updated every 30 seconds)
    last_haproxy_data = Column(Text, nullable=True)
    
    # Cached Traffic data (updated every 60 seconds)
    last_traffic_data = Column(Text, nullable=True)


class MetricsSnapshot(Base):
    """Хранит историю метрик для каждого сервера (сбор на панели)"""
    __tablename__ = "metrics_snapshots"
    
    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    
    # CPU
    cpu_usage = Column(Float)
    load_avg_1 = Column(Float)
    load_avg_5 = Column(Float)
    load_avg_15 = Column(Float)
    
    # Memory (bytes)
    memory_total = Column(BigInteger)
    memory_used = Column(BigInteger)
    memory_available = Column(BigInteger)
    memory_percent = Column(Float)
    swap_used = Column(BigInteger)
    swap_percent = Column(Float)
    
    # Network speed (bytes per second) - calculated by panel
    net_rx_bytes_per_sec = Column(Float, default=0)
    net_tx_bytes_per_sec = Column(Float, default=0)
    
    # Network total bytes (cumulative from node)
    net_rx_bytes = Column(BigInteger)
    net_tx_bytes = Column(BigInteger)
    
    # Disk
    disk_percent = Column(Float)
    disk_read_bytes_per_sec = Column(Float, default=0)
    disk_write_bytes_per_sec = Column(Float, default=0)
    
    # Processes
    process_count = Column(Integer)
    connections_count = Column(Integer)
    
    # Per-CPU usage (JSON array)
    per_cpu_percent = Column(Text, nullable=True)  # JSON array [12.5, 23.1, ...]
    
    __table_args__ = (
        Index('idx_metrics_server_time', 'server_id', 'timestamp'),
    )


class AggregatedMetrics(Base):
    """Агрегированные метрики (почасовые и дневные)"""
    __tablename__ = "aggregated_metrics"
    
    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    period_type = Column(String(10), nullable=False)  # 'hour' or 'day'
    
    # CPU
    avg_cpu = Column(Float)
    max_cpu = Column(Float)
    avg_load = Column(Float)
    
    # Memory
    avg_memory_percent = Column(Float)
    max_memory_percent = Column(Float)
    
    # Disk
    avg_disk_percent = Column(Float)
    
    # Network (total bytes transferred in period)
    total_rx_bytes = Column(BigInteger, default=0)
    total_tx_bytes = Column(BigInteger, default=0)
    avg_rx_speed = Column(Float, default=0)
    avg_tx_speed = Column(Float, default=0)
    
    # Disk IO
    avg_disk_read_speed = Column(Float, default=0)
    avg_disk_write_speed = Column(Float, default=0)
    
    # Count of data points aggregated
    data_points = Column(Integer, default=0)
    
    __table_args__ = (
        Index('idx_aggregated_server_period', 'server_id', 'period_type', 'timestamp'),
    )


class PanelSettings(Base):
    __tablename__ = "panel_settings"
    
    id = Column(Integer, primary_key=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(Text)


class FailedLogin(Base):
    __tablename__ = "failed_logins"
    
    id = Column(Integer, primary_key=True)
    ip_address = Column(String(45), index=True)
    attempts = Column(Integer, default=1)
    banned_until = Column(Float, nullable=True)
    last_attempt = Column(Float)


class BlocklistRule(Base):
    """Правило блокировки IP/CIDR"""
    __tablename__ = "blocklist_rules"
    
    id = Column(Integer, primary_key=True)
    ip_cidr = Column(String(50), nullable=False)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=True, index=True)
    # server_id = NULL означает глобальное правило (для всех серверов)
    is_permanent = Column(Boolean, default=True)
    comment = Column(String(200), nullable=True)
    source = Column(String(50), default="manual")  # manual, auto_list
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    __table_args__ = (
        Index('idx_blocklist_server', 'server_id'),
        Index('idx_blocklist_source', 'source'),
    )


class BlocklistSource(Base):
    """Источник автоматических списков"""
    __tablename__ = "blocklist_sources"
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    url = Column(String(500), nullable=False, unique=True)
    enabled = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False)
    last_updated = Column(DateTime(timezone=True), nullable=True)
    last_hash = Column(String(64), nullable=True)  # SHA256 для проверки изменений
    ip_count = Column(Integer, default=0)
    error_message = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# ==================== Remnawave Integration ====================

class RemnawaveSettings(Base):
    """Настройки подключения к Remnawave API"""
    __tablename__ = "remnawave_settings"
    
    id = Column(Integer, primary_key=True)
    api_url = Column(String(500), nullable=True)  # URL Remnawave Panel API
    api_token = Column(String(500), nullable=True)  # API ключ
    cookie_secret = Column(String(500), nullable=True)  # Формат: name:value для Nginx auth
    enabled = Column(Boolean, default=False)
    collection_interval = Column(Integer, default=60)  # секунды между сборами


class RemnawaveNode(Base):
    """Связь server_id с Remnawave мониторингом"""
    __tablename__ = "remnawave_nodes"
    
    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, unique=True)
    enabled = Column(Boolean, default=True)
    last_collected = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(String(500), nullable=True)


class XrayVisitStats(Base):
    """Агрегированная статистика посещений Xray"""
    __tablename__ = "xray_visit_stats"
    
    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False)
    period_start = Column(DateTime(timezone=True), nullable=False)  # Начало часа
    period_type = Column(String(10), nullable=False)  # 'hour' или 'day'
    destination = Column(String(500), nullable=False)  # Хост или IP:port
    destination_domain = Column(String(500), nullable=True)  # Резолвленный домен (если IP)
    email = Column(Integer, nullable=False)  # User ID в Remnawave
    visit_count = Column(Integer, default=0)  # Количество посещений
    
    __table_args__ = (
        UniqueConstraint('server_id', 'period_start', 'period_type', 'destination', 'email', 
                        name='uq_xray_stats_unique'),
        Index('idx_xray_stats_server_period', 'server_id', 'period_type', 'period_start'),
        Index('idx_xray_stats_email', 'email'),
        Index('idx_xray_stats_destination', 'destination'),
        Index('idx_xray_stats_period_start', 'period_start'),
    )


class RemnawaveUserCache(Base):
    """Кеш пользователей Remnawave для отображения имён"""
    __tablename__ = "remnawave_user_cache"
    
    id = Column(Integer, primary_key=True)
    email = Column(Integer, unique=True, nullable=False, index=True)  # ID пользователя (email field в логах)
    uuid = Column(String(100), nullable=True)  # UUID пользователя в Remnawave
    username = Column(String(200), nullable=True)
    telegram_id = Column(BigInteger, nullable=True)
    status = Column(String(50), nullable=True)  # ACTIVE/DISABLED/LIMITED/EXPIRED
    updated_at = Column(DateTime(timezone=True), server_default=func.now())
