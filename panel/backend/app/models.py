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
    folder = Column(String(200), nullable=True)
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
    
    # Xray node detection (updated periodically)
    has_xray_node = Column(Boolean, default=False, server_default="false")


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
    
    # TCP connection states
    tcp_established = Column(Integer, nullable=True)
    tcp_listen = Column(Integer, nullable=True)
    tcp_time_wait = Column(Integer, nullable=True)
    tcp_close_wait = Column(Integer, nullable=True)
    tcp_syn_sent = Column(Integer, nullable=True)
    tcp_syn_recv = Column(Integer, nullable=True)
    tcp_fin_wait = Column(Integer, nullable=True)
    
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
    
    # TCP connection states (averages)
    avg_tcp_established = Column(Float, nullable=True)
    avg_tcp_listen = Column(Float, nullable=True)
    avg_tcp_time_wait = Column(Float, nullable=True)
    avg_tcp_close_wait = Column(Float, nullable=True)
    avg_tcp_syn_sent = Column(Float, nullable=True)
    avg_tcp_syn_recv = Column(Float, nullable=True)
    avg_tcp_fin_wait = Column(Float, nullable=True)
    
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
    direction = Column(String(3), default="in")  # 'in' (incoming/INPUT) or 'out' (outgoing/OUTPUT)
    comment = Column(String(200), nullable=True)
    source = Column(String(50), default="manual")  # manual, auto_list
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    __table_args__ = (
        Index('idx_blocklist_server', 'server_id'),
        Index('idx_blocklist_source', 'source'),
        Index('idx_blocklist_direction', 'direction'),
    )


class BlocklistSource(Base):
    """Источник автоматических списков"""
    __tablename__ = "blocklist_sources"
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    url = Column(String(500), nullable=False, unique=True)
    enabled = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False)
    direction = Column(String(3), default="in")  # 'in' or 'out'
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
    
    # Retention settings (days)
    visit_stats_retention_days = Column(Integer, default=365)  # xray_visit_stats
    ip_stats_retention_days = Column(Integer, default=90)  # xray_user_ip_stats
    ip_destination_retention_days = Column(Integer, default=90)  # xray_ip_destination_stats
    hourly_stats_retention_days = Column(Integer, default=365)  # xray_hourly_stats


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


class RemnawaveExcludedDestination(Base):
    """Исключаемые destinations (сайты) из статистики.
    
    Домены и IP из этого списка полностью исключаются из сбора статистики.
    Используется для фильтрации тестовых сайтов (google.com) и служебных адресов (DNS).
    """
    __tablename__ = "remnawave_excluded_destinations"
    
    id = Column(Integer, primary_key=True)
    destination = Column(String(500), nullable=False, unique=True)  # www.google.com, 1.1.1.1 (host only, no port)
    description = Column(String(255), nullable=True)  # Описание (опционально)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class XrayStats(Base):
    """Единая таблица статистики: пользователь → IP → сайт → счётчик.
    
    Заменяет 5 таблиц (xray_visit_stats, xray_user_ip_stats,
    xray_ip_destination_stats, xray_destinations, xray_source_ips).
    Все данные в одной таблице — без JOIN, без нормализации.
    """
    __tablename__ = "xray_stats"
    
    email = Column(Integer, primary_key=True, nullable=False)  # User ID в Remnawave
    source_ip = Column(String(45), primary_key=True, nullable=False)  # IPv4/IPv6
    host = Column(String(500), primary_key=True, nullable=False)  # Хост без порта (google.com)
    count = Column(BigInteger, default=0)
    first_seen = Column(DateTime(timezone=True), server_default=func.now())
    last_seen = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    __table_args__ = (
        Index('idx_xray_stats_host', 'host'),
        Index('idx_xray_stats_last_seen', 'last_seen'),
        Index('idx_xray_stats_email_last_seen', 'email', 'last_seen'),
    )


class XrayHourlyStats(Base):
    """Почасовая статистика для timeline графиков.
    
    Составной PK (server_id, hour) вместо суррогатного id.
    """
    __tablename__ = "xray_hourly_stats"
    
    server_id = Column(Integer, primary_key=True)  # 0 = aggregated across all servers
    hour = Column(DateTime(timezone=True), primary_key=True, nullable=False)  # Начало часа (округлено)
    visit_count = Column(BigInteger, default=0)
    unique_users = Column(Integer, default=0)
    unique_destinations = Column(Integer, default=0)


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




class XrayGlobalSummary(Base):
    """Pre-computed global totals for period=all queries.
    
    Single row (id=1), rebuilt after each collection cycle.
    Eliminates full table scan on xray_visit_stats for summary queries.
    """
    __tablename__ = "xray_global_summary"
    
    id = Column(Integer, primary_key=True, default=1)
    total_visits = Column(BigInteger, default=0)
    unique_users = Column(Integer, default=0)
    unique_destinations = Column(Integer, default=0)
    last_updated = Column(DateTime(timezone=True))


class XrayDestinationSummary(Base):
    """Pre-computed per-host stats for top-destinations (period=all).
    
    Rebuilt after each collection cycle.
    Replaces GROUP BY on millions of rows with ORDER BY on small table.
    """
    __tablename__ = "xray_destination_summary"
    
    host = Column(String(500), primary_key=True)
    total_visits = Column(BigInteger, default=0)
    unique_users = Column(Integer, default=0)
    last_seen = Column(DateTime(timezone=True))


class XrayUserSummary(Base):
    """Pre-computed per-user stats for top-users (period=all).
    
    Rebuilt after each collection cycle.
    Includes IP counts to eliminate additional joins.
    """
    __tablename__ = "xray_user_summary"
    
    email = Column(Integer, primary_key=True)
    total_visits = Column(BigInteger, default=0)
    unique_sites = Column(Integer, default=0)
    unique_client_ips = Column(Integer, default=0)
    infrastructure_ips = Column(Integer, default=0)
    first_seen = Column(DateTime(timezone=True))
    last_seen = Column(DateTime(timezone=True))


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


class ASNCache(Base):
    """Кэш ASN-информации для IP-адресов (TTL 7 дней).
    
    Используется анализатором для группировки IP по ASN.
    Данные получаются из RIPE Stat API.
    """
    __tablename__ = "asn_cache"
    
    ip = Column(String(45), primary_key=True)  # IPv4/IPv6
    asn = Column(String(20), nullable=True)  # "8359" или null
    prefix = Column(String(50), nullable=True, index=True)  # "91.76.0.0/14"
    cached_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


# ==================== Server Alerts ====================

class AlertSettings(Base):
    """Настройки системы алертов мониторинга серверов (singleton, одна запись)"""
    __tablename__ = "alert_settings"
    
    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, default=False)
    
    telegram_bot_token = Column(String(200), nullable=True)
    telegram_chat_id = Column(String(100), nullable=True)
    language = Column(String(5), default="en")
    
    check_interval = Column(Integer, default=60)
    alert_cooldown = Column(Integer, default=1800)
    
    # Offline detection
    offline_enabled = Column(Boolean, default=True)
    offline_fail_threshold = Column(Integer, default=3)
    offline_recovery_notify = Column(Boolean, default=True)
    
    # CPU
    cpu_enabled = Column(Boolean, default=True)
    cpu_critical_threshold = Column(Float, default=90.0)
    cpu_spike_percent = Column(Float, default=40.0)
    cpu_sustained_seconds = Column(Integer, default=300)
    cpu_min_value = Column(Float, default=10.0)

    # RAM
    ram_enabled = Column(Boolean, default=True)
    ram_critical_threshold = Column(Float, default=90.0)
    ram_spike_percent = Column(Float, default=30.0)
    ram_sustained_seconds = Column(Integer, default=300)
    ram_min_value = Column(Float, default=10.0)

    # Network (min_bytes — порог в байтах/сек, по умолчанию 100 KB/s)
    network_enabled = Column(Boolean, default=True)
    network_spike_percent = Column(Float, default=200.0)
    network_drop_percent = Column(Float, default=80.0)
    network_sustained_seconds = Column(Integer, default=300)
    network_min_bytes = Column(Float, default=102400.0)

    # TCP Established
    tcp_established_enabled = Column(Boolean, default=True)
    tcp_established_spike_percent = Column(Float, default=200.0)
    tcp_established_drop_percent = Column(Float, default=80.0)
    tcp_established_sustained_seconds = Column(Integer, default=300)
    tcp_min_connections = Column(Integer, default=10)
    
    # TCP Listen
    tcp_listen_enabled = Column(Boolean, default=False)
    tcp_listen_spike_percent = Column(Float, default=150.0)
    tcp_listen_sustained_seconds = Column(Integer, default=300)
    
    # TCP Time Wait
    tcp_timewait_enabled = Column(Boolean, default=False)
    tcp_timewait_spike_percent = Column(Float, default=300.0)
    tcp_timewait_sustained_seconds = Column(Integer, default=300)
    
    # TCP Close Wait
    tcp_closewait_enabled = Column(Boolean, default=True)
    tcp_closewait_spike_percent = Column(Float, default=200.0)
    tcp_closewait_sustained_seconds = Column(Integer, default=300)
    
    # TCP SYN Sent
    tcp_synsent_enabled = Column(Boolean, default=False)
    tcp_synsent_spike_percent = Column(Float, default=200.0)
    tcp_synsent_sustained_seconds = Column(Integer, default=300)
    
    # TCP SYN Recv
    tcp_synrecv_enabled = Column(Boolean, default=False)
    tcp_synrecv_spike_percent = Column(Float, default=200.0)
    tcp_synrecv_sustained_seconds = Column(Integer, default=300)
    
    # TCP FIN Wait
    tcp_finwait_enabled = Column(Boolean, default=False)
    tcp_finwait_spike_percent = Column(Float, default=200.0)
    tcp_finwait_sustained_seconds = Column(Integer, default=300)
    
    # Excluded servers (JSON array of server IDs)
    excluded_server_ids = Column(Text, nullable=True)


class AlertHistory(Base):
    """Лог отправленных алертов мониторинга"""
    __tablename__ = "alert_history"
    
    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    server_name = Column(String(100), nullable=False)
    alert_type = Column(String(50), nullable=False)
    severity = Column(String(20), default="warning")
    message = Column(Text, nullable=True)
    details = Column(Text, nullable=True)
    notified = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    __table_args__ = (
        Index('idx_alert_history_server', 'server_id'),
        Index('idx_alert_history_type', 'alert_type'),
        Index('idx_alert_history_created', 'created_at'),
    )


# ==================== Billing ====================

class BillingServer(Base):
    __tablename__ = "billing_servers"
    
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    billing_type = Column(String(20), nullable=False)  # 'monthly' | 'resource'
    
    paid_until = Column(DateTime(timezone=True), nullable=True)
    
    monthly_cost = Column(Float, nullable=True)
    account_balance = Column(Float, nullable=True)
    balance_updated_at = Column(DateTime(timezone=True), nullable=True)
    
    currency = Column(String(10), default="USD")
    notes = Column(Text, nullable=True)
    folder = Column(String(200), nullable=True)
    
    last_notified_days = Column(Text, nullable=True)  # JSON: which day-thresholds already sent
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class BillingSettings(Base):
    __tablename__ = "billing_settings"
    
    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, default=False)
    notify_days = Column(Text, default="[1, 3, 7]")  # JSON array
    check_interval_minutes = Column(Integer, default=60)
