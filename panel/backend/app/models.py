from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, Float, BigInteger, Index, ForeignKey, UniqueConstraint
from sqlalchemy.sql import func
from app.database import Base
from app.crypto import EncryptedString


class PKIKeygen(Base):
    """Singleton: CA, клиентский сертификат панели и общий серверный cert для всех нод."""
    __tablename__ = "keygen"

    id = Column(Integer, primary_key=True)
    ca_cert_pem = Column(Text, nullable=False)
    ca_key_pem = Column(EncryptedString, nullable=False)
    client_cert_pem = Column(Text, nullable=False)
    client_key_pem = Column(EncryptedString, nullable=False)
    shared_node_cert_pem = Column(Text, nullable=True)
    shared_node_key_pem = Column(EncryptedString, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class NodeInstallKey(Base):
    """Ключ установки под один конкретный сервер — для нод, к которым нет доступа.

    Общий NODE_SECRET несёт приватный ключ сертификата, одинакового для всего парка:
    отдать его владельцу арендованного сервера — значит отдать доступ ко всем нодам.
    Здесь под каждую выдачу выпускается собственный сертификат без clientAuth, и
    сгореть он может только вместе с этим одним сервером.
    """
    __tablename__ = "node_install_keys"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    common_name = Column(String(120), nullable=False, unique=True)
    cert_pem = Column(Text, nullable=False)
    key_pem = Column(EncryptedString, nullable=False)
    fingerprint = Column(String(100), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RemnawaveCertProfile(Base):
    """Сохранённый сертификат (SECRET_KEY) ноды Remnawave — переиспользуется при автоустановке."""
    __tablename__ = "remnawave_cert_profiles"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    secret_key = Column(EncryptedString, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class BackupSettings(Base):
    """Синглтон: настройки автоматических бэкапов панели в Telegram."""
    __tablename__ = "backup_settings"

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, default=False, server_default="false", nullable=False)
    schedule_kind = Column(String(20), default="daily", server_default="daily")  # daily | every_hours
    at_time = Column(String(5), default="04:00", server_default="04:00")  # HH:MM UTC для daily
    every_hours = Column(Integer, default=24, server_default="24")
    bot_token = Column(EncryptedString, nullable=True)
    chat_id = Column(String(100), nullable=True)
    archive_password = Column(EncryptedString, nullable=True)
    volume_size_mb = Column(Integer, default=45, server_default="45")
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    last_status = Column(String(20), nullable=True)  # ok | error
    last_error = Column(String(500), nullable=True)


class Server(Base):
    __tablename__ = "servers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    url = Column(String(500), nullable=False)
    api_key = Column(EncryptedString, nullable=True)
    # SOCKS5-прокси панель→нода: "ip:port" или "ip:port@login:pass"
    proxy_url = Column(String(255), nullable=True)
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

    # Доставка образа ноды с панели (для нод под ТСПУ, без доступа к GHCR).
    # image_delivery: auto — нода тянет GHCR, при провале доставка по SSH; ssh — сразу SSH.
    # SSH-креды для доставки хранятся зашифрованными (EncryptedString), заполняются опционально.
    image_delivery = Column(String(10), nullable=False, server_default="auto")
    ssh_host = Column(String(255), nullable=True)
    ssh_port = Column(Integer, nullable=True)
    ssh_user = Column(String(100), nullable=True)
    ssh_password = Column(EncryptedString, nullable=True)
    ssh_private_key = Column(EncryptedString, nullable=True)
    ssh_passphrase = Column(EncryptedString, nullable=True)

    # Кэш фактического порта sshd с ноды (из /api/ssh/status и применений SSH-конфига).
    # Не путать с ssh_port выше — это порт из кредов доставки образа.
    sshd_port = Column(Integer, nullable=True)

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

    # Firewall (UFW) profile binding
    active_firewall_profile_id = Column(Integer, ForeignKey("firewall_profiles.id", ondelete="SET NULL"), nullable=True)
    firewall_rules_hash = Column(String(64), nullable=True)
    firewall_last_sync_at = Column(DateTime(timezone=True), nullable=True)
    firewall_sync_status = Column(String(20), nullable=True)

    # DNAT (port forwarding) profile binding
    active_dnat_profile_id = Column(Integer, ForeignKey("dnat_profiles.id", ondelete="SET NULL"), nullable=True)
    dnat_rules_hash = Column(String(64), nullable=True)
    dnat_last_sync_at = Column(DateTime(timezone=True), nullable=True)
    dnat_sync_status = Column(String(20), nullable=True)  # synced | pending | failed | denied
    # Порядок привязки к DNAT-профилю: по нему сервер получает свой IP назначения
    # из списка через запятую (первый сервер — первый IP, по кругу)
    dnat_link_position = Column(Integer, nullable=True)

    # PKI (mTLS) — флаги типа авторизации с нодой
    # pki_enabled: нода работает по mTLS (false = legacy с api_key)
    # uses_shared_cert: нода уже мигрирована на общий shared cert
    # dedicated_cert: осознанно на персональном сертификате (одноразовый ключ
    # автоустановки) — миграция на общий cert такую ноду не трогает
    pki_enabled = Column(Boolean, default=False, server_default="false", nullable=False)
    uses_shared_cert = Column(Boolean, default=False, server_default="false", nullable=False)
    dedicated_cert = Column(Boolean, default=False, server_default="false", nullable=False)
    haproxy_sync_status = Column(String(20), nullable=True)

    # Anti-DDoS emergency mode state (mirrored from node ddos-watchdog)
    antiddos_emergency_mode = Column(Boolean, default=False, server_default="false", nullable=False)
    antiddos_source = Column(String(10), nullable=True)   # auto | manual | none
    antiddos_since = Column(DateTime(timezone=True), nullable=True)
    antiddos_reason = Column(String(200), nullable=True)
    antiddos_watchdog = Column(Boolean, default=True, server_default="true", nullable=False)
    antiddos_last_sync_at = Column(DateTime(timezone=True), nullable=True)

    # Remnawave nginx profile binding
    # config_hash — hash ОТРЕНДЕРЕННОГО контента (после подстановки {{DOMAIN}})
    active_remnawave_nginx_profile_id = Column(Integer, ForeignKey("remnawave_nginx_profiles.id", ondelete="SET NULL"), nullable=True)
    remnawave_nginx_domain = Column(String(255), nullable=True)
    remnawave_nginx_config_hash = Column(String(64), nullable=True)
    # Хэш файла, реально лежащего на ноде: нода подставляет в конфиг свои
    # лимиты, поэтому он отличается от отправленного и служит эталоном
    # для обнаружения ручных правок на ноде
    remnawave_nginx_node_hash = Column(String(64), nullable=True)
    remnawave_nginx_last_sync_at = Column(DateTime(timezone=True), nullable=True)
    remnawave_nginx_sync_status = Column(String(20), nullable=True)  # synced | pending | failed
    remnawave_nginx_detected = Column(Boolean, default=False, server_default="false", nullable=False)

    # Учёт трафика: версия агента решает, умеет ли нода отдавать счётчики портов,
    # tracked_ports — JSON-список портов, за которыми нода их ведёт
    node_version = Column(String(20), nullable=True)
    tracked_ports = Column(Text, nullable=True)

    # Карта прав, которую нода прислала о себе (NODE_CAPABILITIES в её .env).
    # NULL — ограничений нет; по ней панель решает, идти ли к ноде вообще.
    node_capabilities = Column(Text, nullable=True)

    # Доп. порты этой ноды, исключаемые из эфемерной выдачи ядра (строка вида
    # "5201,8443-8450"); при рассылке объединяются с общим списком из
    # panel_settings (ключ reserved_ports_global)
    reserved_ports = Column(Text, nullable=True)


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
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class NodePendingSync(Base):
    """Долг перед нодой: что применить на ней, когда она вернётся в сеть.

    Пока сервер офлайн, отправлять ему блок-лист, whitelist или правила firewall
    некуда — запрос упрётся в таймаут, а изменение пропадёт: панель нигде не помнит,
    что нода его не получила. Строка здесь и есть эта память, и живёт она в базе,
    поэтому перезапуск панели очередь не теряет.

    Хранится вид работы, а не готовая команда: исполнитель собирает актуальное
    желаемое состояние в момент отправки. Иначе за неделю простоя накопились бы
    устаревшие payload'ы, которые пришлось бы применять по порядку.
    """
    __tablename__ = "node_pending_sync"

    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True)
    kind = Column(String(40), primary_key=True)
    requested_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    next_attempt_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    attempts = Column(Integer, default=0, server_default="0", nullable=False)
    last_error = Column(String(500), nullable=True)


class PanelHostMetric(Base):
    """История нагрузки хоста самой панели: среднее и пик за интервал снапшота.

    Сетевых счётчиков нет намеренно — бэкенд живёт в bridge-сети Docker и
    видит только трафик своего veth, а не сетевых карт хоста.
    """
    __tablename__ = "panel_host_metrics"

    id = Column(BigInteger, primary_key=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    cpu_usage = Column(Float)
    cpu_usage_max = Column(Float)
    memory_percent = Column(Float)
    memory_percent_max = Column(Float)
    memory_used = Column(BigInteger)
    memory_available = Column(BigInteger)
    load_avg_1 = Column(Float)
    load_avg_1_max = Column(Float)


class MetricsSnapshot(Base):
    """Хранит историю метрик для каждого сервера (сбор на панели)"""
    __tablename__ = "metrics_snapshots"

    # BigInteger: при 500 нодах int4-serial переполнился бы за ~1.5 года
    id = Column(BigInteger, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    
    # CPU — среднее за окно опроса (у старой ноды — мгновенная проба).
    # cpu_usage_max — максимум секундного среднего по ядрам за окно, не самое
    # горячее ядро. NULL в *_max = «пика нет»: старая нода или строка до миграции.
    cpu_usage = Column(Float)
    cpu_usage_max = Column(Float, nullable=True)
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

    # Network speed (bytes per second): average over the poll window and its peak
    net_rx_bytes_per_sec = Column(Float, default=0)
    net_tx_bytes_per_sec = Column(Float, default=0)
    net_rx_bytes_per_sec_max = Column(Float, nullable=True)
    net_tx_bytes_per_sec_max = Column(Float, nullable=True)

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
        # Последний снапшот сервера ищется как max(id) с GROUP BY server_id —
        # индекс по timestamp для этого не подходит, нужен порядок по id.
        Index('idx_metrics_server_latest', 'server_id', 'id'),
        # Одиночного индекса на server_id нет намеренно: он ведущая колонка обоих
        # составных, а вставка идёт каждые 10 секунд по всем нодам — самый горячий
        # путь записи в проекте, лишний индекс на нём дороже всего.
    )


class AggregatedMetrics(Base):
    """Агрегированные метрики (почасовые и дневные)"""
    __tablename__ = "aggregated_metrics"
    
    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    period_type = Column(String(10), nullable=False)  # 'hour' or 'day'
    
    # CPU; NULL в max_* — агрегат до появления пиков
    avg_cpu = Column(Float)
    max_cpu = Column(Float)
    avg_load = Column(Float)
    max_load = Column(Float, nullable=True)

    # Memory
    avg_memory_percent = Column(Float)
    max_memory_percent = Column(Float)

    # Disk
    avg_disk_percent = Column(Float)

    # Network (total bytes transferred in period, average and peak speed)
    total_rx_bytes = Column(BigInteger, default=0)
    total_tx_bytes = Column(BigInteger, default=0)
    avg_rx_speed = Column(Float, default=0)
    avg_tx_speed = Column(Float, default=0)
    max_rx_speed = Column(Float, nullable=True)
    max_tx_speed = Column(Float, nullable=True)
    
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

    # UNIQUE обязателен: на нём держится ON CONFLICT DO NOTHING в агрегации —
    # без него повторный прогон за период (после рестарта панели) плодил дубли
    __table_args__ = (
        UniqueConstraint('server_id', 'period_type', 'timestamp', name='uq_aggregated_metrics'),
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
    list_type = Column(String(10), default="block")  # 'block' (DROP) or 'allow' (whitelist/ACCEPT)
    comment = Column(String(200), nullable=True)
    source = Column(String(50), default="manual")  # manual, auto_list
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index('idx_blocklist_server', 'server_id'),
        Index('idx_blocklist_source', 'source'),
        # direction (in/out) и list_type (block/allow) — кардинальность 2; индекс
        # бесполезен для планировщика и только удорожает массовую загрузку auto-list.
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
    anomaly_ip_enabled = Column(Boolean, default=True)       # IP > лимит устройств
    anomaly_hwid_enabled = Column(Boolean, default=True)     # HWID > лимит (авто-очистка)
    anomaly_ua_enabled = Column(Boolean, default=True)       # неизвестный User-Agent
    anomaly_devdata_enabled = Column(Boolean, default=True)  # невалидные данные устройства
    anomaly_ip_margin = Column(Integer, default=2)           # аномалия когда IP > лимит + запас
    anomaly_ip_confirm_count = Column(Integer, default=5)    # подтверждений подряд до уведомления
    anomaly_asn_margin = Column(Integer, default=0)          # уведомление только если ASN > лимит + запас
    anomaly_ip_smart_enabled = Column(Boolean, default=True)     # умное определение: сверять с расходом трафика
    anomaly_ip_smart_traffic_gb = Column(Float, default=20.0)    # меньше этого за сутки — не уведомлять
    anomaly_devdata_smart_enabled = Column(Boolean, default=True)   # умное определение devdata: сверять с расходом трафика
    anomaly_devdata_smart_traffic_gb = Column(Float, default=20.0)  # меньше этого за сутки — не уведомлять о невалидных данных
    anomaly_ua_patterns = Column(Text, nullable=True)        # реестр известных UA; NULL = встроенный
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
    user_id = Column(Integer, nullable=False, index=True)
    platform = Column(String(100), nullable=True)
    os_version = Column(String(100), nullable=True)
    device_model = Column(String(200), nullable=True)
    user_agent = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)
    synced_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # synced_at несёт двойную нагрузку: по нему удаляются устройства, не попавшие
        # в последнюю синхронизацию, и по нему же сортируется список устройств в API
        Index('idx_hwid_devices_synced_at', 'synced_at'),
    )


class XrayStats(Base):
    """Пользователь -> IP: отслеживание одновременных подключений."""
    __tablename__ = "xray_stats"

    email = Column(Integer, primary_key=True, nullable=False)
    source_ip = Column(String(45), primary_key=True, nullable=False)
    last_seen = Column(DateTime(timezone=True), server_default=func.now())

    # email — ведущая колонка составного PK (email, source_ip), поэтому отдельный
    # индекс на email избыточен: PK-индекс уже покрывает фильтр WHERE email = ...


class RemnawaveIpAnomalyState(Base):
    """Персистентное состояние IP-аномалии по пользователю: счётчик срабатываний,
    известные IP (анти-спам) и message_id предыдущего уведомления для reply-threading."""
    __tablename__ = "remnawave_ip_anomaly_state"

    email = Column(Integer, primary_key=True)            # user id Remnawave (как XrayStats.email)
    trigger_count = Column(Integer, default=0, nullable=False)   # сквозной счётчик «N-е срабатывание»
    known_ips = Column(Text, nullable=True)              # JSON-массив строк — уже показанные IP
    last_message_id = Column(BigInteger, nullable=True)  # message_id предыдущего уведомления (reply)
    last_chat_id = Column(String(100), nullable=True)    # чат предыдущего уведомления (валидация reply)
    last_notified_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


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

    __table_args__ = (
        # Кеш чистится удалением записей старше недели по updated_at
        Index('idx_rw_user_cache_updated_at', 'updated_at'),
    )


# ==================== Torrent Blocker ====================

class TorrentBlockerSettings(Base):
    __tablename__ = "torrent_blocker_settings"

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, default=False)
    poll_interval_minutes = Column(Integer, default=5)
    ban_duration_minutes = Column(Integer, default=30)
    excluded_server_ids = Column(Text, nullable=True)

    # Вебхук-предупреждение: панель уведомляет внешний сервис (бота) о грядущем бане,
    # ждёт webhook_delay_seconds (грейс-период для пользователя), затем банит IP.
    webhook_enabled = Column(Boolean, default=False)
    webhook_url = Column(Text, nullable=True)
    webhook_secret = Column(Text, nullable=True)
    webhook_delay_seconds = Column(Integer, default=60)

    last_poll_at = Column(DateTime(timezone=True), nullable=True)
    last_poll_status = Column(String(20), nullable=True)
    last_poll_message = Column(Text, nullable=True)
    last_ips_banned = Column(Integer, default=0)
    last_reports_processed = Column(Integer, default=0)
    total_ips_banned = Column(Integer, default=0)
    total_cycles = Column(Integer, default=0)


class TorrentBlockerBan(Base):
    """Журнал банов IP — для подсчёта активных банов и графика динамики."""
    __tablename__ = "torrent_blocker_bans"

    id = Column(Integer, primary_key=True)
    ip = Column(String(45), nullable=False)
    banned_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)


Index("ix_torrent_blocker_bans_banned_at", TorrentBlockerBan.banned_at)
Index("ix_torrent_blocker_bans_expires_at", TorrentBlockerBan.expires_at)


# ==================== Anti-DDoS ====================

class AntiDdosSettings(Base):
    """Настройки анти-DDoS защиты (singleton, одна запись).

    Whitelist из двух частей: авто (IP всех нод + IP панели, вычисляется на лету)
    и ручная (user_cidrs — подсети CDN и пр.). Панель ежечасно объединяет их и
    рассылает нодам одним набором."""
    __tablename__ = "antiddos_settings"

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, default=True)
    whitelist_push_interval = Column(Integer, default=3600)   # seconds
    status_poll_interval = Column(Integer, default=60)        # seconds
    watchdog_default_enabled = Column(Boolean, default=True)
    user_cidrs = Column(Text, nullable=True)                  # JSON array of IP/CIDR

    last_push_at = Column(DateTime(timezone=True), nullable=True)
    last_push_status = Column(String(20), nullable=True)
    last_push_count = Column(Integer, default=0)


class AntiDdosWhitelistSource(Base):
    """Авто-источник IP/CIDR для анти-DDoS whitelist (Cloudflare, Yandex Cloud и т.п.).

    Панель периодически тянет URL, извлекает IPv4/CIDR (JSON, текст — не важно) и
    добавляет их в набор antiddos_allow на нодах. Этот набор ACCEPT'ится только
    внутри цепочки ANTIDDOS, т.е. работает лишь когда включён аварийный режим."""
    __tablename__ = "antiddos_whitelist_sources"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    url = Column(String(500), nullable=False, unique=True)
    enabled = Column(Boolean, default=True)
    last_updated = Column(DateTime(timezone=True), nullable=True)
    ip_count = Column(Integer, default=0)
    error_message = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


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

    # Conntrack (заполнение таблицы соединений ядра, % от nf_conntrack_max)
    conntrack_enabled = Column(Boolean, default=True)
    conntrack_threshold = Column(Float, default=80.0)

    # Excluded servers (JSON array of server IDs)
    excluded_server_ids = Column(Text, nullable=True)

    # Per-trigger excluded servers (JSON arrays of server IDs)
    offline_excluded_server_ids = Column(Text, nullable=True)
    cpu_excluded_server_ids = Column(Text, nullable=True)
    ram_excluded_server_ids = Column(Text, nullable=True)
    network_excluded_server_ids = Column(Text, nullable=True)
    tcp_excluded_server_ids = Column(Text, nullable=True)
    load_avg_excluded_server_ids = Column(Text, nullable=True)
    conntrack_excluded_server_ids = Column(Text, nullable=True)


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
        # История листается страницами: фильтр по серверу либо по типу плюс
        # сортировка по дате — одиночные индексы дают сортировку на выборке
        Index('idx_alert_history_server_created', 'server_id', 'created_at'),
        Index('idx_alert_history_type_created', 'alert_type', 'created_at'),
    )


# ==================== Billing ====================

class BillingServer(Base):
    __tablename__ = "billing_servers"
    
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    billing_type = Column(String(20), nullable=False)  # 'monthly' | 'resource' | 'cloud'
    
    paid_until = Column(DateTime(timezone=True), nullable=True)
    
    monthly_cost = Column(Float, nullable=True)
    account_balance = Column(Float, nullable=True)
    balance_updated_at = Column(DateTime(timezone=True), nullable=True)
    
    currency = Column(String(10), default="USD")
    notes = Column(Text, nullable=True)
    folder = Column(String(200), nullable=True)
    
    last_notified_days = Column(Text, nullable=True)  # JSON: which day-thresholds already sent

    # Облачный провайдер (billing_type='cloud'): Yandex Cloud, Selectel
    cloud_provider = Column(String(30), nullable=True)
    cloud_credential = Column(EncryptedString, nullable=True)
    cloud_account_id = Column(String(100), nullable=True)
    cloud_balance_threshold = Column(Float, nullable=True, default=0)
    cloud_daily_cost = Column(Float, nullable=True)
    cloud_last_sync_at = Column(DateTime(timezone=True), nullable=True)
    cloud_last_error = Column(String(500), nullable=True)
    # Снимки баланса [[iso_ts, balance], ...] для провайдеров без API истории
    # списаний (Timeweb): расход считается по снижению баланса между синками
    cloud_balance_history = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class BillingSettings(Base):
    __tablename__ = "billing_settings"
    
    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, default=False)
    notify_days = Column(Text, default="[1, 3, 7]")  # JSON array
    check_interval_minutes = Column(Integer, default=60)


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
        # Журнал читается только в разрезе профиля
        Index('idx_sync_log_profile', 'profile_id'),
    )


# ==================== Remnawave nginx Profiles ====================

class RemnawaveNginxProfile(Base):
    __tablename__ = "remnawave_nginx_profiles"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    # Шаблон полного nginx.conf с плейсхолдером {{DOMAIN}}
    config_content = Column(Text, nullable=False)
    # Опции схемы реального IP (CDN, PROXY protocol и т.п.) — JSON,
    # из конфига не парсятся, генератор рендерит конфиг из них
    options = Column(Text, nullable=True)
    position = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class RemnawaveNginxSyncLog(Base):
    __tablename__ = "remnawave_nginx_sync_log"

    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    profile_id = Column(Integer, ForeignKey("remnawave_nginx_profiles.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(20), nullable=False)  # success | failed | skipped
    message = Column(Text, nullable=True)
    config_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index('idx_rw_nginx_sync_log_server', 'server_id'),
        Index('idx_rw_nginx_sync_log_created', 'created_at'),
        Index('idx_rw_nginx_sync_log_profile', 'profile_id'),
    )


# ==================== Firewall Profiles (UFW) ====================

class FirewallProfile(Base):
    __tablename__ = "firewall_profiles"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    rules_json = Column(Text, nullable=False, default="[]")
    default_incoming = Column(String(20), nullable=False, default="deny")
    default_outgoing = Column(String(20), nullable=False, default="allow")
    position = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class FirewallSyncLog(Base):
    __tablename__ = "firewall_sync_log"

    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    profile_id = Column(Integer, ForeignKey("firewall_profiles.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(20), nullable=False)  # success | failed | rolled_back
    message = Column(Text, nullable=True)
    rules_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index('idx_fw_sync_log_server', 'server_id'),
        Index('idx_fw_sync_log_created', 'created_at'),
        Index('idx_fw_sync_log_profile', 'profile_id'),
    )


# ==================== DNAT Profiles (port forwarding via iptables nat) ====================

class DnatProfile(Base):
    __tablename__ = "dnat_profiles"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    rules_json = Column(Text, nullable=False, default="[]")
    position = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DnatSyncLog(Base):
    __tablename__ = "dnat_sync_log"

    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    profile_id = Column(Integer, ForeignKey("dnat_profiles.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(20), nullable=False)  # success | failed | skipped
    message = Column(Text, nullable=True)
    rules_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index('idx_dnat_sync_log_created', 'created_at'),
        Index('idx_dnat_sync_log_profile', 'profile_id'),
    )


# ==================== ASN Cache ====================

class ASNCache(Base):
    __tablename__ = "asn_cache"

    ip = Column(String(45), primary_key=True)
    asn = Column(String(20), nullable=True)
    prefix = Column(String(50), nullable=True)
    holder = Column(String(200), nullable=True)
    cached_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # Протухшие записи вычищаются на каждом lookup_ips — без индекса это
        # seq scan всего кеша перед каждым разрешением ASN
        Index('idx_asn_cache_cached_at', 'cached_at'),
    )


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


# ==================== Traffic Accounting ====================

class ServerTraffic(Base):
    """История трафика сервера по часам и суткам (панель считает её сама).

    scope_key — сентинел "" вместо NULL и отдельная колонка-дискриминатор scope:
    в UNIQUE-индексе PostgreSQL значения NULL не конфликтуют между собой, поэтому
    UPSERT по ключу с NULL никогда не срабатывал бы и таблица росла бы дублями.
    """
    __tablename__ = "server_traffic"

    id = Column(BigInteger, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False)
    period_type = Column(String(5), nullable=False)   # hour | day
    scope = Column(String(6), nullable=False)         # total | iface | port
    scope_key = Column(String(32), nullable=False, default="", server_default="")  # "" | eth0 | 443
    bucket = Column(DateTime, nullable=False)         # naive UTC, усечён до часа или суток
    rx_bytes = Column(BigInteger, nullable=False, default=0, server_default="0")
    tx_bytes = Column(BigInteger, nullable=False, default=0, server_default="0")
    covered_seconds = Column(Integer, nullable=False, default=0, server_default="0")
    source = Column(String(6), nullable=False, default="live", server_default="live")  # live | legacy

    __table_args__ = (
        UniqueConstraint(
            'server_id', 'period_type', 'scope', 'bucket', 'scope_key',
            name='uq_server_traffic',
        ),
        Index('idx_server_traffic_cleanup', 'period_type', 'bucket'),
    )


class ServerTrafficCounter(Base):
    """Последнее кумулятивное значение счётчика — база для вычисления дельты.

    boot_id и boot_at нужны для детекта перезагрузки ноды: после неё счётчики
    интерфейсов начинаются с нуля, и разница со старым значением была бы мусором.
    """
    __tablename__ = "server_traffic_counters"

    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True)
    scope = Column(String(6), primary_key=True)
    scope_key = Column(String(32), primary_key=True)
    rx_value = Column(BigInteger, nullable=False)
    tx_value = Column(BigInteger, nullable=False)
    observed_at = Column(DateTime, nullable=False)
    boot_id = Column(String(40), nullable=True)
    boot_at = Column(DateTime, nullable=True)


class TrafficImportState(Base):
    """Состояние переноса легаси-истории трафика с ноды в базу панели."""
    __tablename__ = "traffic_import_state"

    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True)
    # pending | node_too_old | imported | purged | empty | failed
    status = Column(String(12), nullable=False, default="pending", server_default="pending")
    node_version = Column(String(20), nullable=True)
    fingerprint = Column(String(64), nullable=True)
    rows_imported = Column(Integer, default=0, server_default="0")
    imported_at = Column(DateTime, nullable=True)
    purged_at = Column(DateTime, nullable=True)
    attempts = Column(Integer, default=0, server_default="0")
    next_attempt_at = Column(DateTime, nullable=True)
    last_error = Column(String(500), nullable=True)


class XrayTestSubscription(Base):
    """Сохранённый источник конфигураций для проверки: подписка или список ссылок."""
    __tablename__ = "xray_test_subscriptions"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)
    kind = Column(String(10), nullable=False, default="url", server_default="url")  # url | links
    # И URL подписки, и сами ссылки содержат ключи доступа — храним зашифрованными
    payload = Column(EncryptedString, nullable=False)
    # Идентификатор профиля клиента (device_profiles): от него зависят
    # User-Agent и заголовки HWID запроса подписки
    user_agent = Column(String(200), nullable=True)
    last_fetched_at = Column(DateTime(timezone=True), nullable=True)
    last_count = Column(Integer, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class XrayTestSniSet(Base):
    """Именованный список SNI для проверки одной конфигурации по многим доменам."""
    __tablename__ = "xray_test_sni_sets"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)
    sni_list = Column(Text, nullable=False)  # JSON-массив доменов
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class XrayTestRun(Base):
    """Сводка прогона. Хранится ограниченное число последних — см. history.py."""
    __tablename__ = "xray_test_runs"

    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    finished_at = Column(DateTime(timezone=True), nullable=True)
    source = Column(String(20), nullable=False)      # links | json | subscription
    source_name = Column(String(200), nullable=True)
    location = Column(String(40), nullable=False)    # panel | node:<id>
    location_name = Column(String(200), nullable=True)
    status = Column(String(12), nullable=False)      # success | error | cancelled
    total = Column(Integer, default=0, server_default="0")
    ok_count = Column(Integer, default=0, server_default="0")
    degraded_count = Column(Integer, default=0, server_default="0")
    fail_count = Column(Integer, default=0, server_default="0")


class XrayTestResult(Base):
    """Строка результата прогона.

    Секретов не хранит: ссылка живёт только в памяти задачи, сюда попадают адрес,
    протокол и метрики — этого хватает, чтобы сравнить прогоны между собой.
    """
    __tablename__ = "xray_test_results"

    id = Column(BigInteger, primary_key=True)
    run_id = Column(Integer, ForeignKey("xray_test_runs.id", ondelete="CASCADE"), nullable=False)
    remark = Column(String(200), nullable=True)
    protocol = Column(String(20), nullable=True)
    address = Column(String(255), nullable=True)
    port = Column(Integer, nullable=True)
    sni = Column(String(255), nullable=True)
    transport = Column(String(20), nullable=True)
    security = Column(String(20), nullable=True)
    core = Column(String(20), nullable=True)
    location = Column(String(40), nullable=True)
    location_name = Column(String(200), nullable=True)
    verdict = Column(String(10), nullable=False)
    reason = Column(String(40), nullable=True)
    rtt_ms = Column(Float, nullable=True)
    handshake_ms = Column(Float, nullable=True)
    tcp_min_ms = Column(Float, nullable=True)
    speed_mbps = Column(Float, nullable=True)
    exit_ip = Column(String(64), nullable=True)
    exit_country = Column(String(8), nullable=True)
    sni_from_config = Column(Boolean, default=False)

    __table_args__ = (
        Index('idx_xray_test_results_run', 'run_id'),
    )


class ServerDowntime(Base):
    """Интервалы простоя: недоступность ноды и остановки самой панели.

    Без них провал в истории трафика не отличить от нулевого трафика.
    """
    __tablename__ = "server_downtime"

    id = Column(BigInteger, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False)
    started_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime, nullable=True)  # NULL — простой длится сейчас
    kind = Column(String(8), nullable=False)    # node | panel

    __table_args__ = (
        Index('idx_server_downtime_lookup', 'server_id', 'started_at'),
    )
