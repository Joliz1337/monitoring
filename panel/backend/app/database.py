"""
Database module with PostgreSQL support and automatic SQLite migration.

On first startup:
1. Creates PostgreSQL tables
2. Checks if SQLite database exists with data
3. Migrates all data from SQLite to PostgreSQL
4. Marks migration as complete
"""

import asyncio
import os
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Ensure data directory exists (for migration flag file)
os.makedirs("data", exist_ok=True)

# Migration flag file
MIGRATION_FLAG_FILE = "data/.postgres_migrated"

# PostgreSQL engine with connection pool
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=3600,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _is_migration_done() -> bool:
    """Check if migration from SQLite has been completed."""
    return Path(MIGRATION_FLAG_FILE).exists()


def _mark_migration_done():
    """Mark migration as completed."""
    Path(MIGRATION_FLAG_FILE).write_text(datetime.utcnow().isoformat())
    logger.info("Migration marked as complete")


def _get_sqlite_path() -> Optional[Path]:
    """Get SQLite database path if it exists."""
    sqlite_path = Path(settings.sqlite_path)
    if sqlite_path.exists() and sqlite_path.stat().st_size > 0:
        return sqlite_path
    return None


def _get_sqlite_tables(sqlite_conn: sqlite3.Connection) -> list[str]:
    """Get list of tables in SQLite database."""
    cursor = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return [row[0] for row in cursor.fetchall()]


def _get_table_columns(sqlite_conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    """Get column names and types for a table."""
    cursor = sqlite_conn.execute(f"PRAGMA table_info({table})")
    return [(row[1], row[2]) for row in cursor.fetchall()]


async def _get_pg_columns(db: AsyncSession, table: str) -> set[str]:
    """Get column names that exist in PostgreSQL table."""
    result = await db.execute(text(f"""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = '{table}' AND table_schema = 'public'
    """))
    return {row[0] for row in result.fetchall()}


def _parse_datetime(val: str) -> datetime:
    """Parse datetime string from SQLite to Python datetime."""
    if not val:
        return None
    # Try common formats
    for fmt in [
        '%Y-%m-%d %H:%M:%S.%f',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%dT%H:%M:%S.%f',
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%d',
    ]:
        try:
            return datetime.strptime(val, fmt)
        except ValueError:
            continue
    return None


async def _migrate_table_data(
    sqlite_conn: sqlite3.Connection,
    db: AsyncSession,
    table: str,
    columns: list[tuple[str, str]],
    batch_size: int = 100
) -> int:
    """Migrate data from SQLite table to PostgreSQL using parameterized queries."""
    
    # Get columns that exist in PostgreSQL (filter out missing ones)
    pg_columns = await _get_pg_columns(db, table)
    
    # Filter columns to only those that exist in both SQLite and PostgreSQL
    filtered_columns = [(name, type_) for name, type_ in columns if name in pg_columns]
    if len(filtered_columns) < len(columns):
        missing = [name for name, _ in columns if name not in pg_columns]
        logger.info(f"  {table}: Skipping columns not in PostgreSQL: {missing}")
    
    col_names = [col[0] for col in filtered_columns]
    col_list = ", ".join(f'"{c}"' for c in col_names)
    sqlite_col_list = ", ".join(f'"{c}"' for c in col_names)
    
    # Count rows
    cursor = sqlite_conn.execute(f"SELECT COUNT(*) FROM {table}")
    total_rows = cursor.fetchone()[0]
    
    if total_rows == 0:
        return 0
    
    logger.info(f"Migrating {total_rows} rows from {table}...")
    
    # Read and insert in batches
    migrated = 0
    failed = 0
    cursor = sqlite_conn.execute(f"SELECT {sqlite_col_list} FROM {table}")
    
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        
        batch_success = 0
        
        # Insert row by row with parameterized queries for safety
        for row in rows:
            # Build parameter dict
            params = {}
            placeholders = []
            for i, val in enumerate(row):
                param_name = f"p{i}"
                col_type = filtered_columns[i][1].upper()
                
                if val is None:
                    placeholders.append("NULL")
                elif col_type == "BOOLEAN":
                    placeholders.append(f":{param_name}")
                    params[param_name] = bool(val)
                elif col_type in ("INTEGER", "BIGINT", "SMALLINT"):
                    placeholders.append(f":{param_name}")
                    params[param_name] = int(val) if val is not None else None
                elif col_type in ("REAL", "FLOAT", "DOUBLE", "NUMERIC"):
                    placeholders.append(f":{param_name}")
                    params[param_name] = float(val) if val is not None else None
                elif col_type in ("TIMESTAMP", "DATETIME", "DATE"):
                    # Parse datetime string to Python datetime
                    placeholders.append(f":{param_name}")
                    if isinstance(val, str):
                        params[param_name] = _parse_datetime(val)
                    else:
                        params[param_name] = val
                else:
                    # String/Text/JSON - pass as-is, SQLAlchemy handles escaping
                    placeholders.append(f":{param_name}")
                    params[param_name] = str(val) if val is not None else None
            
            insert_sql = f'INSERT INTO "{table}" ({col_list}) VALUES ({", ".join(placeholders)}) ON CONFLICT DO NOTHING'
            
            try:
                await db.execute(text(insert_sql), params)
                batch_success += 1
            except Exception as e:
                failed += 1
                if failed <= 5:  # Log first 5 errors only
                    logger.warning(f"Error inserting row into {table}: {e}")
                # Rollback failed transaction to continue
                await db.rollback()
        
        # Commit batch
        try:
            await db.commit()
            migrated += batch_success
        except Exception as e:
            logger.warning(f"Error committing batch to {table}: {e}")
            await db.rollback()
        
        if (migrated + failed) % 5000 == 0:
            logger.info(f"  {table}: {migrated}/{total_rows} rows migrated ({failed} failed)")
    
    if failed > 0:
        logger.warning(f"  {table}: {failed} rows failed to migrate")
    
    return migrated


async def _get_postgres_tables(db: AsyncSession) -> set[str]:
    """Get list of existing tables in PostgreSQL."""
    result = await db.execute(text("""
        SELECT table_name FROM information_schema.tables 
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    """))
    return {row[0] for row in result.fetchall()}


# Tables that must be migrated first (parent tables for foreign keys)
MIGRATION_ORDER = [
    'servers',  # Parent for metrics_snapshot, aggregated_metrics
    'remnawave_settings',
    'remnawave_nodes',
    'panel_settings',
    'blocklist_sources',
    'blocklist_rules',
    'failed_logins',
    'remnawave_user_cache',
    'xray_visit_stats',
    'xray_user_ip_stats',
    'xray_ip_destination_stats',
    'xray_hourly_stats',
    'metrics_snapshot',
    'aggregated_metrics',
]


async def _migrate_from_sqlite():
    """Migrate all data from SQLite to PostgreSQL."""
    import shutil
    
    sqlite_path = _get_sqlite_path()
    if not sqlite_path:
        logger.info("No SQLite database found, skipping migration")
        _mark_migration_done()
        return
    
    logger.info(f"Starting migration from SQLite: {sqlite_path}")
    
    # Create backup BEFORE migration
    backup_path = sqlite_path.with_suffix('.db.backup')
    try:
        shutil.copy2(str(sqlite_path), str(backup_path))
        logger.info(f"SQLite backup created: {backup_path}")
    except Exception as e:
        logger.error(f"Failed to create SQLite backup: {e}")
        logger.error("Migration aborted - cannot proceed without backup")
        return
    
    # Verify backup exists and has same size
    if not backup_path.exists() or backup_path.stat().st_size != sqlite_path.stat().st_size:
        logger.error("Backup verification failed - sizes don't match")
        return
    
    # Connect to SQLite
    sqlite_conn = sqlite3.connect(str(sqlite_path))
    
    try:
        sqlite_tables = set(_get_sqlite_tables(sqlite_conn))
        if not sqlite_tables:
            logger.info("SQLite database is empty, skipping migration")
            _mark_migration_done()
            return
        
        logger.info(f"Found {len(sqlite_tables)} tables in SQLite: {sqlite_tables}")
        
        total_migrated = 0
        migrated_tables = []
        
        async with async_session() as db:
            # Disable foreign key checks for migration (allows orphaned records)
            await db.execute(text("SET session_replication_role = 'replica'"))
            await db.commit()
            logger.info("Foreign key checks disabled for migration")
            
            try:
                # Get existing PostgreSQL tables
                pg_tables = await _get_postgres_tables(db)
                logger.info(f"PostgreSQL has {len(pg_tables)} tables: {pg_tables}")
                
                # Build migration order: first ordered tables, then remaining
                ordered_tables = [t for t in MIGRATION_ORDER if t in sqlite_tables and t in pg_tables]
                remaining_tables = [t for t in sqlite_tables if t in pg_tables and t not in ordered_tables]
                tables_to_migrate = ordered_tables + remaining_tables
                
                # Skip tables that don't exist in PostgreSQL (e.g., ext_* tables)
                skipped = sqlite_tables - pg_tables
                if skipped:
                    logger.info(f"Skipping tables not in PostgreSQL schema: {skipped}")
                
                for table in tables_to_migrate:
                    columns = _get_table_columns(sqlite_conn, table)
                    if not columns:
                        continue
                    
                    try:
                        migrated = await _migrate_table_data(sqlite_conn, db, table, columns)
                        total_migrated += migrated
                        migrated_tables.append(table)
                        logger.info(f"  {table}: {migrated} rows migrated")
                    except Exception as e:
                        logger.error(f"Error migrating table {table}: {e}")
                
                # Reset sequences for auto-increment columns (only for migrated tables)
                for table in migrated_tables:
                    try:
                        await db.execute(text(f"""
                            SELECT setval(pg_get_serial_sequence('"{table}"', 'id'), 
                                   COALESCE((SELECT MAX(id) FROM "{table}"), 0) + 1, false)
                        """))
                        await db.commit()
                    except Exception:
                        pass  # Table might not have id column or sequence
            finally:
                # Re-enable foreign key checks
                await db.execute(text("SET session_replication_role = 'origin'"))
                await db.commit()
                logger.info("Foreign key checks re-enabled")
        
        logger.info(f"Migration complete: {total_migrated} total rows migrated from {len(migrated_tables)} tables")
        
        # Remove original SQLite file (backup exists)
        try:
            sqlite_path.unlink()
            logger.info(f"Original SQLite file removed, backup at: {backup_path}")
        except Exception as e:
            logger.warning(f"Could not remove original SQLite file: {e}")
        
    finally:
        sqlite_conn.close()
    
    _mark_migration_done()


async def run_migrations(conn):
    """Run database migrations for existing tables (PostgreSQL)."""
    
    # Check if servers table exists and has required columns
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'servers'
    """))
    columns = {row[0] for row in result.fetchall()}
    
    if columns:  # Table exists
        # Add missing columns to servers table
        migrations = [
            ("last_seen", "TIMESTAMP"),
            ("last_error", "VARCHAR(500)"),
            ("error_code", "INTEGER"),
            ("last_metrics", "TEXT"),
            ("last_haproxy_data", "TEXT"),
            ("last_traffic_data", "TEXT"),
        ]
        
        for col_name, col_type in migrations:
            if col_name not in columns:
                try:
                    await conn.execute(text(f'ALTER TABLE servers ADD COLUMN "{col_name}" {col_type}'))
                    logger.info(f"Added column: servers.{col_name}")
                except Exception as e:
                    if "already exists" not in str(e).lower():
                        logger.warning(f"Could not add column {col_name}: {e}")
    
    # Check metrics_snapshots columns
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'metrics_snapshots'
    """))
    snapshot_columns = {row[0] for row in result.fetchall()}
    
    if snapshot_columns and "per_cpu_percent" not in snapshot_columns:
        try:
            await conn.execute(text('ALTER TABLE metrics_snapshots ADD COLUMN "per_cpu_percent" TEXT'))
            logger.info("Added column: metrics_snapshots.per_cpu_percent")
        except Exception:
            pass
    
    # Check remnawave_user_cache columns
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'remnawave_user_cache'
    """))
    user_cache_columns = {row[0] for row in result.fetchall()}
    
    if user_cache_columns:
        new_columns = [
            ("short_uuid", "VARCHAR(50)"),
            ("expire_at", "TIMESTAMP"),
            ("subscription_url", "VARCHAR(500)"),
            ("sub_revoked_at", "TIMESTAMP"),
            ("sub_last_user_agent", "VARCHAR(500)"),
            ("sub_last_opened_at", "TIMESTAMP"),
            ("traffic_limit_bytes", "BIGINT"),
            ("traffic_limit_strategy", "VARCHAR(20)"),
            ("last_traffic_reset_at", "TIMESTAMP"),
            ("used_traffic_bytes", "BIGINT"),
            ("lifetime_used_traffic_bytes", "BIGINT"),
            ("online_at", "TIMESTAMP"),
            ("first_connected_at", "TIMESTAMP"),
            ("last_connected_node_uuid", "VARCHAR(100)"),
            ("hwid_device_limit", "INTEGER"),
            ("user_email", "VARCHAR(200)"),
            ("description", "TEXT"),
            ("tag", "VARCHAR(100)"),
            ("created_at", "TIMESTAMP"),
        ]
        
        for col_name, col_type in new_columns:
            if col_name not in user_cache_columns:
                try:
                    await conn.execute(text(f'ALTER TABLE remnawave_user_cache ADD COLUMN "{col_name}" {col_type}'))
                    logger.info(f"Added column: remnawave_user_cache.{col_name}")
                except Exception:
                    pass
    
    # Check xray_user_ip_stats columns
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'xray_user_ip_stats'
    """))
    ip_stats_columns = {row[0] for row in result.fetchall()}
    
    if ip_stats_columns and "is_infrastructure" not in ip_stats_columns:
        try:
            await conn.execute(text('ALTER TABLE xray_user_ip_stats ADD COLUMN "is_infrastructure" BOOLEAN DEFAULT FALSE'))
            logger.info("Added column: xray_user_ip_stats.is_infrastructure")
        except Exception:
            pass


async def init_db():
    """Initialize database: create tables, run migrations, migrate from SQLite if needed."""
    async with engine.begin() as conn:
        # Create all tables
        await conn.run_sync(Base.metadata.create_all)
        
        # Run PostgreSQL migrations
        await run_migrations(conn)
    
    # Migrate from SQLite if not done yet
    if not _is_migration_done():
        try:
            await _migrate_from_sqlite()
        except Exception as e:
            logger.error(f"SQLite migration failed: {e}")
            # Mark as done anyway to prevent repeated attempts
            _mark_migration_done()


async def get_db():
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
