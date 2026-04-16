"""Server Monitoring Agent API — Main Application

Nginx на порту 9100 требует клиентский сертификат (mTLS), подписанный панельным CA.
Uvicorn слушает только 127.0.0.1:7500 и доверяет прошедшим mTLS запросам от nginx.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

from app.config import get_settings
from app.routers import haproxy, metrics, traffic, system, ipset, remnawave, speedtest, ssh, ssl
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

    from app.services.haproxy_manager import get_haproxy_manager
    haproxy_manager = get_haproxy_manager()
    success, msg = haproxy_manager.full_init()
    logger.info(f"HAProxy initialization: {msg}")

    traffic_collector = get_traffic_collector()
    try:
        await traffic_collector.init()
        await traffic_collector.start()
        logger.info("Traffic collector started")
    except Exception as e:
        logger.warning(f"Traffic collector init failed: {e}")

    ipset_manager = get_ipset_manager()
    try:
        success, msg = ipset_manager.init_sets()
        logger.info(f"IPSet initialization: {msg}")
    except Exception as e:
        logger.warning(f"IPSet init failed: {e}")

    logger.info("Server ready")
    yield

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

app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
app.add_middleware(SecurityMiddleware)

# Роутеры без auth dependency: nginx делает mTLS до того как запрос попадает в uvicorn.
app.include_router(metrics.router)
app.include_router(haproxy.router)
app.include_router(traffic.router)
app.include_router(system.router)
app.include_router(ipset.router)
app.include_router(remnawave.router)
app.include_router(speedtest.router)
app.include_router(ssh.router)
app.include_router(ssl.router)


@app.get("/health")
async def health_check():
    return {"status": "ok"}


def get_version() -> str:
    from pathlib import Path
    version_file = Path("/app/VERSION")
    if version_file.exists():
        return version_file.read_text().strip()
    return "unknown"


@app.get("/api/version")
async def api_version():
    return {
        "version": get_version(),
        "component": "node",
        "node_name": settings.node_name
    }


@app.get("/")
async def root():
    return {
        "name": "Server Monitoring Agent",
        "version": get_version(),
        "server_name": settings.node_name
    }


@app.get("/api/security/banned")
async def get_banned_ips():
    security = get_security_manager()
    banned = security.get_banned_ips()
    return {"count": len(banned), "banned_ips": banned}


@app.delete("/api/security/banned/{ip}")
async def unban_ip(ip: str):
    security = get_security_manager()
    success = await security.unban_ip(ip)
    return {"success": success}


@app.get("/api/security/config")
async def get_security_config():
    security = get_security_manager()
    return {
        "max_failed_attempts": security.max_failed_attempts,
        "ban_duration_seconds": security.ban_duration
    }
