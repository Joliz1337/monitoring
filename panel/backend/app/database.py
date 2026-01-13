from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text
from app.config import get_settings
import os
import logging

logger = logging.getLogger(__name__)
settings = get_settings()

os.makedirs("data", exist_ok=True)

engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def run_migrations(conn):
    """Run database migrations for existing tables"""
    
    # Get existing columns for servers table
    result = await conn.execute(text("PRAGMA table_info(servers)"))
    columns = {row[1] for row in result.fetchall()}
    
    # Add missing columns to servers table
    if "last_seen" not in columns:
        await conn.execute(text("ALTER TABLE servers ADD COLUMN last_seen DATETIME"))
        logger.info("Added column: servers.last_seen")
    
    if "last_error" not in columns:
        await conn.execute(text("ALTER TABLE servers ADD COLUMN last_error VARCHAR(500)"))
        logger.info("Added column: servers.last_error")
    
    if "error_code" not in columns:
        await conn.execute(text("ALTER TABLE servers ADD COLUMN error_code INTEGER"))
        logger.info("Added column: servers.error_code")
    
    if "last_metrics" not in columns:
        await conn.execute(text("ALTER TABLE servers ADD COLUMN last_metrics TEXT"))
        logger.info("Added column: servers.last_metrics")
    
    if "last_haproxy_data" not in columns:
        await conn.execute(text("ALTER TABLE servers ADD COLUMN last_haproxy_data TEXT"))
        logger.info("Added column: servers.last_haproxy_data")
    
    if "last_traffic_data" not in columns:
        await conn.execute(text("ALTER TABLE servers ADD COLUMN last_traffic_data TEXT"))
        logger.info("Added column: servers.last_traffic_data")
    
    # Check if aggregated_metrics table exists
    result = await conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='aggregated_metrics'"
    ))
    if not result.fetchone():
        logger.info("Table aggregated_metrics will be created by create_all")


async def init_db():
    async with engine.begin() as conn:
        # First create all tables
        await conn.run_sync(Base.metadata.create_all)
        # Then run migrations for existing data
        await run_migrations(conn)


async def get_db():
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
