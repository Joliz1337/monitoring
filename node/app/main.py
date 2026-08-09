"""Server Monitoring Agent API — Main Application

Nginx на порту 9100 требует клиентский сертификат (mTLS), подписанный панельным CA.
Uvicorn слушает только 127.0.0.1:7500 и доверяет прошедшим mTLS запросам от nginx.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

from app.config import get_settings
from app.routers import haproxy, metrics, traffic, system, ipset, remnawave, ssh, ssl, firewall_profile, antiddos
from app.services.metrics_collector import get_collector as get_metrics_collector
from app.services.port_traffic_sampler import get_port_traffic_sampler
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

    # Прогрев замера CPU до приёма запросов: иначе первый запрос метрик меряет
    # нагрузку на нулевом интервале и отдаёт мусор («одно ядро 100%, остальные 0»)
    await asyncio.to_thread(get_metrics_collector().prime_cpu_baseline)

    from app.services.haproxy_manager import get_haproxy_manager
    haproxy_manager = get_haproxy_manager()
    success, msg = haproxy_manager.full_init()
    logger.info(f"HAProxy initialization: {msg}")

    port_sampler = get_port_traffic_sampler()
    try:
        await port_sampler.init()
        await port_sampler.start()
        logger.info("Port traffic sampler started")
    except Exception as e:
        logger.error(f"Port traffic sampler init failed, per-port counters stay empty: {e}", exc_info=True)

    ipset_manager = get_ipset_manager()
    try:
        success, msg = ipset_manager.init_sets()
        logger.info(f"IPSet initialization: {msg}")
    except Exception as e:
        logger.error(f"IPSet init failed, blocklist rules are not applied: {e}", exc_info=True)

    logger.info("Server ready")
    yield

    try:
        await port_sampler.stop()
    except Exception as e:
        logger.error(f"Port traffic sampler stop failed: {e}", exc_info=True)
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

# Роутеры без auth dependency: nginx делает mTLS до того как запрос попадает в uvicorn.
app.include_router(metrics.router)
app.include_router(haproxy.router)
app.include_router(traffic.router)
app.include_router(system.router)
app.include_router(ipset.router)
app.include_router(remnawave.router)
app.include_router(ssh.router)
app.include_router(ssl.router)
app.include_router(firewall_profile.router)
app.include_router(antiddos.router)


@app.get("/health")
async def health_check():
    return {"status": "ok"}


def _read_version() -> str:
    version_file = Path("/app/VERSION")
    if version_file.exists():
        return version_file.read_text().strip()
    return "unknown"


# Файл лежит в образе и в рантайме не меняется — читаем с диска один раз,
# чтобы async-эндпоинты ниже не ходили в файловую систему на каждый запрос
NODE_VERSION = _read_version()


@app.get("/api/version")
async def api_version():
    return {
        "version": NODE_VERSION,
        "component": "node",
        "node_name": settings.node_name
    }
