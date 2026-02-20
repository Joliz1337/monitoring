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
)
from app.services.haproxy_manager import HAProxyRule, get_haproxy_manager
from app.services.firewall_manager import get_firewall_manager

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
                target_ssl=r.target_ssl,
                send_proxy=r.send_proxy
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
        target_ssl=rule.target_ssl,
        send_proxy=rule.send_proxy
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
        target_ssl=rule_data.target_ssl,
        send_proxy=rule_data.send_proxy
    )
    
    success, message = manager.add_rule(rule)
    
    if success:
        logger.info(f"HAProxy rule created: {rule.name} ({rule.rule_type}) port={rule.listen_port} send_proxy={rule.send_proxy}")
    
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


