from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, Float, BigInteger, Index, ForeignKey
from sqlalchemy.sql import func
from app.database import Base


class PKIKeygen(Base):
    """Singleton: CA, клиентский сертификат панели и общий серверный cert для всех нод."""
    __tablename__ = "keygen"

    id = Column(Integer, primary_key=True)
    ca_cert_pem = Column(Text, nullable=False)
    ca_key_pem = Column(Text, nullable=False)
    client_cert_pem = Column(Text, nullable=False)
    client_key_pem = Column(Text, nullable=False)
    shared_node_cert_pem = Column(Text, nullable=True)
    shared_node_key_pem = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Server(Base):
    __tablename__ = "servers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    url = Column(String(500), nullable=False)
    api_key = Column(String(200), nullable=True)
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
    
    # Xray node detection (updated periodically)
    has_xray_node = Column(Boolean, default=False, server_default="false")
    
    # Speed test results (JSON, updated by speedtest scheduler)
    last_speedtest = Column(Text, nullable=True)

    # Geo data (resolved from server IP, cached)
    country = Column(String(10), nullable=True)
    geo_region = Column(String(20), nullable=True)

    # Wildcard SSL deployment config
    wildcard_ssl_enabled = Column(Boolean, default=False, server_default="false")
    wildcard_ssl_deploy_path = Column(String(500), nullable=True)
    wildcard_ssl_reload_cmd = Column(String(500), nullable=True)
    wildcard_ssl_fullchain_name = Column(String(255), nullable=True)
    wildcard_ssl_privkey_name = Column(String(255), nullable=True)
    wildcard_ssl_custom_path_enabled = Column(Boolean, default=False, server_default="false")
    wildcard_ssl_custom_fullchain_path = Column(String(500), nullable=True)
    wildcard_ssl_custom_privkey_path = Column(String(500), nullable=True)

    # HAProxy config profile binding
    active_haproxy_profile_id = Column(Integer, ForeignKey("haproxy_config_profiles.id", ondelete="SET NULL"), nullable=True)
    haproxy_config_hash = Column(String(64), nullable=True)
    haproxy_last_sync_at = Column(DateTime(timezone=True), nullable=True)

    # PKI (mTLS) — флаги типа авторизации с нодой
    # pki_enabled: нода работает по mTLS (false = legacy с api_key)
    # uses_shared_cert: нода уже мигрирована на общий shared cert
    pki_enabled = Column(Boolean, default=False, server_default="false", nullable=False)
    uses_shared_cert = Column(Boolean, default=False, server_default="false", nullable=False)
    haproxy_sync_status = Column(String(20), nullable=True)


class ServerCache(Base):
    """Отдельная таблица для тяжёлых JSON-кешей, часто обновляемых фоновыми задачами.
    
    Вынесена из Server чтобы:
    - UPDATE большого JSON не блокировал и не раздувал основную таблицу
    - Разные фоновые задачи могли писать в разные строки без deadlock
    - VACUUM работал быстрее (меньше dead tuples в основной таблице)
    """
    __tablename__ = "server_cache"
    
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True)
    last_haproxy_data = Column(Text, nullable=True)
    last_traffic_data = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


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
    __tablename__ = "remnawave_settings"

    id = Column(Integer, primary_key=True)
    api_url = Column(String(500), nullable=True)
    api_token = Column(String(500), nullable=True)
    cookie_secret = Column(String(500), nullable=True)
    enabled = Column(Boolean, default=False)
    collection_interval = Column(Integer, default=300)
    ignored_user_ids = Column(Text, nullable=True)  # JSON array of user IDs

    # Anomaly detection
    anomaly_enabled = Column(Boolean, default=False)
    anomaly_use_custom_bot = Column(Boolean, default=False)
    anomaly_tg_bot_token = Column(String(200), nullable=True)
    anomaly_tg_chat_id = Column(String(100), nullable=True)
    anomaly_ignore_ip = Column(Text, nullable=True)      # JSON array of user IDs to ignore IP checks
    anomaly_ignore_hwid = Column(Text, nullable=True)     # JSON array of user IDs to ignore HWID checks
    anomaly_cooldown = Column(Integer, default=300)       # секунд между уведомлениями об одном пользователе

    # Traffic anomaly detection
    traffic_anomaly_enabled = Column(Boolean, default=False)
    traffic_threshold_gb = Column(Float, default=30.0)
    traffic_confirm_count = Column(Integer, default=2)


class RemnawaveHwidDevice(Base):
    __tablename__ = "remnawave_hwid_devices"

    hwid = Column(String(200), primary_key=True)
    user_uuid = Column(String(100), nullable=False, index=True)
    platform = Column(String(100), nullable=True)
    os_version = Column(String(100), nullable=True)
    device_model = Column(String(200), nullable=True)
    user_agent = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)
    synced_at = Column(DateTime(timezone=True), server_default=func.now())


class XrayStats(Base):
    """Пользователь -> IP: отслеживание одновременных подключений."""
    __tablename__ = "xray_stats"

    email = Column(Integer, primary_key=True, nullable=False)
    source_ip = Column(String(45), primary_key=True, nullable=False)
    last_seen = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index('idx_xray_stats_email', 'email'),
    )


class RemnawaveUserCache(Base):
    __tablename__ = "remnawave_user_cache"
    
    id = Column(Integer, primary_key=True)
    email = Column(Integer, unique=True, nullable=False, index=True)
    uuid = Column(String(100), nullable=True)
    short_uuid = Column(String(50), nullable=True)
    username = Column(String(200), nullable=True)
    telegram_id = Column(BigInteger, nullable=True)
    status = Column(String(50), nullable=True)
    expire_at = Column(DateTime(timezone=True), nullable=True)
    subscription_url = Column(Text, nullable=True)
    sub_revoked_at = Column(DateTime(timezone=True), nullable=True)
    traffic_limit_bytes = Column(BigInteger, nullable=True)
    traffic_limit_strategy = Column(String(20), nullable=True)
    last_traffic_reset_at = Column(DateTime(timezone=True), nullable=True)
    used_traffic_bytes = Column(BigInteger, nullable=True)
    lifetime_used_traffic_bytes = Column(BigInteger, nullable=True)
    online_at = Column(DateTime(timezone=True), nullable=True)
    first_connected_at = Column(DateTime(timezone=True), nullable=True)
    last_connected_node_uuid = Column(String(100), nullable=True)
    hwid_device_limit = Column(Integer, nullable=True)
    user_email = Column(String(200), nullable=True)
    description = Column(Text, nullable=True)
    tag = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


# ==================== Torrent Blocker ====================

class TorrentBlockerSettings(Base):
    __tablename__ = "torrent_blocker_settings"

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, default=False)
    poll_interval_minutes = Column(Integer, default=5)
    ban_duration_minutes = Column(Integer, default=30)
    excluded_server_ids = Column(Text, nullable=True)

    last_poll_at = Column(DateTime(timezone=True), nullable=True)
    last_poll_status = Column(String(20), nullable=True)
    last_poll_message = Column(Text, nullable=True)
    last_ips_banned = Column(Integer, default=0)
    last_reports_processed = Column(Integer, default=0)
    total_ips_banned = Column(Integer, default=0)
    total_cycles = Column(Integer, default=0)


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

    # Network (min_bytes — порог шума в байтах/сек, по умолчанию 1 MB/s;
    # ниже этого значения spike/drop считаются естественной сменой нагрузки)
    network_enabled = Column(Boolean, default=True)
    network_spike_percent = Column(Float, default=200.0)
    network_drop_percent = Column(Float, default=80.0)
    network_sustained_seconds = Column(Integer, default=300)
    network_min_bytes = Column(Float, default=1048576.0)

    # TCP Established
    tcp_established_enabled = Column(Boolean, default=True)
    tcp_established_spike_percent = Column(Float, default=200.0)
    tcp_established_drop_percent = Column(Float, default=80.0)
    tcp_established_sustained_seconds = Column(Integer, default=300)
    tcp_min_connections = Column(Integer, default=100)
    
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

    # Load Average
    load_avg_enabled = Column(Boolean, default=True)
    load_avg_threshold_offset = Column(Float, default=1.0)
    load_avg_sustained_checks = Column(Integer, default=3)

    # Excluded servers (JSON array of server IDs)
    excluded_server_ids = Column(Text, nullable=True)

    # Per-trigger excluded servers (JSON arrays of server IDs)
    offline_excluded_server_ids = Column(Text, nullable=True)
    cpu_excluded_server_ids = Column(Text, nullable=True)
    ram_excluded_server_ids = Column(Text, nullable=True)
    network_excluded_server_ids = Column(Text, nullable=True)
    tcp_excluded_server_ids = Column(Text, nullable=True)
    load_avg_excluded_server_ids = Column(Text, nullable=True)


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
    billing_type = Column(String(20), nullable=False)  # 'monthly' | 'resource' | 'yandex_cloud'
    
    paid_until = Column(DateTime(timezone=True), nullable=True)
    
    monthly_cost = Column(Float, nullable=True)
    account_balance = Column(Float, nullable=True)
    balance_updated_at = Column(DateTime(timezone=True), nullable=True)
    
    currency = Column(String(10), default="USD")
    notes = Column(Text, nullable=True)
    folder = Column(String(200), nullable=True)
    
    last_notified_days = Column(Text, nullable=True)  # JSON: which day-thresholds already sent

    # Yandex Cloud
    yc_oauth_token = Column(String(200), nullable=True)
    yc_billing_account_id = Column(String(100), nullable=True)
    yc_balance_threshold = Column(Float, nullable=True, default=0)
    yc_daily_cost = Column(Float, nullable=True)
    yc_last_sync_at = Column(DateTime(timezone=True), nullable=True)
    yc_last_error = Column(String(500), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class BillingSettings(Base):
    __tablename__ = "billing_settings"
    
    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, default=False)
    notify_days = Column(Text, default="[1, 3, 7]")  # JSON array
    check_interval_minutes = Column(Integer, default=60)


# ==================== Xray Monitor ====================

class XrayMonitorSettings(Base):
    """Настройки мониторинга Xray подключений (singleton)"""
    __tablename__ = "xray_monitor_settings"
    
    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, default=False)
    check_interval = Column(Integer, default=60)
    latency_threshold_ms = Column(Integer, default=500)
    fail_threshold = Column(Integer, default=2)
    
    use_custom_bot = Column(Boolean, default=False)
    telegram_bot_token = Column(String(200), nullable=True)
    telegram_chat_id = Column(String(100), nullable=True)
    
    notify_down = Column(Boolean, default=True)
    notify_recovery = Column(Boolean, default=True)
    notify_latency = Column(Boolean, default=True)

    speedtest_enabled = Column(Boolean, default=False)
    speedtest_interval = Column(Integer, default=30)
    speed_threshold_mbps = Column(Integer, default=100)
    notify_slow_speed = Column(Boolean, default=True)

    ignore_list = Column(Text, default="[]")


class XrayMonitorSubscription(Base):
    """URL подписки с Xray-ключами"""
    __tablename__ = "xray_monitor_subscriptions"
    
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    url = Column(String(1000), nullable=False)
    enabled = Column(Boolean, default=True)
    auto_refresh = Column(Boolean, default=True)
    last_refreshed = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(String(500), nullable=True)
    server_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class XrayMonitorServer(Base):
    """Отдельный Xray-сервер для мониторинга"""
    __tablename__ = "xray_monitor_servers"
    
    id = Column(Integer, primary_key=True)
    subscription_id = Column(Integer, ForeignKey("xray_monitor_subscriptions.id", ondelete="CASCADE"), nullable=True, index=True)
    position = Column(Integer, default=0)
    name = Column(Text, nullable=False)
    protocol = Column(String(20), nullable=False)  # vless, vmess, trojan, shadowsocks
    address = Column(String(500), nullable=False)
    port = Column(Integer, nullable=False)
    raw_key = Column(Text, nullable=False)
    config_json = Column(Text, nullable=True)  # parsed outbound settings as JSON
    enabled = Column(Boolean, default=True)
    socks_port = Column(Integer, nullable=True)
    
    status = Column(String(20), default="unknown")  # online, offline, unknown
    last_ping_ms = Column(Float, nullable=True)
    last_download_mbps = Column(Float, nullable=True)
    last_upload_mbps = Column(Float, nullable=True)
    last_check = Column(DateTime(timezone=True), nullable=True)
    fail_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    __table_args__ = (
        Index('idx_xray_monitor_server_sub', 'subscription_id'),
        Index('idx_xray_monitor_server_status', 'status'),
    )


class XrayMonitorCheck(Base):
    """Результат одной проверки"""
    __tablename__ = "xray_monitor_checks"

    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("xray_monitor_servers.id", ondelete="CASCADE"), nullable=False, index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    status = Column(String(10), nullable=False)  # ok, fail
    ping_ms = Column(Float, nullable=True)
    download_mbps = Column(Float, nullable=True)
    upload_mbps = Column(Float, nullable=True)
    error = Column(String(500), nullable=True)

    __table_args__ = (
        Index('idx_xray_check_server_time', 'server_id', 'timestamp'),
    )


# ==================== Infrastructure Tree ====================

class InfraAccount(Base):
    """Аккаунт верхнего уровня (облачный email / провайдер)"""
    __tablename__ = "infra_accounts"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    position = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class InfraProject(Base):
    """Проект внутри аккаунта (msc1, msc2...)"""
    __tablename__ = "infra_projects"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("infra_accounts.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(200), nullable=False)
    position = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index('idx_infra_project_account', 'account_id'),
    )


class InfraProjectServer(Base):
    """Привязка сервера к проекту (junction table)"""
    __tablename__ = "infra_project_servers"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("infra_projects.id", ondelete="CASCADE"), nullable=False)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False)
    position = Column(Integer, default=0)

    __table_args__ = (
        Index('idx_infra_ps_project', 'project_id'),
        Index('idx_infra_ps_server', 'server_id'),
    )


# ==================== Shared Notes ====================

class SharedNote(Base):
    """Общий блокнот с реалтайм-синхронизацией (singleton, одна запись id=1)"""
    __tablename__ = "shared_notes"

    id = Column(Integer, primary_key=True)
    content = Column(Text, default="")
    version = Column(Integer, default=1)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SharedTask(Base):
    """Общие задачи с реалтайм-синхронизацией"""
    __tablename__ = "shared_tasks"

    id = Column(Integer, primary_key=True, index=True)
    text = Column(String(500), nullable=False)
    is_done = Column(Boolean, default=False)
    position = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# ==================== HAProxy Config Profiles ====================

class HAProxyConfigProfile(Base):
    __tablename__ = "haproxy_config_profiles"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    config_content = Column(Text, nullable=False)
    position = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class HAProxySyncLog(Base):
    __tablename__ = "haproxy_sync_log"

    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    profile_id = Column(Integer, ForeignKey("haproxy_config_profiles.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(20), nullable=False)
    message = Column(Text, nullable=True)
    config_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index('idx_sync_log_server', 'server_id'),
        Index('idx_sync_log_created', 'created_at'),
    )


# ==================== ASN Cache ====================

class ASNCache(Base):
    __tablename__ = "asn_cache"

    ip = Column(String(45), primary_key=True)
    asn = Column(String(20), nullable=True)
    prefix = Column(String(50), nullable=True)
    holder = Column(String(200), nullable=True)
    cached_at = Column(DateTime(timezone=True), server_default=func.now())


# ==================== Wildcard SSL ====================

class WildcardCertificate(Base):
    __tablename__ = "wildcard_certificates"

    id = Column(Integer, primary_key=True)
    domain = Column(String(253), nullable=False)
    base_domain = Column(String(253), nullable=False)
    fullchain_pem = Column(Text, nullable=False)
    privkey_pem = Column(Text, nullable=False)
    expiry_date = Column(DateTime(timezone=True), nullable=True)
    issued_at = Column(DateTime(timezone=True), server_default=func.now())
    last_renewed = Column(DateTime(timezone=True), nullable=True)
    auto_renew = Column(Boolean, default=True, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
