from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import init_db
from app.config import get_settings
from app.routers import servers, auth_router, proxy, settings as settings_router, system, bulk_actions
from app.services.metrics_collector import start_collector, stop_collector
from app.security import SecurityMiddleware
# Import all models to register them with Base.metadata
from app.models import Server, MetricsSnapshot, AggregatedMetrics, PanelSettings, FailedLogin  # noqa: F401

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await start_collector()
    yield
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


@app.get("/health")
async def health():
    """Health check - minimal info without auth"""
    return {"status": "ok"}
