"""Pydantic models for HAProxy API"""

from typing import Optional

from pydantic import BaseModel, Field

# target_ip и cert_domain подставляются прямо в текст haproxy.cfg, а домен
# сертификата ещё и в путь к файлу. Опасны здесь пробел и перевод строки
# (произвольная директива в конфиге) плюс «..» и слэш (выход за каталог
# сертификатов); всё остальное сужать нельзя, иначе отвалятся адреса, которые
# HAProxy принимает штатно.
# Метка DNS-имени; подчёркивание разрешено — оно обычно во внутренних зонах.
# Без look-ahead: pydantic компилирует pattern движком regex из Rust, который
# его не поддерживает.
_LABEL = r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?"

# Домен сертификата уходит в путь, поэтому только метки и точки между ними:
# пустых меток нет — значит не пройдут ни «..», ни ведущая точка, ни слэш.
DOMAIN_PATTERN = rf"^{_LABEL}(?:\.{_LABEL})*$"
# То же, но допускает пустую строку: у tcp-правил домена нет, и клиенты
# присылают «» наравне с null.
OPTIONAL_DOMAIN_PATTERN = rf"^(?:{_LABEL}(?:\.{_LABEL})*)?$"
# Адрес бэкенда в путь не попадает, так что здесь хватает белого списка
# символов: он покрывает hostname, IPv4 и все формы IPv6 — сжатую, в скобках,
# с zone id, IPv4-mapped, — и при этом отсекает пробелы, кавычки, «;» и «#»,
# которыми дописывают директиву. Пусто — у балансировщиков адрес задают
# серверы бэкенда.
TARGET_HOST_PATTERN = r"^[A-Za-z0-9._:%\[\]-]*$"


class BackendServerModel(BaseModel):
    name: str
    address: str
    port: int
    weight: int = 1
    maxconn: Optional[int] = None
    check: bool = True
    inter: str = "5s"
    fall: int = 3
    rise: int = 2
    send_proxy: bool = False
    send_proxy_v2: bool = False
    backup: bool = False
    slowstart: Optional[str] = None
    disabled: bool = False


class BalancerOptionsModel(BaseModel):
    algorithm: str = "roundrobin"
    algorithm_param: Optional[str] = None
    hash_type: Optional[str] = None
    health_check_type: Optional[str] = None
    httpchk_method: Optional[str] = None
    httpchk_uri: Optional[str] = None
    httpchk_expect: Optional[str] = None
    sticky_type: Optional[str] = None
    cookie_name: Optional[str] = None
    cookie_options: Optional[str] = None
    stick_table_type: Optional[str] = None
    stick_table_size: Optional[str] = None
    stick_table_expire: Optional[str] = None
    retries: int = 3
    redispatch: bool = True
    allbackups: bool = False
    fullconn: Optional[int] = None
    timeout_queue: Optional[str] = None


class HAProxyRuleBase(BaseModel):
    """Base rule model"""
    rule_type: str = Field(..., pattern="^(tcp|https)$", description="Rule type: tcp or https")
    listen_port: int = Field(..., ge=1, le=65535, description="Port to listen on")
    target_ip: str = Field("", pattern=TARGET_HOST_PATTERN, max_length=253, description="Target IP or hostname")
    target_port: int = Field(0, ge=0, le=65535, description="Target port")
    cert_domain: Optional[str] = Field(None, pattern=OPTIONAL_DOMAIN_PATTERN, max_length=253, description="Certificate domain (required for https)")
    target_ssl: bool = Field(False, description="Use SSL when connecting to target server")
    send_proxy: bool = Field(False, description="Enable PROXY protocol to backend")
    accept_proxy: bool = Field(False, description="Accept PROXY protocol on frontend bind")
    use_wildcard: bool = Field(False, description="Use wildcard certificate from parent domain")
    is_balancer: bool = Field(False, description="Load balancer mode")
    servers: list[BackendServerModel] = Field(default_factory=list)
    balancer_options: Optional[BalancerOptionsModel] = None


class HAProxyRuleCreate(HAProxyRuleBase):
    """Model for creating a new rule"""
    name: str = Field(..., pattern="^[a-zA-Z0-9_-]+$", min_length=1, max_length=64)


class HAProxyRuleUpdate(BaseModel):
    """Model for updating a rule"""
    rule_type: Optional[str] = Field(None, pattern="^(tcp|https)$", description="Rule type: tcp or https")
    listen_port: Optional[int] = Field(None, ge=1, le=65535)
    target_ip: Optional[str] = Field(None, pattern=TARGET_HOST_PATTERN, max_length=253)
    target_port: Optional[int] = Field(None, ge=0, le=65535)
    cert_domain: Optional[str] = Field(None, pattern=OPTIONAL_DOMAIN_PATTERN, max_length=253, description="Certificate domain (required for https)")
    target_ssl: Optional[bool] = Field(None, description="Use SSL when connecting to target server")
    send_proxy: Optional[bool] = Field(None, description="Enable PROXY protocol to backend")
    accept_proxy: Optional[bool] = Field(None, description="Accept PROXY protocol on frontend bind")
    use_wildcard: Optional[bool] = Field(None, description="Use wildcard certificate from parent domain")
    is_balancer: Optional[bool] = None
    servers: Optional[list[BackendServerModel]] = None
    balancer_options: Optional[BalancerOptionsModel] = None


class HAProxyRuleResponse(BaseModel):
    """Rule response model"""
    name: str
    rule_type: str
    listen_port: int
    target_ip: str
    target_port: int
    cert_domain: Optional[str] = None
    target_ssl: bool = False
    send_proxy: bool = False
    accept_proxy: bool = False
    is_balancer: bool = False
    servers: list[BackendServerModel] = []
    balancer_options: Optional[BalancerOptionsModel] = None


class HAProxyStatusResponse(BaseModel):
    """HAProxy status response"""
    running: bool
    enabled: bool = False  # autostart on boot
    installed: bool = True
    config_valid: bool
    config_exists: bool = True
    config_message: str
    config_path: str = ""
    status_output: str = ""
    service_logs: str = ""


class HAProxyRulesListResponse(BaseModel):
    """List of all rules"""
    count: int
    rules: list[HAProxyRuleResponse]


class HAProxyActionResponse(BaseModel):
    """Response for actions (create, update, delete, reload)"""
    success: bool
    message: str


class HAProxyConfigResponse(BaseModel):
    """Full config content"""
    content: str
    path: str


class HAProxyCertsResponse(BaseModel):
    """Available certificates"""
    certificates: list[str]


class HAProxyValidateResponse(BaseModel):
    """Config validation result"""
    valid: bool
    message: str


class CertificateFiles(BaseModel):
    """Certificate file paths"""
    pem: Optional[str] = None  # Combined cert for HAProxy
    key: Optional[str] = None  # Private key
    cert: Optional[str] = None  # Certificate
    fullchain: Optional[str] = None  # Full certificate chain
    chain: Optional[str] = None  # CA chain


class CertificateInfo(BaseModel):
    """Certificate information"""
    domain: str
    expiry_date: str
    days_left: int
    expired: bool
    combined_exists: bool
    cert_path: str
    source: Optional[str] = None  # 'letsencrypt' or 'custom'
    files: Optional[CertificateFiles] = None  # All certificate file paths


class CertificateGenerateRequest(BaseModel):
    """Request to generate certificate"""
    domain: str = Field(..., min_length=1, description="Domain name")
    email: Optional[str] = Field(None, description="Email for Let's Encrypt notifications")
    method: str = Field("standalone", pattern="^(standalone|webroot)$")


class CertificateRenewResponse(BaseModel):
    """Certificate renewal result"""
    success: bool
    message: str
    renewed_domains: list[str]


class AllCertificatesResponse(BaseModel):
    """All certificates with details"""
    certificates: list[dict]
    count: int


class CertificateDeleteResponse(BaseModel):
    """Certificate deletion result"""
    success: bool
    message: str
    domain: str


class CertificateUploadRequest(BaseModel):
    """Request to upload custom certificate"""
    domain: str = Field(..., pattern=DOMAIN_PATTERN, max_length=253, description="Domain name")
    cert_content: str = Field(..., min_length=1, description="Certificate content (PEM format)")
    key_content: str = Field(..., min_length=1, description="Private key content (PEM format)")


class CertificateUploadResponse(BaseModel):
    """Certificate upload result"""
    success: bool
    message: str
    domain: str


# ==================== Firewall Models ====================

class FirewallRule(BaseModel):
    """Firewall rule representation"""
    number: int
    port: int
    protocol: str
    action: str  # ALLOW/DENY
    from_ip: str
    direction: str
    ipv6: bool = False


class FirewallRulesResponse(BaseModel):
    """List of firewall rules"""
    rules: list[FirewallRule]
    count: int
    active: bool


class FirewallStatusResponse(BaseModel):
    """Firewall status"""
    active: bool
    default_incoming: str
    default_outgoing: str
    logging: str
    error: Optional[str] = None


class FirewallActionRequest(BaseModel):
    """Request to add/remove firewall rule (simple)"""
    port: int = Field(..., ge=1, le=65535, description="Port number")
    protocol: str = Field("tcp", pattern="^(tcp|udp|any)$", description="Protocol")


class FirewallAdvancedActionRequest(BaseModel):
    """Request to add firewall rule with full control"""
    port: int = Field(..., ge=1, le=65535, description="Port number")
    protocol: str = Field("tcp", pattern="^(tcp|udp|any)$", description="Protocol")
    action: str = Field("allow", pattern="^(allow|deny)$", description="Action: allow or deny")
    from_ip: Optional[str] = Field(None, description="Source IP (None = Anywhere)")
    direction: str = Field("in", pattern="^(in|out)$", description="Direction: in or out")


class FirewallActionResponse(BaseModel):
    """Response for firewall actions"""
    success: bool
    message: str
    error_log: Optional[str] = None


# ==================== Extended Certificate Models ====================

class CertificateGenerateResponseExtended(BaseModel):
    """Extended certificate generation result with error log"""
    success: bool
    message: str
    domain: str
    error_log: Optional[str] = None


class CertificateRenewSingleResponse(BaseModel):
    """Single certificate renewal result with output log"""
    success: bool
    message: str
    domain: str
    output_log: Optional[str] = None


# ==================== Config Apply Models ====================

class ConfigApplyRequest(BaseModel):
    """Request to apply HAProxy config from panel"""
    config_content: str = Field(..., min_length=1, description="Full HAProxy config content")
    reload_after: bool = Field(True, description="Reload HAProxy after applying config")
    ensure_started: bool = Field(False, description="Start HAProxy if stopped (enable autostart)")


class ConfigApplyResponse(BaseModel):
    """Config apply result"""
    success: bool
    message: str
    config_valid: bool
    reloaded: bool = False


# ==================== Live Stats Models ====================

class HAProxyStatRow(BaseModel):
    """One row of `show stat` output (frontend, backend or server)"""
    name: str
    kind: str  # frontend | backend | server
    status: str  # UP/DOWN/OPEN/no check/MAINT/DRAIN/...
    check_status: Optional[str] = None
    addr: Optional[str] = None  # only for servers, haproxy 1.7+
    scur: int = 0
    smax: int = 0
    slim: Optional[int] = None
    stot: int = 0
    rate: int = 0
    rate_max: int = 0
    bin: int = 0
    bout: int = 0
    econ: Optional[int] = None
    eresp: Optional[int] = None
    weight: Optional[int] = None
    backup: bool = False
    lastchg: Optional[int] = None  # seconds since last state change
    downtime: Optional[int] = None


class HAProxyProxyStats(BaseModel):
    """Stats rows grouped by proxy (pxname)"""
    name: str
    mode: Optional[str] = None
    frontend: Optional[HAProxyStatRow] = None
    backend: Optional[HAProxyStatRow] = None
    servers: list[HAProxyStatRow] = []


class HAProxyStatsResponse(BaseModel):
    """Live stats from the HAProxy stats socket"""
    available: bool
    reason: Optional[str] = None  # socket_not_configured|haproxy_stopped|socket_unavailable|timeout|error
    message: Optional[str] = None
    haproxy_version: Optional[str] = None
    uptime_sec: Optional[int] = None
    curr_conns: Optional[int] = None
    proxies: list[HAProxyProxyStats] = []


