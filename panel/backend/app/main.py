import asyncio
import logging
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import delete

# Configure logging to show app logs
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

from app.database import init_db, async_session
from app.config import get_settings
from app.routers import servers, auth_router, proxy, settings as settings_router, system, bulk_actions, blocklist, remnawave, alerts, billing, backup
from app.services.metrics_collector import start_collector, stop_collector
from app.services.blocklist_manager import get_blocklist_manager
from app.services.xray_stats_collector import start_xray_stats_collector, stop_xray_stats_collector
from app.services.traffic_analyzer import start_traffic_analyzer, stop_traffic_analyzer
from app.services.server_alerter import start_server_alerter, stop_server_alerter
from app.services.billing_checker import start_billing_checker, stop_billing_checker
from app.security import SecurityMiddleware
# Import all models to register them with Base.metadata
from app.models import (  # noqa: F401
    Server, MetricsSnapshot, AggregatedMetrics, PanelSettings, FailedLogin, 
    BlocklistRule, BlocklistSource, RemnawaveSettings, RemnawaveNode, 
    XrayStats, XrayHourlyStats, RemnawaveUserCache, TrafficAnalyzerSettings,
    TrafficAnomalyLog, UserTrafficSnapshot, AlertSettings, AlertHistory,
    BillingServer, BillingSettings,
)

settings = get_settings()
logger = logging.getLogger(__name__)


async def cleanup_expired_bans():
    """Remove expired bans from database on startup"""
    try:
        async with async_session() as db:
            now = time.time()
            result = await db.execute(
                delete(FailedLogin).where(FailedLogin.banned_until < now)
            )
            await db.commit()
            if result.rowcount > 0:
                logger.info(f"Cleaned up {result.rowcount} expired IP bans from database")
    except Exception as e:
        logger.error(f"Error cleaning up expired bans: {e}")


async def _deferred_startup():
    """Heavy non-critical tasks that run after server is ready to accept requests."""
    try:
        from app.services.xray_stats_collector import rebuild_summaries
        await rebuild_summaries()
        from app.routers.remnawave import warm_batch_cache
        await warm_batch_cache()
        logger.info("Deferred startup tasks completed")
    except Exception as e:
        logger.warning(f"Deferred startup tasks failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await cleanup_expired_bans()
    
    try:
        from app.services._ext import init_ext_db
        from app.database import engine
        await init_ext_db(engine)
    except Exception:
        pass
    
    await start_collector()
    
    blocklist_manager = get_blocklist_manager()
    await blocklist_manager.start()
    
    await start_xray_stats_collector()
    await start_traffic_analyzer()
    await start_server_alerter()
    await start_billing_checker()
    
    # Cache warming runs in background — doesn't block /health
    warmup_task = asyncio.create_task(_deferred_startup())
    
    yield
    
    warmup_task.cancel()
    await stop_billing_checker()
    await stop_server_alerter()
    await stop_traffic_analyzer()
    await stop_xray_stats_collector()
    await blocklist_manager.stop()
    await stop_collector()


def get_version() -> str:
    """Read version from VERSION file"""
    from pathlib import Path
    version_file = Path("/app/VERSION")
    if version_file.exists():
        return version_file.read_text().strip()
    return "1.0.0"


app = FastAPI(
    title="Monitoring Panel API",
    version=get_version(),
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None
)

# Security middleware - drops connections on auth failures (must be first)
app.add_middleware(SecurityMiddleware)

# CORS - restrict to panel domain only (same-origin in production)
cors_origins = []
if settings.domain:
    cors_origins = [f"https://{settings.domain}"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

app.add_middleware(GZipMiddleware, minimum_size=500)

app.include_router(auth_router.router)
app.include_router(servers.router)
app.include_router(proxy.router)
app.include_router(settings_router.router)
app.include_router(system.router)
app.include_router(bulk_actions.router)
app.include_router(blocklist.router)
app.include_router(remnawave.router)
app.include_router(alerts.router)
app.include_router(billing.router)
app.include_router(backup.router)

try:
    from app.routers._internal import router as ext_router
    if ext_router.routes:
        app.include_router(ext_router, prefix="/_int", tags=["internal"])
except Exception:
    pass


@app.get("/health")
async def health():
    """Health check - minimal info without auth"""
    return {"status": "ok"}
