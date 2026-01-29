import logging
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import delete

from app.database import init_db, async_session
from app.config import get_settings
from app.routers import servers, auth_router, proxy, settings as settings_router, system, bulk_actions, blocklist
from app.services.metrics_collector import start_collector, stop_collector
from app.services.blocklist_manager import get_blocklist_manager
from app.security import SecurityMiddleware
# Import all models to register them with Base.metadata
from app.models import Server, MetricsSnapshot, AggregatedMetrics, PanelSettings, FailedLogin, BlocklistRule, BlocklistSource  # noqa: F401

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
    
    # Start blocklist manager for auto-updates
    blocklist_manager = get_blocklist_manager()
    await blocklist_manager.start()
    
    yield
    
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

app.include_router(auth_router.router)
app.include_router(servers.router)
app.include_router(proxy.router)
app.include_router(settings_router.router)
app.include_router(system.router)
app.include_router(bulk_actions.router)
app.include_router(blocklist.router)

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
