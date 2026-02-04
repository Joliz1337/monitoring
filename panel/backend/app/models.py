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
    api_url = Column(String(500), nullable=True)
    api_token = Column(String(500), nullable=True)
    cookie_secret = Column(String(500), nullable=True)
    enabled = Column(Boolean, default=False)
    collection_interval = Column(Integer, default=300)  # 5 minutes recommended
    # Список ID пользователей для игнорирования (JSON массив)
    # Игнорируемые пользователи исключаются из: сбора логов, уведомлений анализатора, всех проверок
    ignored_user_ids = Column(Text, nullable=True)  # JSON array of user IDs (integers)


class RemnawaveNode(Base):
    """Связь server_id с Remnawave мониторингом"""
    __tablename__ = "remnawave_nodes"
    
    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, unique=True)
    enabled = Column(Boolean, default=True)
    last_collected = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(String(500), nullable=True)


class RemnawaveInfrastructureAddress(Base):
    """Список IP/доменов инфраструктуры (серверы, HAProxy и т.д.)
    
    Адреса из этого списка помечаются как инфраструктурные при сборе статистики.
    Домены автоматически резолвятся в IP.
    """
    __tablename__ = "remnawave_infrastructure_addresses"
    
    id = Column(Integer, primary_key=True)
    address = Column(String(255), nullable=False, unique=True)  # IP или домен
    resolved_ips = Column(Text, nullable=True)  # JSON список резолвленных IP
    last_resolved = Column(DateTime(timezone=True), nullable=True)
    description = Column(String(255), nullable=True)  # Описание (опционально)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class XrayVisitStats(Base):
    """Счётчик посещений: (server, destination, email) -> total_count
    
    Оптимизированная схема — одна запись на комбинацию, счётчик инкрементируется.
    Без временных периодов — хранит общую статистику за всё время.
    """
    __tablename__ = "xray_visit_stats"
    
    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False)
    destination = Column(String(500), nullable=False)  # Хост:port (google.com:443)
    email = Column(Integer, nullable=False)  # User ID в Remnawave
    visit_count = Column(BigInteger, default=0)  # Общий счётчик посещений
    first_seen = Column(DateTime(timezone=True), server_default=func.now())
    last_seen = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    __table_args__ = (
        UniqueConstraint('server_id', 'destination', 'email', name='uq_xray_stats_unique'),
        Index('idx_xray_stats_server', 'server_id'),
        Index('idx_xray_stats_email', 'email'),
        Index('idx_xray_stats_destination', 'destination'),
        Index('idx_xray_stats_visits', 'visit_count'),
    )


class XrayHourlyStats(Base):
    """Почасовая статистика для timeline графиков.
    
    Лёгкая таблица без детализации по сайтам/пользователям.
    Хранит только общее число посещений за час.
    """
    __tablename__ = "xray_hourly_stats"
    
    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False)
    hour = Column(DateTime(timezone=True), nullable=False)  # Начало часа (округлено)
    visit_count = Column(BigInteger, default=0)
    unique_users = Column(Integer, default=0)
    unique_destinations = Column(Integer, default=0)
    
    __table_args__ = (
        UniqueConstraint('server_id', 'hour', name='uq_xray_hourly_unique'),
        Index('idx_xray_hourly_server_hour', 'server_id', 'hour'),
    )


class RemnawaveUserCache(Base):
    """Кеш пользователей Remnawave для отображения имён и дополнительной информации"""
    __tablename__ = "remnawave_user_cache"
    
    id = Column(Integer, primary_key=True)
    email = Column(Integer, unique=True, nullable=False, index=True)
    uuid = Column(String(100), nullable=True)
    short_uuid = Column(String(50), nullable=True)
    username = Column(String(200), nullable=True)
    telegram_id = Column(BigInteger, nullable=True)
    status = Column(String(50), nullable=True)
    
    # Subscription info
    expire_at = Column(DateTime(timezone=True), nullable=True)
    subscription_url = Column(String(500), nullable=True)
    sub_revoked_at = Column(DateTime(timezone=True), nullable=True)
    sub_last_user_agent = Column(String(500), nullable=True)
    sub_last_opened_at = Column(DateTime(timezone=True), nullable=True)
    
    # Traffic limits
    traffic_limit_bytes = Column(BigInteger, nullable=True)
    traffic_limit_strategy = Column(String(20), nullable=True)  # NO_RESET, DAY, WEEK, MONTH
    last_traffic_reset_at = Column(DateTime(timezone=True), nullable=True)
    
    # Traffic usage (cached from userTraffic)
    used_traffic_bytes = Column(BigInteger, nullable=True)
    lifetime_used_traffic_bytes = Column(BigInteger, nullable=True)
    online_at = Column(DateTime(timezone=True), nullable=True)
    first_connected_at = Column(DateTime(timezone=True), nullable=True)
    last_connected_node_uuid = Column(String(100), nullable=True)
    
    # Device limit
    hwid_device_limit = Column(Integer, nullable=True)
    
    # Additional info
    user_email = Column(String(200), nullable=True)  # email field from Remnawave (can be actual email)
    description = Column(Text, nullable=True)
    tag = Column(String(100), nullable=True)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


class XrayUserIpStats(Base):
    """Статистика IP адресов пользователей.
    
    Хранит уникальные комбинации (server, email, source_ip) с счётчиком подключений.
    Используется для отслеживания с каких IP подключается пользователь.
    
    is_infrastructure=True означает, что IP принадлежит инфраструктуре (HAProxy, VPN серверы и т.д.)
    """
    __tablename__ = "xray_user_ip_stats"
    
    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False)
    email = Column(Integer, nullable=False)  # User ID в Remnawave
    source_ip = Column(String(45), nullable=False)  # IPv4 или IPv6 адрес
    connection_count = Column(BigInteger, default=0)  # Количество подключений
    is_infrastructure = Column(Boolean, default=False)  # True если IP принадлежит инфраструктуре
    first_seen = Column(DateTime(timezone=True), server_default=func.now())
    last_seen = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    __table_args__ = (
        UniqueConstraint('server_id', 'email', 'source_ip', name='uq_user_ip_stats_unique'),
        Index('idx_user_ip_server', 'server_id'),
        Index('idx_user_ip_email', 'email'),
        Index('idx_user_ip_source', 'source_ip'),
        Index('idx_user_ip_infra', 'is_infrastructure'),
    )


class XrayIpDestinationStats(Base):
    """Статистика destinations по IP клиента.
    
    Хранит связь (server, email, source_ip, destination) -> count.
    Позволяет узнать, к каким сайтам обращался пользователь с конкретного IP.
    """
    __tablename__ = "xray_ip_destination_stats"
    
    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False)
    email = Column(Integer, nullable=False)  # User ID в Remnawave
    source_ip = Column(String(45), nullable=False)  # IPv4 или IPv6 адрес клиента
    destination = Column(String(500), nullable=False)  # Хост:port (google.com:443)
    connection_count = Column(BigInteger, default=0)  # Количество подключений
    first_seen = Column(DateTime(timezone=True), server_default=func.now())
    last_seen = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    __table_args__ = (
        UniqueConstraint('server_id', 'email', 'source_ip', 'destination', name='uq_ip_dest_stats_unique'),
        Index('idx_ip_dest_server', 'server_id'),
        Index('idx_ip_dest_email_ip', 'email', 'source_ip'),
        Index('idx_ip_dest_destination', 'destination'),
    )


class RemnawaveExport(Base):
    """Хранит информацию о задачах экспорта данных Remnawave"""
    __tablename__ = "remnawave_exports"
    
    id = Column(Integer, primary_key=True)
    filename = Column(String(255), nullable=False)
    format = Column(String(10), nullable=False)  # csv, json, xlsx
    status = Column(String(20), default="pending")  # pending, processing, completed, failed
    
    # Export settings (stored as JSON)
    settings = Column(Text, nullable=True)  # JSON with all export options
    
    # Result info
    file_size = Column(BigInteger, nullable=True)
    rows_count = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)


# ==================== Traffic Anomaly Analyzer ====================

class TrafficAnalyzerSettings(Base):
    """Настройки анализатора аномального трафика"""
    __tablename__ = "traffic_analyzer_settings"
    
    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, default=False)
    check_interval_minutes = Column(Integer, default=30)
    
    # Критерии аномалий
    traffic_limit_gb = Column(Float, default=100.0)  # Лимит трафика в ГБ
    ip_limit_multiplier = Column(Float, default=2.0)  # Множитель от hwidDeviceLimit
    check_hwid_anomalies = Column(Boolean, default=True)
    
    # Telegram настройки
    telegram_bot_token = Column(String(200), nullable=True)
    telegram_chat_id = Column(String(100), nullable=True)
    
    # Статус
    last_check_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(String(500), nullable=True)


class TrafficAnomalyLog(Base):
    """Лог обнаруженных аномалий трафика"""
    __tablename__ = "traffic_anomaly_logs"
    
    id = Column(Integer, primary_key=True)
    user_email = Column(Integer, nullable=False, index=True)  # User ID в Remnawave
    username = Column(String(200), nullable=True)
    anomaly_type = Column(String(50), nullable=False)  # traffic, ip_count, hwid
    severity = Column(String(20), default="warning")  # warning, critical
    details = Column(Text, nullable=True)  # JSON с деталями аномалии
    notified = Column(Boolean, default=False)  # Было ли отправлено уведомление
    resolved = Column(Boolean, default=False)  # Помечено как решённое
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    __table_args__ = (
        Index('idx_anomaly_user', 'user_email'),
        Index('idx_anomaly_type', 'anomaly_type'),
        Index('idx_anomaly_created', 'created_at'),
        Index('idx_anomaly_resolved', 'resolved'),
    )


class UserTrafficSnapshot(Base):
    """Снимки трафика пользователей для расчёта потребления за период.
    
    Хранит трафик на момент проверки анализатором.
    При следующей проверке вычисляется разница для определения аномалий.
    """
    __tablename__ = "user_traffic_snapshots"
    
    id = Column(Integer, primary_key=True)
    user_email = Column(Integer, nullable=False, unique=True, index=True)  # User ID в Remnawave
    traffic_bytes = Column(BigInteger, default=0)  # Трафик на момент снимка
    snapshot_at = Column(DateTime(timezone=True), server_default=func.now())  # Время снимка
