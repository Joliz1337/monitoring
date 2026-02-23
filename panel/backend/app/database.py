"""Database module with PostgreSQL support."""

import logging

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# PostgreSQL engine with connection pool
pool_size = 10
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=pool_size,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_timeout=30,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Alias for background tasks
async_session_maker = async_session


class Base(DeclarativeBase):
    pass


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
            ("has_xray_node", "BOOLEAN DEFAULT FALSE"),
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
    
    # Add TCP state columns to metrics_snapshots
    tcp_snapshot_columns = [
        ("tcp_established", "INTEGER"),
        ("tcp_listen", "INTEGER"),
        ("tcp_time_wait", "INTEGER"),
        ("tcp_close_wait", "INTEGER"),
        ("tcp_syn_sent", "INTEGER"),
        ("tcp_syn_recv", "INTEGER"),
        ("tcp_fin_wait", "INTEGER"),
    ]
    for col_name, col_type in tcp_snapshot_columns:
        if snapshot_columns and col_name not in snapshot_columns:
            try:
                await conn.execute(text(f'ALTER TABLE metrics_snapshots ADD COLUMN "{col_name}" {col_type}'))
                logger.info(f"Added column: metrics_snapshots.{col_name}")
            except Exception:
                pass
    
    # Add TCP state columns to aggregated_metrics
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'aggregated_metrics'
    """))
    agg_columns = {row[0] for row in result.fetchall()}
    
    tcp_agg_columns = [
        ("avg_tcp_established", "FLOAT"),
        ("avg_tcp_listen", "FLOAT"),
        ("avg_tcp_time_wait", "FLOAT"),
        ("avg_tcp_close_wait", "FLOAT"),
        ("avg_tcp_syn_sent", "FLOAT"),
        ("avg_tcp_syn_recv", "FLOAT"),
        ("avg_tcp_fin_wait", "FLOAT"),
    ]
    for col_name, col_type in tcp_agg_columns:
        if agg_columns and col_name not in agg_columns:
            try:
                await conn.execute(text(f'ALTER TABLE aggregated_metrics ADD COLUMN "{col_name}" {col_type}'))
                logger.info(f"Added column: aggregated_metrics.{col_name}")
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
    
    # Check remnawave_settings columns
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'remnawave_settings'
    """))
    remnawave_settings_columns = {row[0] for row in result.fetchall()}
    
    if remnawave_settings_columns and "ignored_user_ids" not in remnawave_settings_columns:
        try:
            await conn.execute(text('ALTER TABLE remnawave_settings ADD COLUMN "ignored_user_ids" TEXT'))
            logger.info("Added column: remnawave_settings.ignored_user_ids")
        except Exception:
            pass
    
    # Add retention settings columns to remnawave_settings
    retention_columns = [
        ("visit_stats_retention_days", "INTEGER DEFAULT 365"),
        ("ip_stats_retention_days", "INTEGER DEFAULT 90"),
        ("ip_destination_retention_days", "INTEGER DEFAULT 90"),
        ("hourly_stats_retention_days", "INTEGER DEFAULT 365"),
    ]
    
    for col_name, col_type in retention_columns:
        if remnawave_settings_columns and col_name not in remnawave_settings_columns:
            try:
                await conn.execute(text(f'ALTER TABLE remnawave_settings ADD COLUMN "{col_name}" {col_type}'))
                logger.info(f"Added column: remnawave_settings.{col_name}")
            except Exception:
                pass
    
    # Add direction column to blocklist_rules
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'blocklist_rules'
    """))
    blocklist_rules_columns = {row[0] for row in result.fetchall()}
    
    if blocklist_rules_columns and "direction" not in blocklist_rules_columns:
        try:
            await conn.execute(text("ALTER TABLE blocklist_rules ADD COLUMN direction VARCHAR(3) DEFAULT 'in'"))
            logger.info("Added column: blocklist_rules.direction")
        except Exception:
            pass
    
    # Add direction column to blocklist_sources
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'blocklist_sources'
    """))
    blocklist_sources_columns = {row[0] for row in result.fetchall()}
    
    if blocklist_sources_columns and "direction" not in blocklist_sources_columns:
        try:
            await conn.execute(text("ALTER TABLE blocklist_sources ADD COLUMN direction VARCHAR(3) DEFAULT 'in'"))
            logger.info("Added column: blocklist_sources.direction")
        except Exception:
            pass
    
    # Add new TCP alert columns to alert_settings
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'alert_settings'
    """))
    alert_columns = {row[0] for row in result.fetchall()}
    
    if alert_columns:
        if "language" not in alert_columns:
            try:
                await conn.execute(text("ALTER TABLE alert_settings ADD COLUMN language VARCHAR(5) DEFAULT 'en'"))
                logger.info("Added column: alert_settings.language")
            except Exception:
                pass

        tcp_alert_columns = [
            ("tcp_synsent_enabled", "BOOLEAN DEFAULT FALSE"),
            ("tcp_synsent_spike_percent", "FLOAT DEFAULT 200.0"),
            ("tcp_synsent_sustained_seconds", "INTEGER DEFAULT 300"),
            ("tcp_synrecv_enabled", "BOOLEAN DEFAULT FALSE"),
            ("tcp_synrecv_spike_percent", "FLOAT DEFAULT 200.0"),
            ("tcp_synrecv_sustained_seconds", "INTEGER DEFAULT 300"),
            ("tcp_finwait_enabled", "BOOLEAN DEFAULT FALSE"),
            ("tcp_finwait_spike_percent", "FLOAT DEFAULT 200.0"),
            ("tcp_finwait_sustained_seconds", "INTEGER DEFAULT 300"),
        ]
        for col_name, col_type in tcp_alert_columns:
            if col_name not in alert_columns:
                try:
                    await conn.execute(text(f'ALTER TABLE alert_settings ADD COLUMN "{col_name}" {col_type}'))
                    logger.info(f"Added column: alert_settings.{col_name}")
                except Exception:
                    pass
    
    # Drop redundant indexes (covered by unique constraints or low-cardinality)
    redundant_indexes = [
        "idx_xray_stats_server",      # covered by PK (server_id, ...)
        "idx_xray_stats_visits",       # visit_count never filtered directly
        "idx_user_ip_server",          # covered by PK (server_id, ...)
        "idx_user_ip_infra",           # boolean low-cardinality, seq scan is faster
        "idx_ip_dest_server",          # covered by PK (server_id, ...)
        "idx_user_ip_source",          # replaced by idx_user_ip_source_ip_id
        "idx_xray_stats_email",        # covered by PK (email, source_ip, host)
    ]
    for idx_name in redundant_indexes:
        try:
            await conn.execute(text(f'DROP INDEX IF EXISTS "{idx_name}"'))
        except Exception:
            pass
    
    # Composite index for period-filtered user queries (email + last_seen)
    try:
        await conn.execute(text(
            'CREATE INDEX IF NOT EXISTS idx_xray_stats_email_last_seen ON xray_stats (email, last_seen)'
        ))
    except Exception:
        pass
    
    # Drop unused hit_count column from xray_destinations
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'xray_destinations'
    """))
    xray_dest_columns = {row[0] for row in result.fetchall()}
    
    if xray_dest_columns and "hit_count" in xray_dest_columns:
        try:
            await conn.execute(text('ALTER TABLE xray_destinations DROP COLUMN "hit_count"'))
            logger.info("Dropped column: xray_destinations.hit_count")
        except Exception:
            pass
    
    # Migrate xray_visit_stats and xray_ip_destination_stats to use normalized destinations
    await _migrate_destinations_normalization(conn)
    
    # Add host column to xray_destinations
    await _migrate_destination_host(conn)
    
    # Normalize source_ip into xray_source_ips table
    await _migrate_source_ip_normalization(conn)
    
    # Remove surrogate id columns and convert to composite PKs
    await _migrate_remove_surrogate_ids(conn)
    
    # Migrate to single xray_stats table (replaces 5 old tables)
    await _migrate_to_single_stats_table(conn)
    
    # Drop FK constraint from xray_hourly_stats (server_id=0 used for aggregated data)
    try:
        await conn.execute(text(
            "ALTER TABLE xray_hourly_stats DROP CONSTRAINT IF EXISTS xray_hourly_stats_server_id_fkey"
        ))
    except Exception:
        pass


async def _migrate_destinations_normalization(conn):
    """Migrate destination columns to use normalized xray_destinations table.
    
    This migration is idempotent - safe to run multiple times.
    Handles partially completed migrations gracefully.
    """
    
    # Get current state of xray_visit_stats
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'xray_visit_stats'
    """))
    visit_stats_columns = {row[0] for row in result.fetchall()}
    
    if not visit_stats_columns:
        return  # Table doesn't exist yet, will be created fresh
    
    has_destination = "destination" in visit_stats_columns
    has_destination_id = "destination_id" in visit_stats_columns
    
    # Migration complete: destination_id exists, old destination column is gone
    if has_destination_id and not has_destination:
        logger.info("Destination normalization already complete")
        return
    
    # Nothing to migrate: no old destination column
    if not has_destination and not has_destination_id:
        return
    
    logger.info("Starting destination normalization migration...")
    
    # Get xray_ip_destination_stats columns
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'xray_ip_destination_stats'
    """))
    ip_dest_columns = {row[0] for row in result.fetchall()}
    
    # Step 1: Populate xray_destinations from existing data (if destination column exists)
    if has_destination:
        logger.info("Populating xray_destinations table...")
        try:
            await conn.execute(text("""
                INSERT INTO xray_destinations (destination, first_seen, hit_count)
                SELECT DISTINCT destination, MIN(first_seen), SUM(visit_count)
                FROM xray_visit_stats
                WHERE destination IS NOT NULL
                GROUP BY destination
                ON CONFLICT (destination) DO UPDATE SET hit_count = xray_destinations.hit_count + EXCLUDED.hit_count
            """))
        except Exception as e:
            logger.warning(f"Populating xray_destinations from visit_stats: {e}")
        
        # Also from xray_ip_destination_stats if it has old destination column
        if ip_dest_columns and "destination" in ip_dest_columns:
            try:
                await conn.execute(text("""
                    INSERT INTO xray_destinations (destination, first_seen, hit_count)
                    SELECT DISTINCT destination, MIN(first_seen), SUM(connection_count)
                    FROM xray_ip_destination_stats
                    WHERE destination IS NOT NULL
                    GROUP BY destination
                    ON CONFLICT (destination) DO UPDATE SET hit_count = xray_destinations.hit_count + EXCLUDED.hit_count
                """))
            except Exception as e:
                logger.warning(f"Populating xray_destinations from ip_dest_stats: {e}")
    
    # Step 2: Add destination_id column if not exists
    if not has_destination_id:
        logger.info("Adding destination_id column to xray_visit_stats...")
        try:
            await conn.execute(text("""
                ALTER TABLE xray_visit_stats ADD COLUMN destination_id INTEGER
            """))
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.warning(f"Adding destination_id column: {e}")
    
    # Step 3: Populate destination_id where it's NULL (handles partial migration)
    if has_destination:
        logger.info("Populating destination_id in xray_visit_stats...")
        try:
            await conn.execute(text("""
                UPDATE xray_visit_stats vs
                SET destination_id = d.id
                FROM xray_destinations d
                WHERE vs.destination = d.destination AND vs.destination_id IS NULL
            """))
        except Exception as e:
            logger.warning(f"Populating destination_id: {e}")
    
    # Step 4: Delete rows where destination_id is still null
    try:
        await conn.execute(text("""
            DELETE FROM xray_visit_stats WHERE destination_id IS NULL
        """))
    except Exception as e:
        logger.warning(f"Deleting orphaned rows: {e}")
    
    # Step 5: Make destination_id NOT NULL (if not already)
    try:
        await conn.execute(text("""
            ALTER TABLE xray_visit_stats 
            ALTER COLUMN destination_id SET NOT NULL
        """))
    except Exception as e:
        if "already" not in str(e).lower():
            logger.warning(f"Setting NOT NULL: {e}")
    
    # Step 6: Drop old unique constraint if exists
    try:
        await conn.execute(text("""
            ALTER TABLE xray_visit_stats DROP CONSTRAINT IF EXISTS uq_xray_stats_unique
        """))
    except Exception:
        pass
    
    # Step 7: Add new unique constraint (server_id, destination_id, email) - NO created_at
    try:
        await conn.execute(text("""
            ALTER TABLE xray_visit_stats 
            ADD CONSTRAINT uq_xray_stats_unique_v2 UNIQUE (server_id, destination_id, email)
        """))
    except Exception as e:
        if "already exists" not in str(e).lower():
            logger.debug(f"Adding unique constraint: {e}")
    
    # Step 8: Add FK constraint
    try:
        await conn.execute(text("""
            ALTER TABLE xray_visit_stats 
            ADD CONSTRAINT fk_visit_stats_destination 
            FOREIGN KEY (destination_id) REFERENCES xray_destinations(id) ON DELETE CASCADE
        """))
    except Exception as e:
        if "already exists" not in str(e).lower():
            logger.debug(f"Adding FK constraint: {e}")
    
    # Step 9: Drop old destination column
    if has_destination:
        logger.info("Dropping old destination column from xray_visit_stats...")
        try:
            await conn.execute(text("""
                ALTER TABLE xray_visit_stats DROP COLUMN IF EXISTS destination
            """))
        except Exception as e:
            logger.warning(f"Dropping destination column: {e}")
    
    # Now migrate xray_ip_destination_stats
    ip_has_destination = "destination" in ip_dest_columns
    ip_has_destination_id = "destination_id" in ip_dest_columns
    
    if ip_dest_columns and ip_has_destination:
        logger.info("Migrating xray_ip_destination_stats...")
        
        # Add destination_id column if not exists
        if not ip_has_destination_id:
            try:
                await conn.execute(text("""
                    ALTER TABLE xray_ip_destination_stats ADD COLUMN destination_id INTEGER
                """))
            except Exception as e:
                if "already exists" not in str(e).lower():
                    logger.warning(f"Adding destination_id to ip_dest_stats: {e}")
        
        # Populate destination_id
        try:
            await conn.execute(text("""
                UPDATE xray_ip_destination_stats ips
                SET destination_id = d.id
                FROM xray_destinations d
                WHERE ips.destination = d.destination AND ips.destination_id IS NULL
            """))
        except Exception as e:
            logger.warning(f"Populating destination_id in ip_dest_stats: {e}")
        
        # Delete orphaned rows
        try:
            await conn.execute(text("""
                DELETE FROM xray_ip_destination_stats WHERE destination_id IS NULL
            """))
        except Exception as e:
            logger.warning(f"Deleting orphaned ip_dest_stats rows: {e}")
        
        # Make NOT NULL
        try:
            await conn.execute(text("""
                ALTER TABLE xray_ip_destination_stats 
                ALTER COLUMN destination_id SET NOT NULL
            """))
        except Exception as e:
            if "already" not in str(e).lower():
                logger.warning(f"Setting NOT NULL on ip_dest_stats: {e}")
        
        # Drop old constraint
        try:
            await conn.execute(text("""
                ALTER TABLE xray_ip_destination_stats DROP CONSTRAINT IF EXISTS uq_ip_dest_stats_unique
            """))
        except Exception:
            pass
        
        # Add new constraint (server_id, email, source_ip, destination_id) - NO created_at
        try:
            await conn.execute(text("""
                ALTER TABLE xray_ip_destination_stats 
                ADD CONSTRAINT uq_ip_dest_stats_unique_v2 UNIQUE (server_id, email, source_ip, destination_id)
            """))
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.debug(f"Adding unique constraint to ip_dest_stats: {e}")
        
        # Add FK
        try:
            await conn.execute(text("""
                ALTER TABLE xray_ip_destination_stats 
                ADD CONSTRAINT fk_ip_dest_stats_destination 
                FOREIGN KEY (destination_id) REFERENCES xray_destinations(id) ON DELETE CASCADE
            """))
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.debug(f"Adding FK to ip_dest_stats: {e}")
        
        # Drop old column
        try:
            await conn.execute(text("""
                ALTER TABLE xray_ip_destination_stats DROP COLUMN IF EXISTS destination
            """))
        except Exception as e:
            logger.warning(f"Dropping destination column from ip_dest_stats: {e}")
    
    logger.info("Destination normalization migration completed")


async def _migrate_destination_host(conn):
    """Add host column to xray_destinations and populate from existing destinations.
    
    host = destination without :port suffix, used for fast GROUP BY.
    """
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'xray_destinations'
    """))
    columns = {row[0] for row in result.fetchall()}
    
    if not columns or "host" in columns:
        return  # Table doesn't exist or already has host column
    
    logger.info("Adding host column to xray_destinations...")
    
    try:
        await conn.execute(text('ALTER TABLE xray_destinations ADD COLUMN "host" VARCHAR(500)'))
    except Exception as e:
        if "already exists" not in str(e).lower():
            logger.warning(f"Adding host column: {e}")
        return
    
    # Populate host from destination (strip :port suffix)
    await conn.execute(text("""
        UPDATE xray_destinations 
        SET host = regexp_replace(destination, ':\\d+$', '')
        WHERE host IS NULL
    """))
    
    # Create index
    try:
        await conn.execute(text('CREATE INDEX IF NOT EXISTS "idx_xray_dest_host" ON xray_destinations ("host")'))
    except Exception:
        pass
    
    logger.info("Host column added and populated in xray_destinations")


async def _migrate_source_ip_normalization(conn):
    """Normalize source_ip into xray_source_ips table.
    
    Replaces VARCHAR(45) source_ip with INTEGER source_ip_id FK
    in xray_user_ip_stats and xray_ip_destination_stats.
    """
    # Check if migration is needed
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'xray_user_ip_stats'
    """))
    ip_stats_columns = {row[0] for row in result.fetchall()}
    
    if not ip_stats_columns:
        return  # Table doesn't exist yet
    
    has_source_ip = "source_ip" in ip_stats_columns
    has_source_ip_id = "source_ip_id" in ip_stats_columns
    
    # Already migrated
    if has_source_ip_id and not has_source_ip:
        return
    
    # Nothing to migrate
    if not has_source_ip and not has_source_ip_id:
        return
    
    logger.info("Starting source_ip normalization migration...")
    
    # Step 1: Ensure xray_source_ips table exists (create_all should handle it,
    # but populate from existing data)
    try:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS xray_source_ips (
                id SERIAL PRIMARY KEY,
                ip VARCHAR(45) NOT NULL UNIQUE,
                first_seen TIMESTAMP DEFAULT NOW()
            )
        """))
    except Exception as e:
        logger.warning(f"Creating xray_source_ips: {e}")
    
    # Step 2: Populate xray_source_ips from existing data
    if has_source_ip:
        logger.info("Populating xray_source_ips from existing data...")
        try:
            await conn.execute(text("""
                INSERT INTO xray_source_ips (ip, first_seen)
                SELECT DISTINCT source_ip, MIN(first_seen)
                FROM xray_user_ip_stats
                WHERE source_ip IS NOT NULL
                GROUP BY source_ip
                ON CONFLICT (ip) DO NOTHING
            """))
        except Exception as e:
            logger.warning(f"Populating from user_ip_stats: {e}")
        
        # Also from xray_ip_destination_stats
        result2 = await conn.execute(text("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'xray_ip_destination_stats'
        """))
        ip_dest_columns = {row[0] for row in result2.fetchall()}
        
        if ip_dest_columns and "source_ip" in ip_dest_columns:
            try:
                await conn.execute(text("""
                    INSERT INTO xray_source_ips (ip, first_seen)
                    SELECT DISTINCT source_ip, MIN(first_seen)
                    FROM xray_ip_destination_stats
                    WHERE source_ip IS NOT NULL
                    GROUP BY source_ip
                    ON CONFLICT (ip) DO NOTHING
                """))
            except Exception as e:
                logger.warning(f"Populating from ip_dest_stats: {e}")
    
    # Step 3: Add source_ip_id column if not exists
    if not has_source_ip_id:
        try:
            await conn.execute(text("""
                ALTER TABLE xray_user_ip_stats ADD COLUMN source_ip_id INTEGER
            """))
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.warning(f"Adding source_ip_id to user_ip_stats: {e}")
    
    # Step 4: Populate source_ip_id
    if has_source_ip:
        logger.info("Populating source_ip_id in xray_user_ip_stats...")
        try:
            await conn.execute(text("""
                UPDATE xray_user_ip_stats uis
                SET source_ip_id = sip.id
                FROM xray_source_ips sip
                WHERE uis.source_ip = sip.ip AND uis.source_ip_id IS NULL
            """))
        except Exception as e:
            logger.warning(f"Populating source_ip_id: {e}")
    
    # Delete orphaned rows
    try:
        await conn.execute(text("DELETE FROM xray_user_ip_stats WHERE source_ip_id IS NULL"))
    except Exception:
        pass
    
    # Make NOT NULL
    try:
        await conn.execute(text("ALTER TABLE xray_user_ip_stats ALTER COLUMN source_ip_id SET NOT NULL"))
    except Exception:
        pass
    
    # Drop old unique constraint
    try:
        await conn.execute(text("ALTER TABLE xray_user_ip_stats DROP CONSTRAINT IF EXISTS uq_user_ip_stats_unique"))
    except Exception:
        pass
    
    # Add FK
    try:
        await conn.execute(text("""
            ALTER TABLE xray_user_ip_stats 
            ADD CONSTRAINT fk_user_ip_stats_source_ip 
            FOREIGN KEY (source_ip_id) REFERENCES xray_source_ips(id) ON DELETE CASCADE
        """))
    except Exception as e:
        if "already exists" not in str(e).lower():
            logger.debug(f"Adding FK: {e}")
    
    # Drop old source_ip column
    if has_source_ip:
        try:
            await conn.execute(text("ALTER TABLE xray_user_ip_stats DROP COLUMN IF EXISTS source_ip"))
            logger.info("Dropped source_ip column from xray_user_ip_stats")
        except Exception as e:
            logger.warning(f"Dropping source_ip: {e}")
    
    # Create index on source_ip_id
    try:
        await conn.execute(text('CREATE INDEX IF NOT EXISTS "idx_user_ip_source_ip_id" ON xray_user_ip_stats ("source_ip_id")'))
    except Exception:
        pass
    
    # === Now migrate xray_ip_destination_stats ===
    result3 = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'xray_ip_destination_stats'
    """))
    ip_dest_cols = {row[0] for row in result3.fetchall()}
    
    if not ip_dest_cols:
        logger.info("Source IP normalization completed (no ip_dest_stats table)")
        return
    
    ip_dest_has_source_ip = "source_ip" in ip_dest_cols
    ip_dest_has_source_ip_id = "source_ip_id" in ip_dest_cols
    
    if ip_dest_has_source_ip:
        logger.info("Migrating xray_ip_destination_stats source_ip...")
        
        if not ip_dest_has_source_ip_id:
            try:
                await conn.execute(text("ALTER TABLE xray_ip_destination_stats ADD COLUMN source_ip_id INTEGER"))
            except Exception as e:
                if "already exists" not in str(e).lower():
                    logger.warning(f"Adding source_ip_id to ip_dest_stats: {e}")
        
        # Populate source_ip_id
        try:
            await conn.execute(text("""
                UPDATE xray_ip_destination_stats ids
                SET source_ip_id = sip.id
                FROM xray_source_ips sip
                WHERE ids.source_ip = sip.ip AND ids.source_ip_id IS NULL
            """))
        except Exception as e:
            logger.warning(f"Populating source_ip_id in ip_dest_stats: {e}")
        
        # Delete orphaned rows
        try:
            await conn.execute(text("DELETE FROM xray_ip_destination_stats WHERE source_ip_id IS NULL"))
        except Exception:
            pass
        
        # Make NOT NULL
        try:
            await conn.execute(text("ALTER TABLE xray_ip_destination_stats ALTER COLUMN source_ip_id SET NOT NULL"))
        except Exception:
            pass
        
        # Drop old constraint
        try:
            await conn.execute(text("ALTER TABLE xray_ip_destination_stats DROP CONSTRAINT IF EXISTS uq_ip_dest_stats_unique_v2"))
        except Exception:
            pass
        
        # Add FK
        try:
            await conn.execute(text("""
                ALTER TABLE xray_ip_destination_stats 
                ADD CONSTRAINT fk_ip_dest_stats_source_ip 
                FOREIGN KEY (source_ip_id) REFERENCES xray_source_ips(id) ON DELETE CASCADE
            """))
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.debug(f"Adding FK to ip_dest_stats: {e}")
        
        # Drop old source_ip column
        try:
            await conn.execute(text("ALTER TABLE xray_ip_destination_stats DROP COLUMN IF EXISTS source_ip"))
            logger.info("Dropped source_ip column from xray_ip_destination_stats")
        except Exception as e:
            logger.warning(f"Dropping source_ip from ip_dest_stats: {e}")
    
    # Drop first_seen from xray_ip_destination_stats (not used in queries)
    if "first_seen" in ip_dest_cols or "first_seen" in (ip_dest_cols - {"source_ip"}):
        result_check = await conn.execute(text("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'xray_ip_destination_stats' AND column_name = 'first_seen'
        """))
        if result_check.fetchone():
            try:
                await conn.execute(text("ALTER TABLE xray_ip_destination_stats DROP COLUMN IF EXISTS first_seen"))
                logger.info("Dropped first_seen from xray_ip_destination_stats")
            except Exception as e:
                logger.warning(f"Dropping first_seen: {e}")
    
    # Create index on (email, source_ip_id)
    try:
        await conn.execute(text('CREATE INDEX IF NOT EXISTS "idx_ip_dest_email_ip" ON xray_ip_destination_stats ("email", "source_ip_id")'))
    except Exception:
        pass
    
    logger.info("Source IP normalization completed")


async def _migrate_remove_surrogate_ids(conn):
    """Remove surrogate id columns from stats tables and convert to composite PKs.
    
    Saves ~4 bytes per row + eliminates one index per table.
    Safe because no other table references these ids via FK.
    """
    
    # === xray_visit_stats: id -> PK(server_id, destination_id, email) ===
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'xray_visit_stats'
    """))
    vs_columns = {row[0] for row in result.fetchall()}
    
    if vs_columns and "id" in vs_columns and "destination_id" in vs_columns:
        logger.info("Removing surrogate id from xray_visit_stats...")
        try:
            await conn.execute(text("ALTER TABLE xray_visit_stats DROP CONSTRAINT IF EXISTS xray_visit_stats_pkey"))
            await conn.execute(text("ALTER TABLE xray_visit_stats DROP CONSTRAINT IF EXISTS uq_xray_stats_unique_v2"))
            await conn.execute(text("ALTER TABLE xray_visit_stats DROP COLUMN id"))
            await conn.execute(text("ALTER TABLE xray_visit_stats ADD PRIMARY KEY (server_id, destination_id, email)"))
            logger.info("xray_visit_stats: converted to composite PK")
        except Exception as e:
            if "does not exist" in str(e).lower() or "already" in str(e).lower():
                pass
            else:
                logger.warning(f"Removing id from xray_visit_stats: {e}")
    
    # === xray_hourly_stats: id -> PK(server_id, hour) ===
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'xray_hourly_stats'
    """))
    hs_columns = {row[0] for row in result.fetchall()}
    
    if hs_columns and "id" in hs_columns:
        logger.info("Removing surrogate id from xray_hourly_stats...")
        try:
            await conn.execute(text("ALTER TABLE xray_hourly_stats DROP CONSTRAINT IF EXISTS xray_hourly_stats_pkey"))
            await conn.execute(text("ALTER TABLE xray_hourly_stats DROP CONSTRAINT IF EXISTS uq_xray_hourly_unique"))
            await conn.execute(text("DROP INDEX IF EXISTS idx_xray_hourly_server_hour"))
            await conn.execute(text("ALTER TABLE xray_hourly_stats DROP COLUMN id"))
            await conn.execute(text("ALTER TABLE xray_hourly_stats ADD PRIMARY KEY (server_id, hour)"))
            logger.info("xray_hourly_stats: converted to composite PK")
        except Exception as e:
            if "does not exist" in str(e).lower() or "already" in str(e).lower():
                pass
            else:
                logger.warning(f"Removing id from xray_hourly_stats: {e}")
    
    # === xray_user_ip_stats: id -> PK(server_id, email, source_ip_id) ===
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'xray_user_ip_stats'
    """))
    uis_columns = {row[0] for row in result.fetchall()}
    
    if uis_columns and "id" in uis_columns and "source_ip_id" in uis_columns:
        logger.info("Removing surrogate id from xray_user_ip_stats...")
        try:
            await conn.execute(text("ALTER TABLE xray_user_ip_stats DROP CONSTRAINT IF EXISTS xray_user_ip_stats_pkey"))
            await conn.execute(text("ALTER TABLE xray_user_ip_stats DROP COLUMN id"))
            await conn.execute(text("ALTER TABLE xray_user_ip_stats ADD PRIMARY KEY (server_id, email, source_ip_id)"))
            logger.info("xray_user_ip_stats: converted to composite PK")
        except Exception as e:
            if "does not exist" in str(e).lower() or "already" in str(e).lower():
                pass
            else:
                logger.warning(f"Removing id from xray_user_ip_stats: {e}")
    
    # === xray_ip_destination_stats: id -> PK(server_id, email, source_ip_id, destination_id) ===
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'xray_ip_destination_stats'
    """))
    ids_columns = {row[0] for row in result.fetchall()}
    
    if ids_columns and "id" in ids_columns and "source_ip_id" in ids_columns:
        logger.info("Removing surrogate id from xray_ip_destination_stats...")
        try:
            await conn.execute(text("ALTER TABLE xray_ip_destination_stats DROP CONSTRAINT IF EXISTS xray_ip_destination_stats_pkey"))
            await conn.execute(text("ALTER TABLE xray_ip_destination_stats DROP COLUMN id"))
            await conn.execute(text("ALTER TABLE xray_ip_destination_stats ADD PRIMARY KEY (server_id, email, source_ip_id, destination_id)"))
            logger.info("xray_ip_destination_stats: converted to composite PK")
        except Exception as e:
            if "does not exist" in str(e).lower() or "already" in str(e).lower():
                pass
            else:
                logger.warning(f"Removing id from xray_ip_destination_stats: {e}")


async def _migrate_ip_dest_to_host_schema(conn):
    """Migrate xray_ip_destination_stats from 4D (server_id, email, source_ip_id, destination_id)
    to 3D (email, source_ip_id, host) schema.
    
    Aggregates by host (strips port), removes server_id dimension.
    Idempotent: skips if already migrated (host column exists, destination_id gone).
    """
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'xray_ip_destination_stats'
    """))
    columns = {row[0] for row in result.fetchall()}
    
    if not columns:
        return  # Table doesn't exist yet, will be created fresh by create_all
    
    has_destination_id = "destination_id" in columns
    has_server_id = "server_id" in columns
    has_host = "host" in columns
    
    # Already migrated
    if has_host and not has_destination_id and not has_server_id:
        return
    
    # Fresh table with new schema (no old columns)
    if not has_destination_id and not has_server_id:
        return
    
    logger.info("Migrating xray_ip_destination_stats to host-based schema...")
    
    try:
        # Create temporary table with new schema
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS xray_ip_destination_stats_new (
                email INTEGER NOT NULL,
                source_ip_id INTEGER NOT NULL REFERENCES xray_source_ips(id) ON DELETE CASCADE,
                host VARCHAR(500) NOT NULL,
                connection_count BIGINT DEFAULT 0,
                last_seen TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (email, source_ip_id, host)
            )
        """))
        
        # Migrate data: aggregate by (email, source_ip_id, host), summing counts across servers
        if has_destination_id and has_server_id:
            await conn.execute(text("""
                INSERT INTO xray_ip_destination_stats_new (email, source_ip_id, host, connection_count, last_seen)
                SELECT 
                    ids.email,
                    ids.source_ip_id,
                    COALESCE(d.host, regexp_replace(d.destination, ':\\d+$', '')),
                    SUM(ids.connection_count),
                    MAX(ids.last_seen)
                FROM xray_ip_destination_stats ids
                JOIN xray_destinations d ON ids.destination_id = d.id
                GROUP BY ids.email, ids.source_ip_id, COALESCE(d.host, regexp_replace(d.destination, ':\\d+$', ''))
                ON CONFLICT (email, source_ip_id, host) DO UPDATE SET
                    connection_count = xray_ip_destination_stats_new.connection_count + EXCLUDED.connection_count,
                    last_seen = GREATEST(xray_ip_destination_stats_new.last_seen, EXCLUDED.last_seen)
            """))
            logger.info("Data migrated to new schema")
        
        # Drop old table and rename new one
        await conn.execute(text("DROP TABLE xray_ip_destination_stats"))
        await conn.execute(text("ALTER TABLE xray_ip_destination_stats_new RENAME TO xray_ip_destination_stats"))
        
        # Create indexes
        await conn.execute(text(
            'CREATE INDEX IF NOT EXISTS "idx_ip_dest_email_ip" ON xray_ip_destination_stats (email, source_ip_id)'
        ))
        await conn.execute(text(
            'CREATE INDEX IF NOT EXISTS "idx_ip_dest_host" ON xray_ip_destination_stats (host)'
        ))
        
        logger.info("xray_ip_destination_stats migration to host-based schema completed")
        
    except Exception as e:
        logger.error(f"Failed to migrate xray_ip_destination_stats: {e}")
        # Cleanup temp table on failure
        try:
            await conn.execute(text("DROP TABLE IF EXISTS xray_ip_destination_stats_new"))
        except Exception:
            pass


async def _seed_default_excluded_destinations():
    """Seed default excluded destinations if table is empty."""
    from app.models import RemnawaveExcludedDestination
    
    default_destinations = [
        ("www.google.com", "Google (test destination)"),
        ("1.1.1.1", "Cloudflare DNS"),
    ]
    
    async with async_session() as db:
        # Check if there are any excluded destinations
        result = await db.execute(text("SELECT COUNT(*) FROM remnawave_excluded_destinations"))
        count = result.scalar()
        
        if count == 0:
            logger.info("Seeding default excluded destinations...")
            for dest, desc in default_destinations:
                try:
                    await db.execute(text("""
                        INSERT INTO remnawave_excluded_destinations (destination, description)
                        VALUES (:dest, :desc)
                        ON CONFLICT (destination) DO NOTHING
                    """), {"dest": dest, "desc": desc})
                except Exception as e:
                    logger.debug(f"Could not seed excluded destination {dest}: {e}")
            
            await db.commit()
            logger.info(f"Seeded {len(default_destinations)} default excluded destinations")


async def _warmup_pool():
    """Pre-create database connections to avoid cold-start delays on first requests."""
    try:
        warmup_count = min(pool_size, 5)
        connections = []
        for _ in range(warmup_count):
            conn = await engine.connect()
            connections.append(conn)
        for conn in connections:
            await conn.close()
        logger.info(f"Database pool warmed up with {warmup_count} connections")
    except Exception as e:
        logger.warning(f"Pool warmup failed (non-critical): {e}")


async def _migrate_to_single_stats_table(conn):
    """Migrate from multi-table schema to single xray_stats table.
    
    Old tables: xray_visit_stats, xray_user_ip_stats, xray_ip_destination_stats,
                xray_destinations, xray_source_ips
    New table: xray_stats (email, source_ip, host) -> count, first_seen, last_seen
    
    Idempotent: skips if xray_stats already exists AND old tables are gone.
    """
    # Check if new table already exists
    new_exists = await conn.execute(text("""
        SELECT 1 FROM information_schema.tables WHERE table_name = 'xray_stats'
    """))
    has_new = new_exists.fetchone() is not None
    
    # Check if old tables exist
    old_tables = ['xray_visit_stats', 'xray_user_ip_stats', 'xray_ip_destination_stats',
                  'xray_destinations', 'xray_source_ips']
    existing_old = set()
    for table in old_tables:
        result = await conn.execute(text(f"""
            SELECT 1 FROM information_schema.tables WHERE table_name = '{table}'
        """))
        if result.fetchone():
            existing_old.add(table)
    
    if has_new and not existing_old:
        logger.info("xray_stats migration already completed (new table exists, old tables gone)")
        return
    
    if not has_new:
        logger.info("Creating xray_stats table...")
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS xray_stats (
                email INTEGER NOT NULL,
                source_ip VARCHAR(45) NOT NULL,
                host VARCHAR(500) NOT NULL,
                count BIGINT DEFAULT 0,
                first_seen TIMESTAMP DEFAULT NOW(),
                last_seen TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (email, source_ip, host)
            )
        """))
        await conn.execute(text('CREATE INDEX IF NOT EXISTS idx_xray_stats_host ON xray_stats (host)'))
        await conn.execute(text('CREATE INDEX IF NOT EXISTS idx_xray_stats_last_seen ON xray_stats (last_seen)'))
        await conn.execute(text('CREATE INDEX IF NOT EXISTS idx_xray_stats_email ON xray_stats (email)'))
        logger.info("xray_stats table created")
    
    # Migrate data from old tables (best effort)
    if 'xray_ip_destination_stats' in existing_old:
        # Check which schema: new (host column) or old (destination_id column)
        cols_result = await conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'xray_ip_destination_stats'
        """))
        ip_dest_cols = {row[0] for row in cols_result.fetchall()}
        
        try:
            if 'host' in ip_dest_cols and 'source_ip_id' in ip_dest_cols and 'xray_source_ips' in existing_old:
                # Previous migration state: host-based but still normalized source_ip
                logger.info("Migrating from ip_destination_stats (host + source_ip_id)...")
                await conn.execute(text("""
                    INSERT INTO xray_stats (email, source_ip, host, count, first_seen, last_seen)
                    SELECT ids.email, sip.ip, ids.host,
                           SUM(ids.connection_count), NOW(), MAX(ids.last_seen)
                    FROM xray_ip_destination_stats ids
                    JOIN xray_source_ips sip ON ids.source_ip_id = sip.id
                    GROUP BY ids.email, sip.ip, ids.host
                    ON CONFLICT (email, source_ip, host) DO UPDATE SET
                        count = xray_stats.count + EXCLUDED.count,
                        last_seen = GREATEST(xray_stats.last_seen, EXCLUDED.last_seen)
                """))
                logger.info("Data migrated from ip_destination_stats")
            elif 'destination_id' in ip_dest_cols and 'source_ip_id' in ip_dest_cols:
                # Original old schema: destination_id + source_ip_id + server_id
                if 'xray_destinations' in existing_old and 'xray_source_ips' in existing_old:
                    logger.info("Migrating from ip_destination_stats (old 4D schema)...")
                    await conn.execute(text("""
                        INSERT INTO xray_stats (email, source_ip, host, count, first_seen, last_seen)
                        SELECT ids.email, sip.ip,
                               COALESCE(d.host, regexp_replace(d.destination, ':\\d+$', '')),
                               SUM(ids.connection_count), NOW(), MAX(ids.last_seen)
                        FROM xray_ip_destination_stats ids
                        JOIN xray_source_ips sip ON ids.source_ip_id = sip.id
                        JOIN xray_destinations d ON ids.destination_id = d.id
                        GROUP BY ids.email, sip.ip, COALESCE(d.host, regexp_replace(d.destination, ':\\d+$', ''))
                        ON CONFLICT (email, source_ip, host) DO UPDATE SET
                            count = xray_stats.count + EXCLUDED.count,
                            last_seen = GREATEST(xray_stats.last_seen, EXCLUDED.last_seen)
                    """))
                    logger.info("Data migrated from ip_destination_stats (old schema)")
        except Exception as e:
            logger.warning(f"Could not migrate ip_destination_stats data: {e}")
    
    # If ip_dest was empty/missing, try visit_stats as fallback
    stats_count = await conn.execute(text("SELECT COUNT(*) FROM xray_stats"))
    if (stats_count.scalar() or 0) == 0 and 'xray_visit_stats' in existing_old:
        try:
            if 'xray_destinations' in existing_old:
                logger.info("Migrating from xray_visit_stats as fallback...")
                await conn.execute(text("""
                    INSERT INTO xray_stats (email, source_ip, host, count, first_seen, last_seen)
                    SELECT vs.email, '0.0.0.0',
                           COALESCE(d.host, regexp_replace(d.destination, ':\\d+$', '')),
                           SUM(vs.visit_count), MIN(vs.first_seen), MAX(vs.last_seen)
                    FROM xray_visit_stats vs
                    JOIN xray_destinations d ON vs.destination_id = d.id
                    GROUP BY vs.email, COALESCE(d.host, regexp_replace(d.destination, ':\\d+$', ''))
                    ON CONFLICT (email, source_ip, host) DO UPDATE SET
                        count = xray_stats.count + EXCLUDED.count,
                        last_seen = GREATEST(xray_stats.last_seen, EXCLUDED.last_seen)
                """))
                logger.info("Data migrated from xray_visit_stats")
        except Exception as e:
            logger.warning(f"Could not migrate visit_stats data: {e}")
    
    # Drop old tables
    for table in old_tables:
        if table in existing_old:
            try:
                await conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
                logger.info(f"Dropped old table: {table}")
            except Exception as e:
                logger.warning(f"Could not drop {table}: {e}")
    
    logger.info("Migration to single xray_stats table completed")


async def _migrate_folder_columns(conn):
    for table in ("billing_servers", "servers"):
        result = await conn.execute(text(f"""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = '{table}'
        """))
        columns = {row[0] for row in result.fetchall()}
        if columns and "folder" not in columns:
            try:
                await conn.execute(text(f'ALTER TABLE {table} ADD COLUMN "folder" VARCHAR(200)'))
                logger.info(f"Added column: {table}.folder")
            except Exception as e:
                if "already exists" not in str(e).lower():
                    logger.warning(f"Could not add {table}.folder: {e}")


async def init_db():
    """Initialize database: create tables, run migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await run_migrations(conn)
        await _migrate_folder_columns(conn)
    
    try:
        await _seed_default_excluded_destinations()
    except Exception as e:
        logger.debug(f"Could not seed excluded destinations: {e}")
    
    await _warmup_pool()


async def get_db():
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
