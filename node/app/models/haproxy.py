"""Pydantic models for HAProxy API"""

from typing import Optional

from pydantic import BaseModel, Field


class HAProxyRuleBase(BaseModel):
    """Base rule model"""
    rule_type: str = Field(..., pattern="^(tcp|https)$", description="Rule type: tcp or https")
    listen_port: int = Field(..., ge=1, le=65535, description="Port to listen on")
    target_ip: str = Field(..., min_length=1, description="Target IP or hostname")
    target_port: int = Field(..., ge=1, le=65535, description="Target port")
    cert_domain: Optional[str] = Field(None, description="Certificate domain (required for https)")
    target_ssl: bool = Field(False, description="Use SSL when connecting to target server")
    send_proxy: bool = Field(False, description="Enable PROXY protocol to backend")


class HAProxyRuleCreate(HAProxyRuleBase):
    """Model for creating a new rule"""
    name: str = Field(..., pattern="^[a-zA-Z0-9_-]+$", min_length=1, max_length=64)


class HAProxyRuleUpdate(BaseModel):
    """Model for updating a rule"""
    rule_type: Optional[str] = Field(None, pattern="^(tcp|https)$", description="Rule type: tcp or https")
    listen_port: Optional[int] = Field(None, ge=1, le=65535)
    target_ip: Optional[str] = Field(None, min_length=1)
    target_port: Optional[int] = Field(None, ge=1, le=65535)
    cert_domain: Optional[str] = Field(None, description="Certificate domain (required for https)")
    target_ssl: Optional[bool] = Field(None, description="Use SSL when connecting to target server")
    send_proxy: Optional[bool] = Field(None, description="Enable PROXY protocol to backend")


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


class CertificateGenerateResponse(BaseModel):
    """Certificate generation result"""
    success: bool
    message: str
    domain: str


class CertificateRenewResponse(BaseModel):
    """Certificate renewal result"""
    success: bool
    message: str
    renewed_domains: list[str]


class CertificateUpdateResponse(BaseModel):
    """Combined certificate update result"""
    updated_domains: list[str]
    count: int


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
    domain: str = Field(..., min_length=1, max_length=253, description="Domain name")
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


class FirewallDeleteByNumberRequest(BaseModel):
    """Request to delete firewall rule by number"""
    rule_number: int = Field(..., ge=1, description="Rule number from UFW status")


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


class ConfigApplyResponse(BaseModel):
    """Config apply result"""
    success: bool
    message: str
    config_valid: bool
    reloaded: bool = False


# ==================== Certificate Auto-Renewal Cron Models ====================

class CronStatus(BaseModel):
    """Certificate auto-renewal cron status"""
    enabled: bool
    cron_file: str
    cron_exists: bool
    script_exists: bool
    schedule: Optional[str] = None


class CronActionResponse(BaseModel):
    """Cron action result"""
    success: bool
    message: str

