"""HAProxy management API endpoints"""

import logging
from fastapi import APIRouter, HTTPException

from app.models.haproxy import (
    AllCertificatesResponse,
    CertificateDeleteResponse,
    CertificateGenerateRequest,
    CertificateGenerateResponseExtended,
    CertificateInfo,
    CertificateRenewResponse,
    CertificateRenewSingleResponse,
    CertificateUpdateResponse,
    CertificateUploadRequest,
    CertificateUploadResponse,
    ConfigApplyRequest,
    ConfigApplyResponse,
    CronActionResponse,
    CronStatus,
    CurrentOptimizationValues,
    FirewallActionRequest,
    FirewallActionResponse,
    FirewallAdvancedActionRequest,
    FirewallRule,
    FirewallRulesResponse,
    FirewallStatusResponse,
    HAProxyActionResponse,
    HAProxyCertsResponse,
    HAProxyConfigResponse,
    HAProxyRuleCreate,
    HAProxyRuleResponse,
    HAProxyRulesListResponse,
    HAProxyRuleUpdate,
    HAProxyStatusResponse,
    HAProxyValidateResponse,
    LimitsConfig,
    OptimizationsApplyResponse,
    OptimizationsStatusResponse,
    OptimizationsUpdateRequest,
    SysctlConfig,
)
from app.services.haproxy_manager import HAProxyRule, get_haproxy_manager
from app.services.firewall_manager import get_firewall_manager
from app.services.host_executor import get_host_executor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/haproxy", tags=["haproxy"])


@router.get("/status", response_model=HAProxyStatusResponse)
async def get_haproxy_status():
    """Get HAProxy service status"""
    manager = get_haproxy_manager()
    return manager.get_status()


@router.get("/logs")
async def get_haproxy_logs(tail: int = 100):
    """Get HAProxy service logs for diagnostics"""
    manager = get_haproxy_manager()
    logs = manager.get_logs(tail=tail)
    return {"logs": logs}


@router.get("/rules", response_model=HAProxyRulesListResponse)
async def list_rules():
    """Get all configured rules"""
    manager = get_haproxy_manager()
    rules = manager.parse_rules()
    
    return HAProxyRulesListResponse(
        count=len(rules),
        rules=[
            HAProxyRuleResponse(
                name=r.name,
                rule_type=r.rule_type,
                listen_port=r.listen_port,
                target_ip=r.target_ip,
                target_port=r.target_port,
                cert_domain=r.cert_domain,
                target_ssl=r.target_ssl
            )
            for r in rules
        ]
    )


@router.get("/rules/{name}", response_model=HAProxyRuleResponse)
async def get_rule(name: str):
    """Get specific rule by name"""
    manager = get_haproxy_manager()
    rule = manager.get_rule(name)
    
    if not rule:
        raise HTTPException(status_code=404)
    
    return HAProxyRuleResponse(
        name=rule.name,
        rule_type=rule.rule_type,
        listen_port=rule.listen_port,
        target_ip=rule.target_ip,
        target_port=rule.target_port,
        cert_domain=rule.cert_domain,
        target_ssl=rule.target_ssl
    )


@router.post("/rules", response_model=HAProxyActionResponse)
async def create_rule(rule_data: HAProxyRuleCreate):
    """Create new routing rule"""
    manager = get_haproxy_manager()
    
    rule = HAProxyRule(
        name=rule_data.name,
        rule_type=rule_data.rule_type,
        listen_port=rule_data.listen_port,
        target_ip=rule_data.target_ip,
        target_port=rule_data.target_port,
        cert_domain=rule_data.cert_domain,
        target_ssl=rule_data.target_ssl
    )
    
    success, message = manager.add_rule(rule)
    
    if success:
        logger.info(f"HAProxy rule created: {rule.name} ({rule.rule_type}) port={rule.listen_port} target_ssl={rule.target_ssl}")
    
    if not success:
        raise HTTPException(status_code=400)
    
    return HAProxyActionResponse(success=True, message=message)


@router.put("/rules/{name}", response_model=HAProxyActionResponse)
async def update_rule(name: str, updates: HAProxyRuleUpdate):
    """Update existing rule"""
    manager = get_haproxy_manager()
    
    update_dict = updates.model_dump(exclude_none=True)
    if not update_dict:
        raise HTTPException(status_code=400)
    
    success, message = manager.update_rule(name, update_dict)
    
    if success:
        logger.info(f"HAProxy rule updated: {name} with {update_dict}")
    
    if not success:
        raise HTTPException(status_code=400)
    
    return HAProxyActionResponse(success=True, message=message)


@router.delete("/rules/{name}", response_model=HAProxyActionResponse)
async def delete_rule(name: str):
    """Delete routing rule"""
    manager = get_haproxy_manager()
    
    rule = manager.get_rule(name)
    rule_type = rule.rule_type if rule else "unknown"
    
    success, message = manager.delete_rule(name)
    
    if success:
        logger.info(f"HAProxy rule deleted: {name} ({rule_type})")
    
    if not success:
        raise HTTPException(status_code=400)
    
    return HAProxyActionResponse(success=True, message=message)


@router.post("/reload", response_model=HAProxyActionResponse)
async def reload_haproxy():
    """Reload HAProxy configuration (graceful, via SIGHUP)"""
    manager = get_haproxy_manager()
    success, message = manager.reload()
    
    if not success:
        raise HTTPException(status_code=500)
    
    return HAProxyActionResponse(success=True, message=message)


@router.post("/restart", response_model=HAProxyActionResponse)
async def restart_haproxy():
    """Restart HAProxy service (full restart)"""
    manager = get_haproxy_manager()
    success, message = manager.restart()
    
    if not success:
        raise HTTPException(status_code=500)
    
    return HAProxyActionResponse(success=True, message=message)


@router.post("/start", response_model=HAProxyActionResponse)
async def start_haproxy():
    """Start HAProxy service"""
    manager = get_haproxy_manager()
    success, message = manager.start_haproxy()
    
    if not success:
        raise HTTPException(status_code=500)
    
    return HAProxyActionResponse(success=True, message=message)


@router.post("/stop", response_model=HAProxyActionResponse)
async def stop_haproxy():
    """Stop HAProxy service"""
    manager = get_haproxy_manager()
    success, message = manager.stop_haproxy()
    
    if not success:
        raise HTTPException(status_code=500)
    
    return HAProxyActionResponse(success=True, message=message)


@router.post("/validate", response_model=HAProxyValidateResponse)
async def validate_config():
    """Validate HAProxy configuration"""
    manager = get_haproxy_manager()
    is_valid, message = manager.check_config()
    
    return HAProxyValidateResponse(valid=is_valid, message=message)


@router.get("/config", response_model=HAProxyConfigResponse)
async def get_config():
    """Get full HAProxy configuration"""
    manager = get_haproxy_manager()
    content = manager.get_config()
    
    return HAProxyConfigResponse(
        content=content,
        path=str(manager.config_path)
    )


@router.post("/config/apply", response_model=ConfigApplyResponse)
async def apply_config(request: ConfigApplyRequest):
    """Apply HAProxy config from panel (replaces file and optionally reloads)"""
    manager = get_haproxy_manager()
    success, message, reloaded = manager.apply_config(
        config_content=request.config_content,
        reload_after=request.reload_after
    )
    
    if success:
        logger.info(f"HAProxy config applied from panel (reloaded: {reloaded})")
    
    return ConfigApplyResponse(
        success=success,
        message=message,
        config_valid=success,
        reloaded=reloaded
    )


@router.get("/certs", response_model=HAProxyCertsResponse)
async def get_available_certificates():
    """Get list of available SSL certificates"""
    manager = get_haproxy_manager()
    certs = manager.get_available_certs()
    
    return HAProxyCertsResponse(certificates=certs)


@router.get("/certs/all", response_model=AllCertificatesResponse)
async def get_all_certificates():
    """Get detailed information about all certificates"""
    manager = get_haproxy_manager()
    certs = manager.get_all_certs_info()
    
    return AllCertificatesResponse(
        certificates=certs,
        count=len(certs)
    )


@router.get("/certs/{domain}", response_model=CertificateInfo)
async def get_certificate_info(domain: str):
    """Get detailed certificate information"""
    manager = get_haproxy_manager()
    info = manager.get_cert_info(domain)
    
    if not info:
        raise HTTPException(status_code=404)
    
    return CertificateInfo(**info)


@router.post("/certs/generate", response_model=CertificateGenerateResponseExtended)
async def generate_certificate(request: CertificateGenerateRequest):
    """Generate new Let's Encrypt certificate using certbot"""
    manager = get_haproxy_manager()
    
    result = await manager.generate_certificate(
        domain=request.domain,
        email=request.email,
        method=request.method
    )
    
    # Handle both old (2-tuple) and new (3-tuple) return format
    if len(result) == 3:
        success, message, error_log = result
    else:
        success, message = result
        error_log = None
    
    if not success:
        return CertificateGenerateResponseExtended(
            success=False,
            message=message,
            domain=request.domain,
            error_log=error_log
        )
    
    return CertificateGenerateResponseExtended(
        success=True,
        message=message,
        domain=request.domain,
        error_log=None
    )


@router.post("/certs/renew", response_model=CertificateRenewResponse)
async def renew_certificates():
    """Renew all Let's Encrypt certificates"""
    logger.info("API: Received request to renew all certificates")
    manager = get_haproxy_manager()
    
    success, message, renewed = await manager.renew_certificates()
    
    logger.info(f"API: Certificate renewal completed - success={success}, renewed={len(renewed)}")
    return CertificateRenewResponse(
        success=success,
        message=message,
        renewed_domains=renewed
    )


@router.post("/certs/{domain}/renew", response_model=CertificateRenewSingleResponse)
async def renew_single_certificate(domain: str):
    """Renew specific Let's Encrypt certificate"""
    logger.info(f"API: Received request to renew certificate for {domain}")
    manager = get_haproxy_manager()
    
    success, message, output_log = await manager.renew_certificate(domain)
    
    logger.info(f"API: Certificate renewal for {domain} completed - success={success}")
    return CertificateRenewSingleResponse(
        success=success,
        message=message,
        domain=domain,
        output_log=output_log
    )


@router.post("/certs/update-combined", response_model=CertificateUpdateResponse)
async def update_combined_certificates():
    """Update all combined certificates from Let's Encrypt originals"""
    manager = get_haproxy_manager()
    updated = manager.update_combined_certs()
    
    return CertificateUpdateResponse(
        updated_domains=updated,
        count=len(updated)
    )


@router.delete("/certs/{domain}", response_model=CertificateDeleteResponse)
async def delete_certificate(domain: str):
    """Delete certificate for a domain"""
    manager = get_haproxy_manager()
    success, message = manager.delete_certificate(domain)
    
    if not success:
        raise HTTPException(status_code=400)
    
    return CertificateDeleteResponse(
        success=True,
        message=message,
        domain=domain
    )


@router.post("/certs/upload", response_model=CertificateUploadResponse)
async def upload_certificate(request: CertificateUploadRequest):
    """Upload custom certificate (cert + key)"""
    manager = get_haproxy_manager()
    
    success, message = manager.upload_certificate(
        domain=request.domain,
        cert_content=request.cert_content,
        key_content=request.key_content
    )
    
    if not success:
        raise HTTPException(status_code=400)
    
    return CertificateUploadResponse(
        success=True,
        message=message,
        domain=request.domain
    )


# ==================== Certificate Auto-Renewal Cron ====================

@router.get("/certs/cron/status", response_model=CronStatus)
async def get_cron_status():
    """Get certificate auto-renewal cron status"""
    manager = get_haproxy_manager()
    status = manager.get_cron_status()
    return CronStatus(**status)


@router.post("/certs/cron/enable", response_model=CronActionResponse)
async def enable_cron():
    """Enable certificate auto-renewal cron (daily at 3:00 AM)"""
    manager = get_haproxy_manager()
    success, message = manager.setup_cert_renewal_cron()
    
    if not success:
        raise HTTPException(status_code=500)
    
    return CronActionResponse(success=True, message=message)


@router.post("/certs/cron/disable", response_model=CronActionResponse)
async def disable_cron():
    """Disable certificate auto-renewal cron"""
    manager = get_haproxy_manager()
    success, message = manager.remove_cert_renewal_cron()
    
    if not success:
        raise HTTPException(status_code=500)
    
    return CronActionResponse(success=True, message=message)


@router.post("/config/regenerate", response_model=HAProxyActionResponse)
async def regenerate_config(preserve_rules: bool = True):
    """Regenerate HAProxy config (preserving rules by default)"""
    manager = get_haproxy_manager()
    success, message = manager.regenerate_config(preserve_rules=preserve_rules)
    
    if not success:
        raise HTTPException(status_code=500)
    
    return HAProxyActionResponse(success=success, message=message)


@router.post("/system/full-init", response_model=HAProxyActionResponse)
async def full_system_init():
    """Full initialization: apply all optimizations and regenerate config"""
    manager = get_haproxy_manager()
    success, message = manager.full_init()
    
    return HAProxyActionResponse(success=success, message=message)


# ==================== Firewall Management Endpoints ====================

@router.get("/firewall/status", response_model=FirewallStatusResponse)
async def get_firewall_status():
    """Get firewall (UFW) status"""
    fw = get_firewall_manager()
    status = fw.get_status()
    
    return FirewallStatusResponse(**status)


@router.get("/firewall/rules", response_model=FirewallRulesResponse)
async def list_firewall_rules():
    """Get all firewall rules"""
    fw = get_firewall_manager()
    rules = fw.list_rules()
    
    return FirewallRulesResponse(
        rules=[
            FirewallRule(
                number=r.number,
                port=r.port,
                protocol=r.protocol,
                action=r.action,
                from_ip=r.from_ip,
                direction=r.direction,
                ipv6=r.ipv6
            )
            for r in rules
        ],
        count=len(rules),
        active=fw.is_active()
    )


@router.post("/firewall/allow", response_model=FirewallActionResponse)
async def allow_port(request: FirewallActionRequest):
    """Open port in firewall"""
    fw = get_firewall_manager()
    success, message, error_log = fw.add_rule(request.port, request.protocol)
    
    if success:
        logger.info(f"Firewall: allowed port {request.port}/{request.protocol}")
    
    return FirewallActionResponse(
        success=success,
        message=message,
        error_log=error_log
    )


@router.post("/firewall/deny", response_model=FirewallActionResponse)
async def deny_port(request: FirewallActionRequest):
    """Close port in firewall"""
    fw = get_firewall_manager()
    success, message, error_log = fw.remove_rule(request.port, request.protocol)
    
    if success:
        logger.info(f"Firewall: denied port {request.port}/{request.protocol}")
    
    return FirewallActionResponse(
        success=success,
        message=message,
        error_log=error_log
    )


@router.delete("/firewall/{port}", response_model=FirewallActionResponse)
async def delete_firewall_rule(port: int, protocol: str = "tcp"):
    """Remove firewall rule by port"""
    fw = get_firewall_manager()
    success, message, error_log = fw.remove_rule(port, protocol)
    
    if success:
        logger.info(f"Firewall: deleted rule for port {port}/{protocol}")
    
    return FirewallActionResponse(
        success=success,
        message=message,
        error_log=error_log
    )


@router.post("/firewall/rule", response_model=FirewallActionResponse)
async def add_advanced_firewall_rule(request: FirewallAdvancedActionRequest):
    """Add firewall rule with full control (action, from_ip, direction)"""
    fw = get_firewall_manager()
    success, message, error_log = fw.add_advanced_rule(
        port=request.port,
        protocol=request.protocol,
        action=request.action,
        from_ip=request.from_ip,
        direction=request.direction
    )
    
    if success:
        logger.info(f"Firewall: added advanced rule - {request.action} {request.direction} "
                   f"port {request.port}/{request.protocol} from {request.from_ip or 'Anywhere'}")
    
    return FirewallActionResponse(
        success=success,
        message=message,
        error_log=error_log
    )


@router.delete("/firewall/rule/{rule_number}", response_model=FirewallActionResponse)
async def delete_firewall_rule_by_number(rule_number: int):
    """Remove firewall rule by its number"""
    fw = get_firewall_manager()
    success, message, error_log = fw.remove_rule_by_number(rule_number)
    
    if success:
        logger.info(f"Firewall: deleted rule #{rule_number}")
    
    return FirewallActionResponse(
        success=success,
        message=message,
        error_log=error_log
    )


@router.post("/firewall/enable", response_model=FirewallActionResponse)
async def enable_firewall():
    """Enable UFW firewall"""
    fw = get_firewall_manager()
    success, message, error_log = fw.enable()
    
    if success:
        logger.info("Firewall enabled via API")
    
    return FirewallActionResponse(
        success=success,
        message=message,
        error_log=error_log
    )


@router.post("/firewall/disable", response_model=FirewallActionResponse)
async def disable_firewall():
    """Disable UFW firewall"""
    fw = get_firewall_manager()
    success, message, error_log = fw.disable()
    
    if success:
        logger.info("Firewall disabled via API")
    
    return FirewallActionResponse(
        success=success,
        message=message,
        error_log=error_log
    )


# ==================== System Optimizations Endpoints ====================

SYSCTL_CONFIG_PATH = "/etc/sysctl.d/99-vless-tuning.conf"
LIMITS_CONFIG_PATH = "/etc/security/limits.d/99-nofile.conf"
SYSTEMD_USER_SLICE_PATH = "/etc/systemd/system/user-.slice.d/limits.conf"
SYSTEMD_SYSTEM_CONF_PATH = "/etc/systemd/system.conf.d/limits.conf"

# Default sysctl optimization config (from install.sh)
DEFAULT_SYSCTL_CONFIG = '''# =============================================================================
# System optimization for VLESS+Reality VPN v2.0
# - Anti-bufferbloat (low jitter for gaming)
# - High connection limits (no bottlenecks)
# - Fast dead connection cleanup
# - Gaming stability (low latency, no drops)
# - Enhanced anti-DDoS protection
# =============================================================================

# --- Disable IPv6 (improves stability, reduces attack surface) ---
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1

# --- BBR + fq_codel (best combo for low latency + anti-bufferbloat) ---
net.ipv4.tcp_congestion_control = bbr
net.core.default_qdisc = fq_codel

# --- File Descriptors (massive limits) ---
fs.file-max = 10485760
fs.nr_open = 10485760

# --- Socket Buffers (OPTIMIZED for low jitter) ---
net.core.rmem_default = 262144
net.core.wmem_default = 262144
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.core.optmem_max = 65536

# TCP buffers: min/default/max - optimized for 1Gbps with low latency
net.ipv4.tcp_rmem = 4096 131072 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
net.ipv4.tcp_mem = 786432 1048576 1572864

# UDP buffers (important for gaming/VoIP)
net.ipv4.udp_rmem_min = 16384
net.ipv4.udp_wmem_min = 16384
net.ipv4.udp_mem = 65536 131072 262144

# --- Connection Queues (high limits) ---
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 65535
net.core.netdev_max_backlog = 50000
net.core.netdev_budget = 50000
net.core.netdev_budget_usecs = 8000

# --- TCP Performance (gaming + VPN optimized) ---
net.ipv4.tcp_fastopen = 3
net.ipv4.tcp_slow_start_after_idle = 0
net.ipv4.tcp_mtu_probing = 1
net.ipv4.tcp_timestamps = 1
net.ipv4.tcp_sack = 1
net.ipv4.tcp_dsack = 1
net.ipv4.tcp_window_scaling = 1
net.ipv4.tcp_adv_win_scale = 1
net.ipv4.tcp_moderate_rcvbuf = 1
net.ipv4.tcp_no_metrics_save = 1

# ECN: Explicit Congestion Notification
net.ipv4.tcp_ecn = 2
net.ipv4.tcp_ecn_fallback = 1
net.ipv4.tcp_frto = 2

# --- TCP Retries (balanced for stability) ---
net.ipv4.tcp_retries1 = 3
net.ipv4.tcp_retries2 = 8

# --- TIME-WAIT (fast cleanup, huge limits) ---
net.ipv4.tcp_max_tw_buckets = 2000000
net.ipv4.tcp_tw_reuse = 1
net.ipv4.ip_local_port_range = 1024 65535

# --- Orphaned Connections ---
net.ipv4.tcp_max_orphans = 524288
net.ipv4.tcp_orphan_retries = 2

# --- Keepalive (detect dead connections faster) ---
net.ipv4.tcp_keepalive_time = 60
net.ipv4.tcp_keepalive_probes = 5
net.ipv4.tcp_keepalive_intvl = 10

# --- Fast Close of Dead Connections ---
net.ipv4.tcp_fin_timeout = 5

# --- Anti-DDoS: SYN Flood Protection ---
net.ipv4.tcp_syncookies = 1
net.ipv4.tcp_syn_retries = 2
net.ipv4.tcp_synack_retries = 2
net.ipv4.tcp_rfc1337 = 1
net.ipv4.tcp_abort_on_overflow = 0
net.ipv4.tcp_max_syn_backlog = 65535

# --- Anti-DDoS: IP Spoofing Protection ---
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1
net.ipv4.conf.all.log_martians = 0
net.ipv4.conf.default.log_martians = 0

# --- Anti-DDoS: ICMP Protection ---
net.ipv4.icmp_echo_ignore_broadcasts = 1
net.ipv4.icmp_ignore_bogus_error_responses = 1
net.ipv4.icmp_echo_ignore_all = 0
net.ipv4.icmp_ratelimit = 1000
net.ipv4.icmp_ratemask = 6168

# --- Anti-DDoS: Redirect Protection ---
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
net.ipv4.conf.all.secure_redirects = 0
net.ipv4.conf.default.secure_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0

# --- Anti-DDoS: IGMP/Multicast limits ---
net.ipv4.igmp_max_memberships = 256
net.ipv4.igmp_max_msf = 256

# --- Conntrack (auto-scaled, fast timeouts) ---
net.netfilter.nf_conntrack_tcp_timeout_syn_sent = 30
net.netfilter.nf_conntrack_tcp_timeout_syn_recv = 30
net.netfilter.nf_conntrack_tcp_timeout_established = 7200
net.netfilter.nf_conntrack_tcp_timeout_fin_wait = 30
net.netfilter.nf_conntrack_tcp_timeout_close_wait = 15
net.netfilter.nf_conntrack_tcp_timeout_last_ack = 15
net.netfilter.nf_conntrack_tcp_timeout_time_wait = 30
net.netfilter.nf_conntrack_tcp_timeout_close = 10
net.netfilter.nf_conntrack_tcp_timeout_max_retrans = 60
net.netfilter.nf_conntrack_tcp_timeout_unacknowledged = 60
net.netfilter.nf_conntrack_udp_timeout = 30
net.netfilter.nf_conntrack_udp_timeout_stream = 120
net.netfilter.nf_conntrack_icmp_timeout = 10
net.netfilter.nf_conntrack_generic_timeout = 60

# --- ARP Cache (prevent table overflow) ---
net.ipv4.neigh.default.gc_thresh1 = 4096
net.ipv4.neigh.default.gc_thresh2 = 8192
net.ipv4.neigh.default.gc_thresh3 = 16384
net.ipv4.neigh.default.gc_stale_time = 60

# --- Memory Pressure ---
vm.swappiness = 10
vm.dirty_ratio = 40
vm.dirty_background_ratio = 10
vm.overcommit_memory = 1
'''

# Default limits config
DEFAULT_LIMITS_CONFIG = '''# File descriptor limits for high connections
* soft nofile 10485760
* hard nofile 10485760
* soft nproc 65535
* hard nproc 65535
* soft memlock unlimited
* hard memlock unlimited
root soft nofile 10485760
root hard nofile 10485760
root soft nproc 65535
root hard nproc 65535
root soft memlock unlimited
root hard memlock unlimited
'''

# Default systemd user slice config
DEFAULT_SYSTEMD_USER_SLICE = '''[Slice]
DefaultLimitNOFILE=10485760
DefaultLimitNPROC=65535
DefaultLimitMEMLOCK=infinity
'''

# Default systemd system config
DEFAULT_SYSTEMD_SYSTEM_CONF = '''[Manager]
DefaultLimitNOFILE=10485760
DefaultLimitNPROC=65535
DefaultLimitMEMLOCK=infinity
'''


async def _read_file_via_host(path: str) -> tuple[bool, str]:
    """Read file from host via nsenter"""
    executor = get_host_executor()
    result = await executor.execute(f"cat {path} 2>/dev/null")
    return result.success, result.stdout if result.success else ""


async def _write_file_via_host(path: str, content: str) -> tuple[bool, str]:
    """Write file to host via nsenter using heredoc"""
    executor = get_host_executor()
    
    # Ensure parent directory exists
    parent_dir = "/".join(path.split("/")[:-1])
    await executor.execute(f"mkdir -p {parent_dir}")
    
    # Use heredoc to write file (bash required for heredoc)
    write_cmd = f'''cat > {path} << 'CONFIG_EOF'
{content}
CONFIG_EOF'''
    
    result = await executor.execute(write_cmd, shell="bash")
    return result.success, result.stderr if not result.success else "OK"


async def _get_sysctl_value(key: str) -> str:
    """Get current sysctl value"""
    executor = get_host_executor()
    result = await executor.execute(f"sysctl -n {key} 2>/dev/null")
    return result.stdout.strip() if result.success else ""


async def _get_current_optimization_values() -> CurrentOptimizationValues:
    """Get current system optimization values"""
    tcp_congestion = await _get_sysctl_value("net.ipv4.tcp_congestion_control")
    ipv6_disabled_str = await _get_sysctl_value("net.ipv6.conf.all.disable_ipv6")
    file_max_str = await _get_sysctl_value("fs.file-max")
    somaxconn_str = await _get_sysctl_value("net.core.somaxconn")
    tcp_tw_reuse_str = await _get_sysctl_value("net.ipv4.tcp_tw_reuse")
    tcp_fastopen_str = await _get_sysctl_value("net.ipv4.tcp_fastopen")
    
    return CurrentOptimizationValues(
        tcp_congestion=tcp_congestion,
        ipv6_disabled=ipv6_disabled_str == "1",
        file_max=int(file_max_str) if file_max_str.isdigit() else 0,
        somaxconn=int(somaxconn_str) if somaxconn_str.isdigit() else 0,
        tcp_tw_reuse=tcp_tw_reuse_str == "1",
        tcp_fastopen=int(tcp_fastopen_str) if tcp_fastopen_str.isdigit() else 0
    )


@router.get("/system/optimizations", response_model=OptimizationsStatusResponse)
async def get_optimizations_status():
    """Get current system optimizations status and config contents"""
    # Read sysctl config
    sysctl_exists, sysctl_content = await _read_file_via_host(SYSCTL_CONFIG_PATH)
    
    # Read limits config
    limits_exists, limits_content = await _read_file_via_host(LIMITS_CONFIG_PATH)
    
    # Get current values
    current_values = await _get_current_optimization_values()
    
    # Determine if optimizations are applied (sysctl file exists and BBR is enabled)
    applied = sysctl_exists and current_values.tcp_congestion == "bbr"
    
    return OptimizationsStatusResponse(
        applied=applied,
        sysctl=SysctlConfig(
            path=SYSCTL_CONFIG_PATH,
            exists=sysctl_exists,
            content=sysctl_content
        ),
        limits=LimitsConfig(
            path=LIMITS_CONFIG_PATH,
            exists=limits_exists,
            content=limits_content
        ),
        current_values=current_values
    )


@router.post("/system/optimize", response_model=OptimizationsApplyResponse)
async def apply_default_optimizations():
    """Apply default system optimizations (BBR, sysctl, limits)"""
    executor = get_host_executor()
    errors: list[str] = []
    sysctl_applied = False
    limits_applied = False
    systemd_applied = False
    
    # 1. Write sysctl config
    success, msg = await _write_file_via_host(SYSCTL_CONFIG_PATH, DEFAULT_SYSCTL_CONFIG.strip())
    if not success:
        errors.append(f"Failed to write sysctl config: {msg}")
    else:
        # Apply sysctl settings
        result = await executor.execute(f"sysctl -p {SYSCTL_CONFIG_PATH} 2>&1")
        if result.success:
            sysctl_applied = True
            logger.info("Sysctl optimizations applied")
        else:
            errors.append(f"Failed to apply sysctl: {result.stderr or result.stdout}")
    
    # 2. Write limits config
    success, msg = await _write_file_via_host(LIMITS_CONFIG_PATH, DEFAULT_LIMITS_CONFIG.strip())
    if not success:
        errors.append(f"Failed to write limits config: {msg}")
    else:
        limits_applied = True
        logger.info("Limits config written")
    
    # 3. Write systemd configs
    success1, _ = await _write_file_via_host(SYSTEMD_USER_SLICE_PATH, DEFAULT_SYSTEMD_USER_SLICE.strip())
    success2, _ = await _write_file_via_host(SYSTEMD_SYSTEM_CONF_PATH, DEFAULT_SYSTEMD_SYSTEM_CONF.strip())
    
    if success1 and success2:
        # Reload systemd
        result = await executor.execute("systemctl daemon-reload 2>&1")
        if result.success:
            systemd_applied = True
            logger.info("Systemd limits configured")
        else:
            errors.append(f"Failed to reload systemd: {result.stderr}")
    
    # 4. Load conntrack module
    await executor.execute("modprobe nf_conntrack 2>/dev/null")
    
    # 5. Configure PAM limits
    pam_check = await executor.execute("grep -q pam_limits.so /etc/pam.d/common-session 2>/dev/null")
    if not pam_check.success:
        await executor.execute('echo "session required pam_limits.so" >> /etc/pam.d/common-session 2>/dev/null')
    
    overall_success = sysctl_applied and limits_applied
    
    if overall_success:
        message = "System optimizations applied successfully"
        if errors:
            message += f" (with {len(errors)} warnings)"
    else:
        message = "Failed to apply some optimizations"
    
    logger.info(f"Optimizations apply result: sysctl={sysctl_applied}, limits={limits_applied}, systemd={systemd_applied}")
    
    return OptimizationsApplyResponse(
        success=overall_success,
        message=message,
        sysctl_applied=sysctl_applied,
        limits_applied=limits_applied,
        systemd_applied=systemd_applied,
        errors=errors
    )


@router.put("/system/optimizations", response_model=OptimizationsApplyResponse)
async def update_optimizations(request: OptimizationsUpdateRequest):
    """Update system optimization configs with custom content"""
    executor = get_host_executor()
    errors: list[str] = []
    sysctl_applied = False
    limits_applied = False
    
    # 1. Update sysctl config if provided
    if request.sysctl_content is not None:
        success, msg = await _write_file_via_host(SYSCTL_CONFIG_PATH, request.sysctl_content.strip())
        if not success:
            errors.append(f"Failed to write sysctl config: {msg}")
        elif request.apply:
            result = await executor.execute(f"sysctl -p {SYSCTL_CONFIG_PATH} 2>&1")
            if result.success:
                sysctl_applied = True
                logger.info("Custom sysctl config applied")
            else:
                errors.append(f"Failed to apply sysctl: {result.stderr or result.stdout}")
        else:
            sysctl_applied = True  # Written but not applied
    
    # 2. Update limits config if provided
    if request.limits_content is not None:
        success, msg = await _write_file_via_host(LIMITS_CONFIG_PATH, request.limits_content.strip())
        if not success:
            errors.append(f"Failed to write limits config: {msg}")
        else:
            limits_applied = True
            logger.info("Custom limits config written")
    
    # Determine overall success
    sysctl_ok = request.sysctl_content is None or sysctl_applied
    limits_ok = request.limits_content is None or limits_applied
    overall_success = sysctl_ok and limits_ok and len(errors) == 0
    
    if overall_success:
        message = "Optimization configs updated successfully"
        if request.apply:
            message += " and applied"
    else:
        message = "Failed to update some configs"
    
    return OptimizationsApplyResponse(
        success=overall_success,
        message=message,
        sysctl_applied=sysctl_applied,
        limits_applied=limits_applied,
        systemd_applied=False,
        errors=errors
    )
