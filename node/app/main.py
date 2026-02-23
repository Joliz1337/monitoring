"""Server Monitoring Agent API - Main Application

Simple API that returns current system metrics.
All history/calculations are done on the panel side.

Security: Connection drop on all auth failures (no HTTP error responses).
"""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.auth import verify_api_key
from app.config import get_settings
from app.routers import haproxy, metrics, traffic, system, ipset, remnawave, torrent_blocker
from app.security import get_security_manager, SecurityMiddleware
from app.services.traffic_collector import get_traffic_collector
from app.services.ipset_manager import get_ipset_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan"""
    logger.info("Starting Server Monitoring Agent...")
    
    # HAProxy initialization
    from app.services.haproxy_manager import get_haproxy_manager
    haproxy_manager = get_haproxy_manager()
    success, msg = haproxy_manager.full_init()
    logger.info(f"HAProxy initialization: {msg}")
    
    # Traffic collector
    traffic_collector = get_traffic_collector()
    try:
        await traffic_collector.init()
        await traffic_collector.start()
        logger.info("Traffic collector started")
    except Exception as e:
        logger.warning(f"Traffic collector init failed: {e}")
    
    # IPSet manager initialization
    ipset_manager = get_ipset_manager()
    try:
        success, msg = ipset_manager.init_sets()
        logger.info(f"IPSet initialization: {msg}")
    except Exception as e:
        logger.warning(f"IPSet init failed: {e}")
    
    # Xray log collector starts lazily on first request (not all nodes need it)
    
    # Torrent blocker: auto-start if previously enabled
    from app.services.torrent_blocker import get_torrent_blocker
    try:
        tb = get_torrent_blocker()
        await tb.auto_start_if_enabled()
        if tb.is_enabled:
            logger.info("Torrent blocker auto-started (was enabled)")
    except Exception as e:
        logger.warning(f"Torrent blocker auto-start failed: {e}")
    
    logger.info("Server ready")
    yield
    
    # Stop torrent blocker process (preserve enabled state for next startup)
    from app.services.torrent_blocker import get_torrent_blocker
    try:
        tb = get_torrent_blocker()
        if tb._running:
            await tb._graceful_stop()
    except Exception:
        pass
    
    # Stop Xray collector if it was started
    from app.services.xray_log_collector import get_xray_log_collector
    try:
        collector = get_xray_log_collector()
        if collector._running:
            await collector.stop()
    except Exception:
        pass
    try:
        await traffic_collector.stop()
    except Exception:
        pass
    logger.info("Shutdown complete")


settings = get_settings()

app = FastAPI(
    title="Server Monitoring Agent API",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan
)

# Security middleware - drops connections on auth failures
app.add_middleware(SecurityMiddleware)


# Routers with API key authentication
app.include_router(metrics.router, dependencies=[Depends(verify_api_key)])
app.include_router(haproxy.router, dependencies=[Depends(verify_api_key)])
app.include_router(traffic.router, dependencies=[Depends(verify_api_key)])
app.include_router(system.router, dependencies=[Depends(verify_api_key)])
app.include_router(ipset.router, dependencies=[Depends(verify_api_key)])
app.include_router(remnawave.router, dependencies=[Depends(verify_api_key)])
app.include_router(torrent_blocker.router, dependencies=[Depends(verify_api_key)])

@app.get("/health")
async def health_check():
    """Health check (rate limited)"""
    return {"status": "ok"}


def get_version() -> str:
    """Read version"""
    from pathlib import Path
    version_file = Path("/app/VERSION")
    if version_file.exists():
        return version_file.read_text().strip()
    return "unknown"


@app.get("/api/version")
async def api_version():
    """Node version (for panel checks)"""
    return {
        "version": get_version(),
        "component": "node",
        "node_name": settings.node_name
    }


@app.get("/", dependencies=[Depends(verify_api_key)])
async def root():
    """API root"""
    return {
        "name": "Server Monitoring Agent",
        "version": get_version(),
        "server_name": settings.node_name
    }


# Security endpoints
@app.get("/api/security/banned", dependencies=[Depends(verify_api_key)])
async def get_banned_ips():
    """Get banned IPs"""
    security = get_security_manager()
    banned = security.get_banned_ips()
    return {"count": len(banned), "banned_ips": banned}


@app.delete("/api/security/banned/{ip}", dependencies=[Depends(verify_api_key)])
async def unban_ip(ip: str):
    """Unban IP"""
    security = get_security_manager()
    success = await security.unban_ip(ip)
    return {"success": success}


@app.get("/api/security/config", dependencies=[Depends(verify_api_key)])
async def get_security_config():
    """Security config"""
    security = get_security_manager()
    return {
        "max_failed_attempts": security.max_failed_attempts,
        "ban_duration_seconds": security.ban_duration
    }
