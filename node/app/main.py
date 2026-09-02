"""Server Monitoring Agent API — Main Application

Nginx на порту 9100 требует клиентский сертификат (mTLS), подписанный панельным CA.
Uvicorn слушает только 127.0.0.1:7500 и доверяет прошедшим mTLS запросам от nginx.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

from app.capabilities import CapabilityMiddleware, get_policy
from app.config import get_settings
from app.routers import haproxy, metrics, traffic, system, ipset, remnawave, ssh, ssl, firewall_profile, antiddos, dnat, network, exit_proxy
from app.services.port_traffic_sampler import get_port_traffic_sampler
from app.services.rate_sampler import get_rate_sampler
from app.services.ipset_manager import get_ipset_manager
from app.services.dnat_manager import get_dnat_manager
from app.services.bandwidth_limit import get_bandwidth_limiter
from app.services.extra_ips import get_extra_ip_manager
from app.services.exit_proxy.manager import get_exit_proxy_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan"""
    logger.info("Starting Server Monitoring Agent...")

    # Прогрев посекундного замера до приёма запросов: иначе первый запрос метрик
    # ушёл бы без скоростей и CPU, и панель на цикл посчитала бы дельты сама
    rate_sampler = get_rate_sampler()
    try:
        await asyncio.to_thread(rate_sampler.prime)
        await rate_sampler.start()
    except Exception as e:
        logger.error(f"Rate sampler start failed, speeds fall back to panel-side deltas: {e}", exc_info=True)

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

    dnat_manager = get_dnat_manager()
    try:
        await dnat_manager.start()
        logger.info("DNAT manager started")
    except Exception as e:
        logger.error(f"DNAT manager start failed, port forwarding rules are not restored: {e}", exc_info=True)

    bandwidth_limiter = get_bandwidth_limiter()
    try:
        await bandwidth_limiter.start()
    except Exception as e:
        logger.error(f"Bandwidth limiter start failed, shaping is not restored: {e}", exc_info=True)

    try:
        await get_extra_ip_manager().start()
    except Exception as e:
        logger.error(f"Extra IP manager start failed, a stale transaction may stay pending: {e}", exc_info=True)

    exit_proxy_manager = get_exit_proxy_manager()
    try:
        await exit_proxy_manager.start()
    except Exception as e:
        logger.error(f"Exit proxy start failed, local SOCKS5 exit is not available: {e}", exc_info=True)

    from app.services import cpu_affinity
    from app.services.host_executor import get_host_executor
    affinity_sync = cpu_affinity.ContainerAffinitySync(
        get_host_executor(), os.cpu_count() or 1
    )
    try:
        await affinity_sync.start()
        logger.info("CPU affinity sync started (enabled=%s)", cpu_affinity.is_enabled())
    except Exception as e:
        logger.error(f"CPU affinity sync start failed: {e}", exc_info=True)

    if CAPABILITY_POLICY.unrestricted:
        logger.info("Capabilities: unrestricted")
    else:
        logger.info(
            "Capabilities: %s%s",
            CAPABILITY_POLICY.published(),
            f", unknown tokens: {list(CAPABILITY_POLICY.unknown_tokens)}"
            if CAPABILITY_POLICY.unknown_tokens else "",
        )

    logger.info("Server ready")
    yield

    try:
        await port_sampler.stop()
    except Exception as e:
        logger.error(f"Port traffic sampler stop failed: {e}", exc_info=True)
    try:
        await rate_sampler.stop()
    except Exception as e:
        logger.error(f"Rate sampler stop failed: {e}", exc_info=True)
    try:
        await affinity_sync.stop()
    except Exception as e:
        logger.error(f"CPU affinity sync stop failed: {e}", exc_info=True)
    try:
        await dnat_manager.stop()
    except Exception as e:
        logger.error(f"DNAT manager stop failed: {e}", exc_info=True)
    try:
        await bandwidth_limiter.stop()
    except Exception as e:
        logger.error(f"Bandwidth limiter stop failed: {e}", exc_info=True)
    try:
        await exit_proxy_manager.stop()
    except Exception as e:
        logger.error(f"Exit proxy stop failed: {e}", exc_info=True)
    logger.info("Shutdown complete")


settings = get_settings()
CAPABILITY_POLICY = get_policy()

app = FastAPI(
    title="Server Monitoring Agent API",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan
)

app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)

# Ноду без ограничений слой не трогает вовсе — у подавляющего большинства
# установок стек обработки запроса остаётся прежним.
if not CAPABILITY_POLICY.unrestricted:
    app.add_middleware(CapabilityMiddleware, policy=CAPABILITY_POLICY)

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
app.include_router(dnat.router)
app.include_router(network.router)
app.include_router(exit_proxy.router)


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
        "node_name": settings.node_name,
        "capabilities": CAPABILITY_POLICY.published(),
        "capabilities_unknown": list(CAPABILITY_POLICY.unknown_tokens),
    }
