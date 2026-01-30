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
    
    # Add per_cpu_percent column to metrics_snapshots table
    result = await conn.execute(text("PRAGMA table_info(metrics_snapshots)"))
    snapshot_columns = {row[1] for row in result.fetchall()}
    
    if "per_cpu_percent" not in snapshot_columns:
        await conn.execute(text("ALTER TABLE metrics_snapshots ADD COLUMN per_cpu_percent TEXT"))
        logger.info("Added column: metrics_snapshots.per_cpu_percent")
    
    # Migration: Remnawave xray_visit_stats schema change (v2)
    # Old schema had: period_start, period_type columns
    # New schema has: first_seen, last_seen columns (cumulative counters)
    result = await conn.execute(text("PRAGMA table_info(xray_visit_stats)"))
    xray_columns = {row[1] for row in result.fetchall()}
    
    if xray_columns and "period_start" in xray_columns:
        # Old schema detected - drop and recreate
        logger.info("Migrating xray_visit_stats to new schema (dropping old data)...")
        await conn.execute(text("DROP TABLE IF EXISTS xray_visit_stats"))
        logger.info("Dropped old xray_visit_stats table")
    elif xray_columns and "first_seen" not in xray_columns:
        # Table exists but missing new columns - drop and recreate
        logger.info("xray_visit_stats missing required columns, recreating...")
        await conn.execute(text("DROP TABLE IF EXISTS xray_visit_stats"))
        logger.info("Dropped incompatible xray_visit_stats table")
    
    # Check if xray_hourly_stats table exists (new table)
    result = await conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='xray_hourly_stats'"
    ))
    if not result.fetchone():
        logger.info("Table xray_hourly_stats will be created by create_all")
    
    # Migration: Add new columns to remnawave_user_cache for extended user info
    result = await conn.execute(text("PRAGMA table_info(remnawave_user_cache)"))
    user_cache_columns = {row[1] for row in result.fetchall()}
    
    if user_cache_columns:  # Table exists
        new_columns = [
            ("short_uuid", "VARCHAR(50)"),
            ("expire_at", "DATETIME"),
            ("subscription_url", "VARCHAR(500)"),
            ("sub_revoked_at", "DATETIME"),
            ("sub_last_user_agent", "VARCHAR(500)"),
            ("sub_last_opened_at", "DATETIME"),
            ("traffic_limit_bytes", "BIGINT"),
            ("traffic_limit_strategy", "VARCHAR(20)"),
            ("last_traffic_reset_at", "DATETIME"),
            ("used_traffic_bytes", "BIGINT"),
            ("lifetime_used_traffic_bytes", "BIGINT"),
            ("online_at", "DATETIME"),
            ("first_connected_at", "DATETIME"),
            ("last_connected_node_uuid", "VARCHAR(100)"),
            ("hwid_device_limit", "INTEGER"),
            ("user_email", "VARCHAR(200)"),
            ("description", "TEXT"),
            ("tag", "VARCHAR(100)"),
            ("created_at", "DATETIME"),
        ]
        
        for col_name, col_type in new_columns:
            if col_name not in user_cache_columns:
                await conn.execute(text(f"ALTER TABLE remnawave_user_cache ADD COLUMN {col_name} {col_type}"))
                logger.info(f"Added column: remnawave_user_cache.{col_name}")
    
    # Migration: Add is_infrastructure column to xray_user_ip_stats
    result = await conn.execute(text("PRAGMA table_info(xray_user_ip_stats)"))
    ip_stats_columns = {row[1] for row in result.fetchall()}
    
    if ip_stats_columns and "is_infrastructure" not in ip_stats_columns:
        await conn.execute(text("ALTER TABLE xray_user_ip_stats ADD COLUMN is_infrastructure BOOLEAN DEFAULT 0"))
        logger.info("Added column: xray_user_ip_stats.is_infrastructure")


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
