"""
Extension Service Module
Contains models, service and business logic
"""

import os
import json
import ipaddress
import asyncio
import logging
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ip_search import received_stats

RECEIVED_IPS = received_stats.ReceivedIpsStore("ext_received_ips")

logger = logging.getLogger(__name__)

# Available subnets
AVAILABLE_SUBNETS = {
    1: "89.208.216.0/24",
    2: "212.111.84.0/22",
    3: "90.156.212.0/22",
    4: "90.156.216.0/22",
    5: "91.219.226.0/23",
    6: "83.166.232.0/21",
    7: "83.166.248.0/21",
    8: "217.16.16.0/21",
    9: "90.156.150.0/23",
    10: "217.16.24.0/22",
    11: "85.192.32.0/22",
    12: "37.139.40.0/22",
    13: "213.219.212.0/22",
    14: "146.185.208.0/22",
    15: "89.208.220.0/22",
    16: "89.208.208.0/22",
    17: "109.120.188.0/22",
    18: "185.86.144.0/22",
    19: "89.208.228.0/22",
    20: "109.120.180.0/22",
    21: "185.241.192.0/22",
    22: "94.139.244.0/22",
    23: "89.208.196.0/22",
    24: "5.188.140.0/22",
    25: "212.233.96.0/22",
    26: "95.163.212.0/22",
    27: "212.233.120.0/22",
    28: "212.233.72.0/21",
    29: "89.208.84.0/22",
    30: "185.130.112.0/22",
    31: "87.239.104.0/21",
    32: "146.185.240.0/22",
    33: "95.163.180.0/23",
    34: "84.23.52.0/22",
    35: "95.163.248.0/22",
    36: "79.137.174.0/23",
    37: "37.139.32.0/22",
    38: "212.233.88.0/21",
    39: "95.163.208.0/22",
    40: "95.163.182.0/23",
    41: "217.16.28.0/22",
    44: "161.104.104.0/22",
    45: "161.104.108.0/23",
    46: "138.16.248.0/23",
    47: "138.16.240.0/25",
}

SUBNET_NAME_OVERRIDES: Dict[int, str] = {
    1: "ext-sub",
    44: "ext-subnet44",
    45: "ext-subnet45",
}


def get_subnet_name(key: int) -> str:
    return SUBNET_NAME_OVERRIDES.get(key, f"ext-sub{key}")


# per_account — аккаунту достаётся одна подсеть из выбранных; rotate — все, по кругу
DEFAULT_SUBNET_MODE = "per_account"


TABLES_SQL = """
CREATE TABLE IF NOT EXISTS ext_accounts (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE,
    password VARCHAR(500),
    proxy VARCHAR(500),
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ext_projects (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL,
    project_id VARCHAR(100) NOT NULL,
    project_name VARCHAR(255) NOT NULL,
    disabled BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES ext_accounts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ext_caught_ips (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL,
    project_id INTEGER,
    ip_address VARCHAR(50) NOT NULL,
    subnet_name VARCHAR(50),
    caught_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES ext_accounts(id) ON DELETE CASCADE,
    FOREIGN KEY (project_id) REFERENCES ext_projects(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS ext_settings (
    id INTEGER PRIMARY KEY DEFAULT 1,
    delay_min INTEGER DEFAULT 60,
    delay_max INTEGER DEFAULT 90,
    max_ip_per_project INTEGER DEFAULT 1,
    stop_on_catch BOOLEAN DEFAULT TRUE,
    telegram_bot_token VARCHAR(500),
    telegram_chat_id VARCHAR(100),
    target_subnets TEXT DEFAULT '[17, 19, 24, 35]',
    proxy_required BOOLEAN DEFAULT TRUE,
    proxy_check_interval INTEGER DEFAULT 50,
    log_level VARCHAR(10) DEFAULT 'info',
    start_delay_max INTEGER DEFAULT 60,
    subnet_mode VARCHAR(20) DEFAULT 'per_account',
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ext_custom_subnets (
    id INTEGER PRIMARY KEY,
    cidr VARCHAR(50) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ext_precise_cidrs (
    id SERIAL PRIMARY KEY,
    cidr VARCHAR(50) NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ext_projects_account ON ext_projects(account_id);
CREATE INDEX IF NOT EXISTS idx_ext_caught_ips_account ON ext_caught_ips(account_id);
CREATE INDEX IF NOT EXISTS idx_ext_caught_ips_project ON ext_caught_ips(project_id);
""" + received_stats.received_ips_ddl("ext_received_ips", "ext_accounts")


async def _get_pg_columns(conn, table: str) -> set:
    """Get column names from PostgreSQL table"""
    result = await conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :table AND table_schema = 'public'"
    ), {"table": table})
    return {row[0] for row in result.fetchall()}


async def _table_exists(conn, table: str) -> bool:
    """Check if table exists in PostgreSQL"""
    result = await conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = :table AND table_schema = 'public')"
    ), {"table": table})
    return result.scalar()


async def init_tables(engine):
    """Create tables if not exist (PostgreSQL)"""
    ExtService._engine = engine  # Store for watchdog DB access
    async with engine.begin() as conn:
        for statement in TABLES_SQL.strip().split(';'):
            statement = statement.strip()
            if statement:
                try:
                    await conn.execute(text(statement))
                except Exception as e:
                    # Ignore errors for existing objects
                    if "already exists" not in str(e).lower():
                        logger.debug(f"Table creation note: {e}")
        
        # Migration: add missing columns to ext_settings
        if await _table_exists(conn, 'ext_settings'):
            columns = await _get_pg_columns(conn, 'ext_settings')
            
            migrations = [
                ("log_level", "VARCHAR(10) DEFAULT 'info'"),
                ("start_delay_max", "INTEGER DEFAULT 60"),
                ("subnet_mode", "VARCHAR(20) DEFAULT 'per_account'"),
            ]
            for col_name, col_type in migrations:
                if col_name not in columns:
                    try:
                        await conn.execute(text(f'ALTER TABLE ext_settings ADD COLUMN {col_name} {col_type}'))
                        logger.info(f"Added column: ext_settings.{col_name}")
                    except Exception:
                        pass
        
        # Migration: add missing columns to ext_accounts
        if await _table_exists(conn, 'ext_accounts'):
            columns = await _get_pg_columns(conn, 'ext_accounts')
            
            migrations = [
                ("enabled", "BOOLEAN DEFAULT TRUE"),
                ("password", "VARCHAR(500)"),
                ("proxy", "VARCHAR(500)"),
            ]
            for col_name, col_type in migrations:
                if col_name not in columns:
                    try:
                        await conn.execute(text(f'ALTER TABLE ext_accounts ADD COLUMN {col_name} {col_type}'))
                        logger.info(f"Added column: ext_accounts.{col_name}")
                    except Exception:
                        pass
        
        # Migration: migrate password/proxy from old ext_projects if columns exist
        if await _table_exists(conn, 'ext_projects'):
            proj_columns = await _get_pg_columns(conn, 'ext_projects')
            
            if 'password' in proj_columns and 'proxy' in proj_columns:
                try:
                    result = await conn.execute(text(
                        """SELECT a.id, p.password, p.proxy FROM ext_accounts a
                           LEFT JOIN ext_projects p ON p.account_id = a.id
                           WHERE (a.password IS NULL OR a.password = '') AND p.password IS NOT NULL AND p.password != ''
                           GROUP BY a.id, p.password, p.proxy"""
                    ))
                    rows = result.fetchall()
                    for row in rows:
                        if row[1]:
                            await conn.execute(text(
                                "UPDATE ext_accounts SET password = :pwd, proxy = :prx WHERE id = :id"
                            ), {"pwd": row[1], "prx": row[2], "id": row[0]})
                    if rows:
                        logger.info(f"Migrated password/proxy for {len(rows)} accounts from old schema")
                except Exception as e:
                    logger.debug(f"Migration password/proxy check: {e}")
            
            # Remove old columns from ext_projects if they exist (PostgreSQL supports DROP COLUMN)
            if 'username' in proj_columns or 'password' in proj_columns or 'proxy' in proj_columns:
                for old_col in ['username', 'password', 'proxy']:
                    if old_col in proj_columns:
                        try:
                            await conn.execute(text(f'ALTER TABLE ext_projects DROP COLUMN {old_col}'))
                            logger.info(f"Removed old column: ext_projects.{old_col}")
                        except Exception:
                            pass
    
    logger.info("Extension tables initialized")
    
    # Restore logs from persistent file (survives OOM/container restart)
    ExtService._load_logs_from_file()


class AccountWorkerInfo:
    """Info about a running per-account worker subprocess"""
    __slots__ = (
        'process', 'log_task', 'account_id', 'email', 'status',
        'error_type', 'error_message', 'started_at', 'attempts', 'caught', 'released', 'logs',
        'config_json', 'finish_reason',
    )

    def __init__(self, process: subprocess.Popen, log_task: asyncio.Task,
                 account_id: int, email: str, config_json: str = ""):
        self.process = process
        self.log_task = log_task
        self.account_id = account_id
        self.email = email
        self.status: str = "running"          # running, stopping, stopped, error, proxy_waiting
        self.error_type: Optional[str] = None  # ACCESS_DENIED, QUOTA_EXCEEDED, PROXY_DEAD, etc.
        self.error_message: Optional[str] = None
        self.started_at: datetime = datetime.now()
        self.attempts: int = 0
        self.caught: int = 0
        self.released: int = 0  # IP вне узкого фильтра, освобождённые обратно в пул
        self.logs: List[str] = []
        self.config_json: str = config_json
        self.finish_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        is_alive = self.process and self.process.poll() is None
        return {
            "account_id": self.account_id,
            "email": self.email,
            "status": self.status if is_alive else "stopped",
            "error_type": self.error_type,
            "error_message": self.error_message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "attempts": self.attempts,
            "caught": self.caught,
            "released": self.released,
            "pid": self.process.pid if is_alive else None,
        }


class ExtService:
    """Service for managing the extension"""
    
    # Per-account worker management
    _account_workers: Dict[int, AccountWorkerInfo] = {}
    _logs: List[str] = []
    _max_logs: int = 1000
    _start_lock: asyncio.Lock = None  # Initialized lazily
    _account_errors: List[Dict[str, Any]] = []  # In-memory error list
    _max_errors: int = 200
    _log_seq: int = 0  # Monotonically increasing log counter for SSE tracking
    
    # Persistent file logging (survives OOM/restart)
    LOG_FILE = Path("/app/data/ext_worker.log")
    _LOG_MAX_SIZE: int = 5 * 1024 * 1024   # 5MB — rotate after this
    _LOG_ROTATE_KEEP: int = 3000            # Keep last 3000 lines on rotation
    
    # Watchdog: auto-restart crashed workers
    _desired_running: set = set()       # account IDs that should be running
    _watchdog_task: Optional[asyncio.Task] = None
    _restart_counts: Dict[int, int] = {}
    _MAX_RESTARTS: int = 10
    _WATCHDOG_INTERVAL: int = 30
    _HEALTHY_UPTIME: int = 300          # 5 min to consider process "healthy" and reset counter
    _engine = None                       # DB engine for watchdog config refresh
    _log_executor: Optional[ThreadPoolExecutor] = None
    
    # Legacy single-process support (kept for backward compat)
    _process: Optional[subprocess.Popen] = None
    _log_task: Optional[asyncio.Task] = None
    _status: str = "stopped"
    
    SCRIPT_DIR = Path("/app/ext")
    SCRIPT_PATH = SCRIPT_DIR / "worker.py"
    CONFIG_PATH = SCRIPT_DIR / "profiles.conf"
    
    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        if cls._start_lock is None:
            cls._start_lock = asyncio.Lock()
        return cls._start_lock
    
    @classmethod
    def _get_log_executor(cls) -> ThreadPoolExecutor:
        if cls._log_executor is None or cls._log_executor._shutdown:
            cls._log_executor = ThreadPoolExecutor(max_workers=500, thread_name_prefix="log-reader")
        return cls._log_executor
    
    # ─────────────────────────────────────────────────────────────────
    # ACCOUNTS
    # ─────────────────────────────────────────────────────────────────
    
    @staticmethod
    async def get_accounts(db: AsyncSession) -> List[Dict[str, Any]]:
        # Get accounts with all columns (new schema)
        result = await db.execute(text(
            "SELECT id, email, password, proxy, enabled, created_at FROM ext_accounts ORDER BY id"
        ))
        accounts = result.fetchall()
        
        # Check if ext_caught_ips table exists
        try:
            table_check = await db.execute(text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ext_caught_ips' AND table_schema = 'public')"
            ))
            has_caught_ips_table = table_check.scalar()
        except:
            has_caught_ips_table = False
        
        output = []
        for acc in accounts:
            proj_result = await db.execute(text(
                "SELECT COUNT(*) FROM ext_projects WHERE account_id = :aid"
            ), {"aid": acc[0]})
            count = proj_result.scalar()
            
            ip_count = 0
            if has_caught_ips_table:
                try:
                    ip_result = await db.execute(text(
                        "SELECT COUNT(*) FROM ext_caught_ips WHERE account_id = :aid"
                    ), {"aid": acc[0]})
                    ip_count = ip_result.scalar() or 0
                except:
                    pass
            
            output.append({
                "id": acc[0],
                "email": acc[1],
                "password": acc[2] or "",
                "proxy": acc[3] or "",
                "enabled": bool(acc[4]) if acc[4] is not None else True,
                "created_at": acc[5],
                "project_count": count or 0,
                "caught_ip_count": ip_count
            })
        return output
    
    @staticmethod
    async def create_account(db: AsyncSession, email: str, password: str = "", proxy: str = "") -> Dict[str, Any]:
        await db.execute(text(
            "INSERT INTO ext_accounts (email, password, proxy, enabled) VALUES (:email, :password, :proxy, TRUE)"
        ), {"email": email, "password": password, "proxy": proxy or None})
        await db.commit()
        
        result = await db.execute(text(
            "SELECT id, email, password, proxy, enabled, created_at FROM ext_accounts WHERE email = :email"
        ), {"email": email})
        acc = result.fetchone()
        
        return {
            "id": acc[0],
            "email": acc[1],
            "password": acc[2] or "",
            "proxy": acc[3] or "",
            "enabled": bool(acc[4]) if acc[4] is not None else True,
            "created_at": acc[5],
            "project_count": 0,
            "caught_ip_count": 0
        }
    
    @staticmethod
    async def update_account(db: AsyncSession, account_id: int, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Update account fields"""
        updates = []
        params = {"id": account_id}
        
        if "enabled" in data:
            updates.append("enabled = :enabled")
            params["enabled"] = bool(data["enabled"])
        if "password" in data:
            updates.append("password = :password")
            params["password"] = data["password"]
        if "proxy" in data:
            updates.append("proxy = :proxy")
            params["proxy"] = data["proxy"] or None
        
        if updates:
            await db.execute(text(
                f"UPDATE ext_accounts SET {', '.join(updates)} WHERE id = :id"
            ), params)
            await db.commit()
        
        result = await db.execute(text(
            "SELECT id, email, password, proxy, enabled, created_at FROM ext_accounts WHERE id = :id"
        ), {"id": account_id})
        acc = result.fetchone()
        
        if not acc:
            return None
        
        proj_result = await db.execute(text(
            "SELECT COUNT(*) FROM ext_projects WHERE account_id = :aid"
        ), {"aid": acc[0]})
        count = proj_result.scalar()
        
        ip_result = await db.execute(text(
            "SELECT COUNT(*) FROM ext_caught_ips WHERE account_id = :aid"
        ), {"aid": acc[0]})
        ip_count = ip_result.scalar() or 0
        
        return {
            "id": acc[0],
            "email": acc[1],
            "password": acc[2] or "",
            "proxy": acc[3] or "",
            "enabled": bool(acc[4]) if acc[4] is not None else True,
            "created_at": acc[5],
            "project_count": count or 0,
            "caught_ip_count": ip_count
        }
    
    @staticmethod
    async def delete_account(db: AsyncSession, account_id: int) -> bool:
        await db.execute(text(
            "DELETE FROM ext_projects WHERE account_id = :aid"
        ), {"aid": account_id})
        result = await db.execute(text(
            "DELETE FROM ext_accounts WHERE id = :aid"
        ), {"aid": account_id})
        await db.commit()
        return result.rowcount > 0
    
    # ─────────────────────────────────────────────────────────────────
    # PROJECTS
    # ─────────────────────────────────────────────────────────────────
    
    @staticmethod
    async def get_projects(db: AsyncSession, account_id: int) -> List[Dict[str, Any]]:
        result = await db.execute(text(
            """SELECT id, account_id, project_id, project_name, disabled, created_at 
               FROM ext_projects WHERE account_id = :aid ORDER BY id"""
        ), {"aid": account_id})
        projects = result.fetchall()
        
        # Check if ext_caught_ips table exists
        try:
            table_check = await db.execute(text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ext_caught_ips' AND table_schema = 'public')"
            ))
            has_caught_ips_table = table_check.scalar()
        except:
            has_caught_ips_table = False
        
        output = []
        for p in projects:
            ip_count = 0
            if has_caught_ips_table:
                try:
                    ip_result = await db.execute(text(
                        "SELECT COUNT(*) FROM ext_caught_ips WHERE project_id = :pid"
                    ), {"pid": p[0]})
                    ip_count = ip_result.scalar() or 0
                except:
                    pass
            
            output.append({
                "id": p[0],
                "account_id": p[1],
                "project_id": p[2],
                "project_name": p[3],
                "disabled": bool(p[4]),
                "created_at": p[5],
                "caught_ip_count": ip_count
            })
        return output
    
    @staticmethod
    async def get_all_projects(db: AsyncSession) -> List[Dict[str, Any]]:
        result = await db.execute(text(
            """SELECT id, account_id, project_id, project_name, disabled, created_at 
               FROM ext_projects ORDER BY account_id, id"""
        ))
        projects = result.fetchall()
        
        return [{
            "id": p[0],
            "account_id": p[1],
            "project_id": p[2],
            "project_name": p[3],
            "disabled": bool(p[4]),
            "created_at": p[5],
        } for p in projects]
    
    @staticmethod
    async def create_project(
        db: AsyncSession,
        account_id: int,
        project_id: str,
        project_name: str
    ) -> Dict[str, Any]:
        await db.execute(text(
            """INSERT INTO ext_projects (account_id, project_id, project_name)
               VALUES (:account_id, :project_id, :project_name)"""
        ), {
            "account_id": account_id,
            "project_id": project_id,
            "project_name": project_name
        })
        await db.commit()
        
        # Get the created project by unique fields
        result = await db.execute(text(
            """SELECT id, account_id, project_id, project_name, disabled, created_at
               FROM ext_projects 
               WHERE account_id = :account_id AND project_id = :project_id
               ORDER BY id DESC LIMIT 1"""
        ), {"account_id": account_id, "project_id": project_id})
        p = result.fetchone()
        
        return {
            "id": p[0],
            "account_id": p[1],
            "project_id": p[2],
            "project_name": p[3],
            "disabled": bool(p[4]),
            "created_at": p[5],
            "caught_ip_count": 0
        }
    
    @staticmethod
    async def update_project(db: AsyncSession, project_id: int, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        allowed_fields = ["project_id", "project_name", "disabled"]
        updates = []
        params = {"id": project_id}
        
        for field in allowed_fields:
            if field in data:
                if field == "project_id":
                    updates.append("project_id = :proj_id")
                    params["proj_id"] = data[field]
                elif field == "disabled":
                    updates.append("disabled = :disabled")
                    params["disabled"] = bool(data[field])
                else:
                    updates.append(f"{field} = :{field}")
                    params[field] = data[field]
        
        if not updates:
            return None
        
        await db.execute(text(
            f"UPDATE ext_projects SET {', '.join(updates)} WHERE id = :id"
        ), params)
        await db.commit()
        
        result = await db.execute(text(
            """SELECT id, account_id, project_id, project_name, disabled, created_at
               FROM ext_projects WHERE id = :id"""
        ), {"id": project_id})
        p = result.fetchone()
        
        if not p:
            return None
        
        ip_result = await db.execute(text(
            "SELECT COUNT(*) FROM ext_caught_ips WHERE project_id = :pid"
        ), {"pid": p[0]})
        ip_count = ip_result.scalar() or 0
        
        return {
            "id": p[0],
            "account_id": p[1],
            "project_id": p[2],
            "project_name": p[3],
            "disabled": bool(p[4]),
            "created_at": p[5],
            "caught_ip_count": ip_count
        }
    
    @staticmethod
    async def delete_project(db: AsyncSession, project_id: int) -> bool:
        result = await db.execute(text(
            "DELETE FROM ext_projects WHERE id = :id"
        ), {"id": project_id})
        await db.commit()
        return result.rowcount > 0
    
    # ─────────────────────────────────────────────────────────────────
    # CAUGHT IPS
    # ─────────────────────────────────────────────────────────────────
    
    @staticmethod
    async def get_caught_ips(db: AsyncSession, account_id: int = None, project_id: int = None) -> List[Dict[str, Any]]:
        """Get caught IPs with optional filtering"""
        query = """SELECT c.id, c.account_id, c.project_id, c.ip_address, c.subnet_name, c.caught_at,
                          a.email, p.project_name
                   FROM ext_caught_ips c
                   LEFT JOIN ext_accounts a ON c.account_id = a.id
                   LEFT JOIN ext_projects p ON c.project_id = p.id"""
        
        params = {}
        conditions = []
        
        if account_id is not None:
            conditions.append("c.account_id = :account_id")
            params["account_id"] = account_id
        if project_id is not None:
            conditions.append("c.project_id = :project_id")
            params["project_id"] = project_id
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY c.caught_at DESC"
        
        result = await db.execute(text(query), params)
        rows = result.fetchall()
        
        return [{
            "id": r[0],
            "account_id": r[1],
            "project_id": r[2],
            "ip_address": r[3],
            "subnet_name": r[4],
            "caught_at": r[5],
            "account_email": r[6],
            "project_name": r[7]
        } for r in rows]
    
    @staticmethod
    async def save_caught_ip(db: AsyncSession, account_id: int, project_id: int, ip_address: str, subnet_name: str) -> Dict[str, Any]:
        """Save a caught IP"""
        await db.execute(text(
            """INSERT INTO ext_caught_ips (account_id, project_id, ip_address, subnet_name)
               VALUES (:account_id, :project_id, :ip_address, :subnet_name)"""
        ), {
            "account_id": account_id,
            "project_id": project_id,
            "ip_address": ip_address,
            "subnet_name": subnet_name
        })
        await db.commit()
        
        result = await db.execute(text(
            "SELECT id, caught_at FROM ext_caught_ips WHERE ip_address = :ip ORDER BY id DESC LIMIT 1"
        ), {"ip": ip_address})
        row = result.fetchone()
        
        return {
            "id": row[0],
            "account_id": account_id,
            "project_id": project_id,
            "ip_address": ip_address,
            "subnet_name": subnet_name,
            "caught_at": row[1]
        }
    
    @staticmethod
    async def delete_caught_ip(db: AsyncSession, ip_id: int) -> bool:
        """Delete a single caught IP"""
        result = await db.execute(text(
            "DELETE FROM ext_caught_ips WHERE id = :id"
        ), {"id": ip_id})
        await db.commit()
        return result.rowcount > 0
    
    @staticmethod
    async def clear_caught_ips(db: AsyncSession, account_id: int = None, project_id: int = None) -> int:
        """Clear caught IPs with optional filtering"""
        if project_id is not None:
            result = await db.execute(text(
                "DELETE FROM ext_caught_ips WHERE project_id = :pid"
            ), {"pid": project_id})
        elif account_id is not None:
            result = await db.execute(text(
                "DELETE FROM ext_caught_ips WHERE account_id = :aid"
            ), {"aid": account_id})
        else:
            result = await db.execute(text("DELETE FROM ext_caught_ips"))
        
        await db.commit()
        return result.rowcount
    
    # ─────────────────────────────────────────────────────────────────
    # RECEIVED IPS STATS
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    async def save_received_ips(db: AsyncSession, account_id: int, items: List[Dict[str, Any]]) -> int:
        return await RECEIVED_IPS.save(db, account_id, items)

    @staticmethod
    async def get_received_stats(db: AsyncSession, account_id: Optional[int] = None) -> Dict[str, Any]:
        # Попадание у VK — узкий фильтр precise CIDR: без него воркер оставляет любой выданный адрес
        precise_cidrs = [item["cidr"] for item in await get_precise_cidrs(db)]
        return await RECEIVED_IPS.stats(db, account_id, precise_cidrs)

    @staticmethod
    async def get_received_subnet_ips(
        db: AsyncSession, subnet: str, account_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        return await RECEIVED_IPS.subnet_ips(db, subnet, account_id)

    @staticmethod
    async def clear_received_ips(db: AsyncSession, account_id: Optional[int] = None) -> int:
        return await RECEIVED_IPS.clear(db, account_id)

    # ─────────────────────────────────────────────────────────────────
    # SETTINGS
    # ─────────────────────────────────────────────────────────────────
    
    @staticmethod
    async def get_settings(db: AsyncSession) -> Dict[str, Any]:
        # Try with all columns first, fallback if some don't exist
        try:
            result = await db.execute(text(
                """SELECT delay_min, delay_max, max_ip_per_project, stop_on_catch,
                          telegram_bot_token, telegram_chat_id, target_subnets,
                          proxy_required, proxy_check_interval, log_level, start_delay_max,
                          subnet_mode
                   FROM ext_settings WHERE id = 1"""
            ))
            row = result.fetchone()
            has_all_columns = True
        except Exception:
            try:
                result = await db.execute(text(
                    """SELECT delay_min, delay_max, max_ip_per_project, stop_on_catch,
                              telegram_bot_token, telegram_chat_id, target_subnets,
                              proxy_required, proxy_check_interval, log_level
                       FROM ext_settings WHERE id = 1"""
                ))
                row = result.fetchone()
                has_all_columns = False
            except Exception:
                result = await db.execute(text(
                    """SELECT delay_min, delay_max, max_ip_per_project, stop_on_catch,
                              telegram_bot_token, telegram_chat_id, target_subnets,
                              proxy_required, proxy_check_interval
                       FROM ext_settings WHERE id = 1"""
                ))
                row = result.fetchone()
                has_all_columns = False
        
        if not row:
            await db.execute(text("INSERT INTO ext_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING"))
            await db.commit()
            return await ExtService.get_settings(db)
        
        settings = {
            "delay_min": row[0] if row[0] is not None else 60,
            "delay_max": row[1] if row[1] is not None else 90,
            "max_ip_per_project": row[2] if row[2] is not None else 1,
            "stop_on_catch": bool(row[3]) if row[3] is not None else True,
            "telegram_bot_token": row[4] or "",
            "telegram_chat_id": row[5] or "",
            "target_subnets": json.loads(row[6]) if row[6] else [17, 19, 24, 35],
            "proxy_required": bool(row[7]) if row[7] is not None else True,
            "proxy_check_interval": row[8] if row[8] is not None else 50,
            "log_level": (row[9] if row[9] else "info") if len(row) > 9 else "info",
            "start_delay_max": row[10] if (has_all_columns and len(row) > 10 and row[10] is not None) else 60,
            "subnet_mode": row[11] if (has_all_columns and len(row) > 11 and row[11]) else DEFAULT_SUBNET_MODE,
            "available_subnets": await get_all_subnets(db),
            "precise_cidrs": await get_precise_cidr_list(db),
        }
        return settings
    
    @staticmethod
    async def update_settings(db: AsyncSession, data: Dict[str, Any]) -> Dict[str, Any]:
        await db.execute(text("INSERT INTO ext_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING"))
        
        updates = []
        params = {}
        
        field_map = {
            "delay_min": "delay_min",
            "delay_max": "delay_max",
            "max_ip_per_project": "max_ip_per_project",
            "stop_on_catch": "stop_on_catch",
            "telegram_bot_token": "telegram_bot_token",
            "telegram_chat_id": "telegram_chat_id",
            "proxy_required": "proxy_required",
            "proxy_check_interval": "proxy_check_interval",
            "log_level": "log_level",
            "start_delay_max": "start_delay_max",
            "subnet_mode": "subnet_mode",
        }
        
        # Boolean fields need explicit conversion for PostgreSQL
        boolean_fields = {"stop_on_catch", "proxy_required"}
        
        for key, col in field_map.items():
            if key in data:
                updates.append(f"{col} = :{key}")
                params[key] = bool(data[key]) if key in boolean_fields else data[key]
        
        if "target_subnets" in data:
            updates.append("target_subnets = :subnets")
            params["subnets"] = json.dumps(data["target_subnets"])
        
        updates.append("updated_at = CURRENT_TIMESTAMP")
        
        if updates:
            await db.execute(text(
                f"UPDATE ext_settings SET {', '.join(updates)} WHERE id = 1"
            ), params)
            await db.commit()
        
        return await ExtService.get_settings(db)
    
    # ─────────────────────────────────────────────────────────────────
    # WORKER CONTROL (Per-Account Subprocess Management)
    # ─────────────────────────────────────────────────────────────────
    
    @classmethod
    async def _get_account_config_json(cls, db: AsyncSession, account_id: int) -> Optional[str]:
        """Build JSON config for a single account's worker subprocess"""
        # Get account
        acc_result = await db.execute(text(
            "SELECT id, email, password, proxy FROM ext_accounts WHERE id = :id AND enabled = TRUE"
        ), {"id": account_id})
        acc = acc_result.fetchone()
        if not acc:
            return None
        
        # Get projects
        proj_result = await db.execute(text(
            """SELECT id, project_id, project_name FROM ext_projects
               WHERE account_id = :aid AND disabled = FALSE ORDER BY id"""
        ), {"aid": account_id})
        projects = proj_result.fetchall()
        if not projects:
            return None
        
        # Get settings
        settings = await cls.get_settings(db)
        
        target_subnets = settings.get("target_subnets", [17, 19, 24, 35])
        all_subnets = settings.get("available_subnets", AVAILABLE_SUBNETS)
        subnets_dict = {str(k): all_subnets[k] for k in target_subnets if k in all_subnets}
        precise_cidrs = settings.get("precise_cidrs", [])

        config_data = {
            "account_id": acc[0],
            "email": acc[1],
            "password": acc[2] or "",
            "proxy": acc[3] or None,
            "projects": [
                {
                    "project_id": p[1],
                    "project_name": p[2],
                    "project_db_id": p[0],
                }
                for p in projects
            ],
            "target_subnets": subnets_dict,
            "config": {
                "delay_min": settings.get("delay_min", 60),
                "delay_max": settings.get("delay_max", 90),
                "max_ip_per_project": settings.get("max_ip_per_project", 1),
                "stop_on_catch": settings.get("stop_on_catch", True),
                "proxy_required": settings.get("proxy_required", True),
                "proxy_check_interval": settings.get("proxy_check_interval", 50),
                "log_level": settings.get("log_level", "info"),
                "start_delay_max": settings.get("start_delay_max", 60),
                "subnet_mode": settings.get("subnet_mode", DEFAULT_SUBNET_MODE),
                "precise_cidrs": precise_cidrs,
                "telegram_bot_token": settings.get("telegram_bot_token", ""),
                "telegram_chat_id": settings.get("telegram_chat_id", ""),
            },
        }
        
        return json.dumps(config_data, ensure_ascii=False)
    
    @classmethod
    async def _launch_worker(cls, account_id: int, email: str, config_json: str) -> Dict[str, Any]:
        """Core method: launch a worker subprocess from config JSON"""
        if not cls.SCRIPT_PATH.exists():
            return {"success": False, "error": "Worker script not found"}
        
        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            
            process = subprocess.Popen(
                ["python3", str(cls.SCRIPT_PATH), "--single-account"],
                cwd=str(cls.SCRIPT_DIR),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
            
            if process.stdin:
                process.stdin.write(config_json)
                process.stdin.close()
            
            log_task = asyncio.create_task(
                cls._read_account_logs(account_id, process)
            )
            
            worker_info = AccountWorkerInfo(
                process=process,
                log_task=log_task,
                account_id=account_id,
                email=email,
                config_json=config_json,
            )
            cls._account_workers[account_id] = worker_info
            cls._status = "running"
            
            ts = datetime.now().strftime("%H:%M:%S")
            cls._append_log(f"{ts} [SYSTEM] Started account {account_id} ({email}) PID={process.pid}")
            logger.info(f"Account {account_id} ({email}) started with PID {process.pid}")
            
            return {"success": True, "pid": process.pid, "account_id": account_id}
        except Exception as e:
            logger.error(f"Failed to start account {account_id}: {e}")
            return {"success": False, "error": str(e)}
    
    @classmethod
    async def start_account(cls, db: AsyncSession, account_id: int) -> Dict[str, Any]:
        """Start a worker subprocess for a specific account"""
        async with cls._get_lock():
            if account_id in cls._account_workers:
                info = cls._account_workers[account_id]
                if info.process and info.process.poll() is None:
                    return {"success": False, "error": "Account already running"}
                del cls._account_workers[account_id]
            
            config_json = await cls._get_account_config_json(db, account_id)
            if not config_json:
                return {"success": False, "error": "Account not found, disabled, or has no projects"}
            
            config_data = json.loads(config_json)
            email = config_data.get("email", "unknown")
            
            result = await cls._launch_worker(account_id, email, config_json)
            if result.get("success"):
                cls._desired_running.add(account_id)
                cls._restart_counts.pop(account_id, None)
                cls._ensure_watchdog()
            
            return result
    
    @classmethod
    async def stop_account(cls, account_id: int) -> Dict[str, Any]:
        """Stop a specific account's worker subprocess"""
        async with cls._get_lock():
            cls._desired_running.discard(account_id)
            
            if account_id not in cls._account_workers:
                return {"success": True, "message": "Was not running"}
            
            info = cls._account_workers[account_id]
            
            if not info.process or info.process.poll() is not None:
                info.status = "stopped"
                del cls._account_workers[account_id]
                cls._update_global_status()
                return {"success": True, "message": "Was not running"}
            
            try:
                info.status = "stopping"
                pid = info.process.pid
                
                info.process.terminate()
                
                loop = asyncio.get_event_loop()
                try:
                    await loop.run_in_executor(None, lambda: info.process.wait(timeout=3))
                except subprocess.TimeoutExpired:
                    info.process.kill()
                    await loop.run_in_executor(None, info.process.wait)
                
                if info.log_task and not info.log_task.done():
                    info.log_task.cancel()
                    try:
                        await info.log_task
                    except (asyncio.CancelledError, Exception):
                        pass
                
                info.status = "stopped"
                del cls._account_workers[account_id]
                cls._update_global_status()
                
                ts = datetime.now().strftime("%H:%M:%S")
                cls._append_log(f"{ts} [SYSTEM] Stopped account {account_id} ({info.email}) PID={pid}")
                
                logger.info(f"Account {account_id} ({info.email}) stopped")
                return {"success": True}
                
            except Exception as e:
                logger.error(f"Failed to stop account {account_id}: {e}")
                return {"success": False, "error": str(e)}
    
    @classmethod
    async def start_worker(cls, db: AsyncSession) -> Dict[str, Any]:
        """Start all enabled accounts (start-all) — parallel"""
        result = await db.execute(text(
            "SELECT id FROM ext_accounts WHERE enabled = TRUE ORDER BY id"
        ))
        account_ids = [row[0] for row in result.fetchall()]
        
        if not account_ids:
            return {"success": False, "error": "No enabled accounts"}
        
        already_running = []
        to_launch: Dict[int, str] = {}
        
        for aid in account_ids:
            if aid in cls._account_workers:
                info = cls._account_workers[aid]
                if info.process and info.process.poll() is None:
                    cls._desired_running.add(aid)
                    already_running.append(aid)
                    continue
                del cls._account_workers[aid]
            
            config_json = await cls._get_account_config_json(db, aid)
            if config_json:
                to_launch[aid] = config_json
        
        started = list(already_running)
        errors = []
        
        if to_launch:
            async def _launch_one(aid: int, cfg: str):
                config_data = json.loads(cfg)
                email = config_data.get("email", "unknown")
                return aid, await cls._launch_worker(aid, email, cfg)
            
            results = await asyncio.gather(*[_launch_one(aid, cfg) for aid, cfg in to_launch.items()])
            for aid, res in results:
                if res.get("success"):
                    cls._desired_running.add(aid)
                    cls._restart_counts.pop(aid, None)
                    started.append(aid)
                else:
                    errors.append(f"ACC {aid}: {res.get('error', 'unknown')}")
        
        if started:
            cls._ensure_watchdog()
        cls._status = "running" if started else "stopped"
        
        return {
            "success": len(started) > 0,
            "started": started,
            "errors": errors,
            "pid": None,
        }
    
    @classmethod
    async def stop_worker(cls, grace_seconds: float = 3) -> Dict[str, Any]:
        """Stop all running account workers (stop-all) — parallel"""
        cls._desired_running.clear()
        
        alive = {}
        dead_ids = []
        for aid, info in list(cls._account_workers.items()):
            if info.process and info.process.poll() is None:
                alive[aid] = info
            else:
                dead_ids.append(aid)
        
        dead_tasks = []
        for aid in dead_ids:
            info = cls._account_workers.pop(aid, None)
            if info and info.log_task and not info.log_task.done():
                info.log_task.cancel()
                dead_tasks.append(info.log_task)
        if dead_tasks:
            await asyncio.gather(*dead_tasks, return_exceptions=True)
        
        if not alive:
            cls._status = "stopped"
            return {"success": True, "message": "No workers running"}
        
        for info in alive.values():
            info.status = "stopping"
            try:
                info.process.terminate()
            except Exception:
                pass
        
        loop = asyncio.get_event_loop()
        pool = ThreadPoolExecutor(max_workers=len(alive))
        
        async def _wait_and_cleanup(aid: int, info):
            try:
                await loop.run_in_executor(pool, lambda: info.process.wait(timeout=grace_seconds))
            except subprocess.TimeoutExpired:
                try:
                    info.process.kill()
                except Exception:
                    pass
                try:
                    await loop.run_in_executor(pool, info.process.wait)
                except Exception:
                    pass
            except Exception:
                pass
            
            if info.log_task and not info.log_task.done():
                info.log_task.cancel()
            info.status = "stopped"
            cls._account_workers.pop(aid, None)
        
        try:
            await asyncio.gather(*[_wait_and_cleanup(aid, info) for aid, info in alive.items()])
        finally:
            pool.shutdown(wait=False)
        
        cancelled_logs = [info.log_task for info in alive.values() if info.log_task and not info.log_task.done()]
        if cancelled_logs:
            await asyncio.gather(*cancelled_logs, return_exceptions=True)
        
        ts = datetime.now().strftime("%H:%M:%S")
        cls._append_log(f"{ts} [SYSTEM] Stopped all workers ({len(alive)} accounts)")
        cls._status = "stopped"
        return {"success": True, "stopped": list(alive.keys())}
    
    @classmethod
    def _update_global_status(cls):
        """Update the global status based on active workers"""
        has_running = any(
            info.process and info.process.poll() is None
            for info in cls._account_workers.values()
        )
        cls._status = "running" if has_running else "stopped"
    
    @classmethod
    async def _read_account_logs(cls, account_id: int, process: subprocess.Popen):
        """Read logs from a per-account worker subprocess stdout"""
        loop = asyncio.get_event_loop()
        executor = cls._get_log_executor()
        
        def read_line():
            try:
                if process and process.stdout:
                    return process.stdout.readline()
            except:
                pass
            return ""
        
        try:
            while process and process.poll() is None:
                line = await loop.run_in_executor(executor, read_line)
                if line:
                    raw = line.rstrip()
                    
                    if raw.startswith("EVENT:"):
                        try:
                            event = json.loads(raw[6:])
                            cls._handle_event(account_id, event)
                        except json.JSONDecodeError:
                            pass
                        continue
                    
                    ts = datetime.now().strftime("%H:%M:%S")
                    log_entry = f"{ts} {raw}"
                    
                    cls._append_log(log_entry)
                    
                    if account_id in cls._account_workers:
                        info = cls._account_workers[account_id]
                        info.logs.append(log_entry)
                        if len(info.logs) > 500:
                            info.logs = info.logs[-500:]
                else:
                    await asyncio.sleep(0.1)
            
            if process and process.stdout:
                remaining = await loop.run_in_executor(executor, process.stdout.read)
                if remaining:
                    for line in remaining.split('\n'):
                        stripped = line.strip()
                        if not stripped:
                            continue
                        if stripped.startswith("EVENT:"):
                            try:
                                event = json.loads(stripped[6:])
                                cls._handle_event(account_id, event)
                            except json.JSONDecodeError:
                                pass
                            continue
                        ts = datetime.now().strftime("%H:%M:%S")
                        cls._append_log(f"{ts} {stripped}")
        
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Log reader error for account {account_id}: {e}")
        finally:
            exit_code = process.returncode if process else "?"
            ts = datetime.now().strftime("%H:%M:%S")
            info = cls._account_workers.get(account_id)
            email = info.email if info else "?"
            reason = info.finish_reason if info else None
            
            if reason:
                cls._append_log(
                    f"{ts} [SYSTEM] Account {account_id} ({email}) "
                    f"exited (code={exit_code}, reason={reason})"
                )
            else:
                cls._append_log(
                    f"{ts} [SYSTEM] Account {account_id} ({email}) "
                    f"died unexpectedly (exit_code={exit_code})"
                )
            logger.info(f"Account {account_id} process ended: exit_code={exit_code}, reason={reason}")
            
            if account_id in cls._account_workers:
                cls._account_workers[account_id].status = "stopped"
            cls._update_global_status()
    
    @classmethod
    def _handle_event(cls, account_id: int, event: Dict[str, Any]):
        """Handle a structured event from a worker subprocess"""
        event_type = event.get("type", "")
        info = cls._account_workers.get(account_id)
        
        if event_type == "ip_caught":
            if info:
                info.caught = event.get("total_caught", info.caught + 1)
                info.attempts = event.get("total_attempts", info.attempts)
                info.released = event.get("total_released", info.released)
            logger.info(f"IP caught for account {account_id}: {event.get('ip')}")

        elif event_type == "ip_released":
            if info:
                info.released = event.get("total_released", info.released + 1)
                info.attempts = event.get("total_attempts", info.attempts)

        elif event_type == "error":
            error_type = event.get("error_type", "UNKNOWN")
            error_msg = event.get("message", "")
            
            if info:
                info.error_type = error_type
                info.error_message = error_msg
                if error_type in ("BALANCE_DEPLETED", "ACCOUNT_BLOCKED"):
                    info.status = "error"
                    cls._desired_running.discard(account_id)
            
            _BALANCE_ERRORS = ("ACCESS_DENIED", "BALANCE_DEPLETED")
            if error_type in _BALANCE_ERRORS:
                for e in cls._account_errors:
                    if e["account_id"] == account_id and e["error_type"] in _BALANCE_ERRORS and not e.get("resolved"):
                        e["resolved"] = True
            
            cls._account_errors.append({
                "id": len(cls._account_errors) + 1,
                "account_id": account_id,
                "error_type": error_type,
                "error_message": error_msg,
                "project_name": event.get("project"),
                "created_at": datetime.now().isoformat(),
                "resolved": False,
            })
            if len(cls._account_errors) > cls._max_errors:
                cls._account_errors = cls._account_errors[-cls._max_errors:]
            
            logger.warning(f"Worker error for account {account_id}: {error_type} - {error_msg}")
        
        elif event_type == "proxy_waiting":
            if info:
                info.status = "proxy_waiting"
                info.error_type = "PROXY_DEAD"
            logger.warning(f"Account {account_id}: proxy down, waiting for recovery")

        elif event_type == "proxy_recovered":
            if info:
                info.status = "running"
                info.error_type = None
                info.error_message = None
            for e in cls._account_errors:
                if e["account_id"] == account_id and e["error_type"] == "PROXY_DEAD" and not e.get("resolved"):
                    e["resolved"] = True
            logger.info(f"Account {account_id}: proxy recovered")

        elif event_type == "status":
            if info:
                info.attempts = event.get("attempts", info.attempts)
                info.caught = event.get("caught", info.caught)
                info.released = event.get("released", info.released)

        elif event_type == "account_finished":
            reason = event.get("reason", "")
            caught = event.get("caught", 0)
            if info:
                info.attempts = event.get("attempts", info.attempts)
                info.caught = event.get("caught", info.caught)
                info.released = event.get("released", info.released)
                info.status = "stopped"
                info.finish_reason = reason
            
            no_restart_reasons = ("ALL_PROJECTS_EXHAUSTED", "PROXY_FAILED", "NO_PROXY", "FATAL_CRASH", "BALANCE_DEPLETED")
            if reason in no_restart_reasons or caught > 0:
                cls._desired_running.discard(account_id)
            
            logger.info(f"Account {account_id} finished: {reason}")
    
    # ─────────────────────────────────────────────────────────────────
    # WATCHDOG: auto-restart crashed workers
    # ─────────────────────────────────────────────────────────────────
    
    @classmethod
    def _ensure_watchdog(cls):
        """Ensure the watchdog background task is running"""
        if cls._watchdog_task is None or cls._watchdog_task.done():
            cls._watchdog_task = asyncio.create_task(cls._watchdog_loop())
    
    @classmethod
    async def _watchdog_loop(cls):
        """Background loop that checks worker health and restarts crashed ones"""
        _NO_RESTART_REASONS = frozenset({
            "ALL_PROJECTS_EXHAUSTED", "PROXY_FAILED", "NO_PROXY", "FATAL_CRASH", "BALANCE_DEPLETED",
        })
        _NO_RESTART_ERRORS = frozenset({
            "BALANCE_DEPLETED", "ACCOUNT_BLOCKED",
        })
        
        while True:
            await asyncio.sleep(cls._WATCHDOG_INTERVAL)
            
            if not cls._desired_running:
                continue
            
            try:
                await cls._watchdog_tick(_NO_RESTART_REASONS, _NO_RESTART_ERRORS)
            except Exception as e:
                logger.error(f"Watchdog tick error: {e}", exc_info=True)
                ts = datetime.now().strftime("%H:%M:%S")
                cls._append_log(f"{ts} [WATCHDOG] Internal error: {e}")
    
    @classmethod
    async def _watchdog_tick(cls, _NO_RESTART_REASONS: frozenset, _NO_RESTART_ERRORS: frozenset):
        """Single watchdog iteration — separated for error isolation"""
        for account_id in list(cls._desired_running):
            if account_id not in cls._desired_running:
                continue
            
            info = cls._account_workers.get(account_id)
            
            if info is None:
                cls._desired_running.discard(account_id)
                continue
            
            if info.process and info.process.poll() is None:
                if cls._restart_counts.get(account_id, 0) > 0:
                    uptime = (datetime.now() - info.started_at).total_seconds()
                    if uptime > cls._HEALTHY_UPTIME:
                        cls._restart_counts[account_id] = 0
                continue
            
            # Worker is dead — check if we should restart
            
            if info.finish_reason in _NO_RESTART_REASONS:
                ts = datetime.now().strftime("%H:%M:%S")
                cls._append_log(
                    f"{ts} [WATCHDOG] Account {account_id} ({info.email}): "
                    f"{info.finish_reason}, not restarting"
                )
                cls._desired_running.discard(account_id)
                continue
            
            if info.error_type in _NO_RESTART_ERRORS:
                ts = datetime.now().strftime("%H:%M:%S")
                cls._append_log(
                    f"{ts} [WATCHDOG] Account {account_id} ({info.email}): "
                    f"error {info.error_type}, not restarting"
                )
                cls._desired_running.discard(account_id)
                continue
            
            if info.caught > 0:
                ts = datetime.now().strftime("%H:%M:%S")
                cls._append_log(
                    f"{ts} [WATCHDOG] Account {account_id} ({info.email}): "
                    f"caught {info.caught} IPs, not restarting"
                )
                cls._desired_running.discard(account_id)
                continue
            
            count = cls._restart_counts.get(account_id, 0)
            if count >= cls._MAX_RESTARTS:
                ts = datetime.now().strftime("%H:%M:%S")
                cls._append_log(
                    f"{ts} [WATCHDOG] Account {account_id} ({info.email}): "
                    f"max restarts ({cls._MAX_RESTARTS}) reached, giving up"
                )
                cls._desired_running.discard(account_id)
                continue
            
            config_json = None
            if cls._engine:
                try:
                    async with AsyncSession(cls._engine, expire_on_commit=False) as session:
                        config_json = await cls._get_account_config_json(session, account_id)
                except Exception as e:
                    logger.warning(f"Watchdog: DB refresh failed for account {account_id}: {e}")
            
            if not config_json:
                config_json = info.config_json
            
            if not config_json:
                ts = datetime.now().strftime("%H:%M:%S")
                cls._append_log(
                    f"{ts} [WATCHDOG] Account {account_id}: no config available, cannot restart"
                )
                cls._desired_running.discard(account_id)
                continue
            
            if info.log_task and not info.log_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(info.log_task), timeout=10.0)
                except asyncio.TimeoutError:
                    logger.warning(f"Watchdog: log_task for account {account_id} still running, skipping this tick")
                    continue
                except (asyncio.CancelledError, Exception):
                    pass
            
            if account_id not in cls._desired_running:
                continue
            
            if info.finish_reason in _NO_RESTART_REASONS:
                ts = datetime.now().strftime("%H:%M:%S")
                cls._append_log(
                    f"{ts} [WATCHDOG] Account {account_id} ({info.email}): "
                    f"{info.finish_reason}, not restarting"
                )
                cls._desired_running.discard(account_id)
                continue
            
            if info.error_type in _NO_RESTART_ERRORS:
                ts = datetime.now().strftime("%H:%M:%S")
                cls._append_log(
                    f"{ts} [WATCHDOG] Account {account_id} ({info.email}): "
                    f"error {info.error_type}, not restarting"
                )
                cls._desired_running.discard(account_id)
                continue
            
            if info.caught > 0:
                ts = datetime.now().strftime("%H:%M:%S")
                cls._append_log(
                    f"{ts} [WATCHDOG] Account {account_id} ({info.email}): "
                    f"caught {info.caught} IPs, not restarting"
                )
                cls._desired_running.discard(account_id)
                continue
            
            exit_code = info.process.returncode if info.process else "?"
            cls._restart_counts[account_id] = count + 1
            
            ts = datetime.now().strftime("%H:%M:%S")
            cls._append_log(
                f"{ts} [WATCHDOG] Account {account_id} ({info.email}) "
                f"died (exit={exit_code}), restarting #{count + 1}..."
            )
            
            result = await cls._launch_worker(account_id, info.email, config_json)
            ts = datetime.now().strftime("%H:%M:%S")
            if result.get("success"):
                cls._append_log(
                    f"{ts} [WATCHDOG] Account {account_id} restarted OK "
                    f"(PID={result['pid']})"
                )
                logger.info(f"Watchdog restarted account {account_id} (PID={result['pid']})")
            else:
                cls._append_log(
                    f"{ts} [WATCHDOG] Account {account_id} restart FAILED: "
                    f"{result.get('error')}"
                )
                logger.error(f"Watchdog failed to restart account {account_id}: {result.get('error')}")
    
    @classmethod
    def get_status(cls) -> Dict[str, Any]:
        """Get aggregated status"""
        running_workers = {
            aid: info for aid, info in cls._account_workers.items()
            if info.process and info.process.poll() is None
        }
        has_running = len(running_workers) > 0
        
        return {
            "status": "running" if has_running else "stopped",
            "pid": None,
            "running": has_running,
            "active_count": len(running_workers),
            "total_workers": len(cls._account_workers),
        }
    
    @classmethod
    def get_worker_statuses(cls) -> Dict[str, Any]:
        """Get per-account worker statuses"""
        statuses = {}
        for aid, info in cls._account_workers.items():
            statuses[aid] = info.to_dict()
        
        # Also return unresolved errors
        errors = [e for e in cls._account_errors if not e.get("resolved")]
        
        return {
            "workers": statuses,
            "errors": errors,
        }
    
    @classmethod
    def get_account_errors(cls, account_id: int = None) -> List[Dict[str, Any]]:
        """Get errors, optionally filtered by account"""
        if account_id is not None:
            return [e for e in cls._account_errors if e["account_id"] == account_id and not e.get("resolved")]
        return [e for e in cls._account_errors if not e.get("resolved")]
    
    @classmethod
    def clear_account_errors(cls, account_id: int = None):
        """Clear/resolve errors for an account"""
        if account_id is not None:
            for e in cls._account_errors:
                if e["account_id"] == account_id:
                    e["resolved"] = True
            # Also clear error status on the worker
            if account_id in cls._account_workers:
                info = cls._account_workers[account_id]
                info.error_type = None
                info.error_message = None
        else:
            for e in cls._account_errors:
                e["resolved"] = True
    
    @classmethod
    def _append_log(cls, entry: str):
        """Append log entry to memory (for SSE) and file (persistent)"""
        cls._logs.append(entry)
        cls._log_seq += 1
        if len(cls._logs) > cls._max_logs:
            cls._logs = cls._logs[-cls._max_logs:]
        try:
            with open(cls.LOG_FILE, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
            if cls._log_seq % 500 == 0:
                cls._rotate_log_file()
        except Exception:
            pass
    
    @classmethod
    def _tail_file(cls, n: int) -> List[str]:
        """Read last N lines from log file"""
        try:
            if not cls.LOG_FILE.exists():
                return []
            with open(cls.LOG_FILE, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return []
                chunk_size = min(size, max(n * 256, 65536))
                f.seek(max(0, size - chunk_size))
                data = f.read().decode("utf-8", errors="replace")
                lines = [l for l in data.strip().split("\n") if l]
                return lines[-n:]
        except Exception:
            return []
    
    @classmethod
    def _rotate_log_file(cls):
        """Rotate log file if it exceeds max size"""
        try:
            if not cls.LOG_FILE.exists():
                return
            if cls.LOG_FILE.stat().st_size <= cls._LOG_MAX_SIZE:
                return
            lines = cls._tail_file(cls._LOG_ROTATE_KEEP)
            with open(cls.LOG_FILE, "w", encoding="utf-8") as f:
                for line in lines:
                    f.write(line + "\n")
            logger.info(f"Log file rotated, kept {len(lines)} lines")
        except Exception as e:
            logger.warning(f"Log rotation failed: {e}")
    
    @classmethod
    def _load_logs_from_file(cls):
        """Load persisted logs into memory on startup (survive OOM/restart)"""
        try:
            lines = cls._tail_file(cls._max_logs)
            if lines:
                cls._logs = lines
                cls._log_seq = len(lines)
                logger.info(f"Restored {len(lines)} log entries from {cls.LOG_FILE}")
        except Exception as e:
            logger.warning(f"Failed to load logs from file: {e}")
    
    @classmethod
    def get_logs(cls, lines: int = 100) -> List[str]:
        return cls._logs[-lines:] if cls._logs else []
    
    @classmethod
    def get_log_seq(cls) -> int:
        return cls._log_seq
    
    @classmethod
    def get_logs_after(cls, after_seq: int) -> tuple:
        """Return (new_logs, current_seq) for logs added after after_seq"""
        current_seq = cls._log_seq
        if after_seq >= current_seq:
            return [], current_seq
        new_count = min(current_seq - after_seq, len(cls._logs))
        return (cls._logs[-new_count:] if new_count > 0 else []), current_seq
    
    @classmethod
    def clear_logs(cls):
        cls._logs = []
        for info in cls._account_workers.values():
            info.logs = []
        try:
            cls.LOG_FILE.write_text("")
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────
    # MANUAL IP OBTAINMENT
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    async def manual_obtain_ip(
        db: AsyncSession,
        account_id: int,
        project_db_id: int,
        subnet_key: int,
        custom_subnet_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        acc_result = await db.execute(text(
            "SELECT id, email, password, proxy FROM ext_accounts WHERE id = :id"
        ), {"id": account_id})
        acc = acc_result.fetchone()
        if not acc:
            return {"success": False, "error": "Аккаунт не найден", "subnet_name": ""}

        proj_result = await db.execute(text(
            "SELECT id, account_id, project_id, project_name FROM ext_projects WHERE id = :id"
        ), {"id": project_db_id})
        proj = proj_result.fetchone()
        if not proj:
            return {"success": False, "error": "Проект не найден", "subnet_name": ""}
        if proj[1] != account_id:
            return {"success": False, "error": "Проект не принадлежит этому аккаунту", "subnet_name": ""}

        email = acc[1]
        password = acc[2] or ""
        proxy_str = acc[3] or None
        project_id = proj[2]
        subnet_name = custom_subnet_name.strip() if custom_subnet_name else get_subnet_name(subnet_key)

        def _do_obtain() -> Dict[str, Any]:
            session = _manual_create_session(proxy_str)
            try:
                ok, err, token = _manual_authenticate(session, email, password, project_id)
                if not ok:
                    return {"success": False, "error": f"Авторизация: {err}", "subnet_name": subnet_name}

                subnet_id, sub_err = _manual_get_subnet_id(session, token, subnet_name)
                if not subnet_id:
                    return {"success": False, "error": f"Подсеть: {sub_err}", "subnet_name": subnet_name}

                network_id, net_err = _manual_get_network_id(session, token)
                if not network_id:
                    return {"success": False, "error": f"Сеть: {net_err}", "subnet_name": subnet_name}

                ok, msg, ip_address = _manual_create_floating_ip(session, token, network_id, subnet_id)
                if ok:
                    return {"success": True, "ip_address": ip_address, "subnet_name": subnet_name}
                return {"success": False, "error": msg, "subnet_name": subnet_name}
            finally:
                session.close()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _do_obtain)

    # ─────────────────────────────────────────────────────────────────
    # CLOUD IPS — живой список адресов на ВМ и флоатингов аккаунта
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    async def get_account_cloud_ips(db: AsyncSession, account_id: int) -> Optional[Dict[str, Any]]:
        acc_result = await db.execute(text(
            "SELECT email, password, proxy FROM ext_accounts WHERE id = :id"
        ), {"id": account_id})
        acc = acc_result.fetchone()
        if not acc:
            return None

        proj_result = await db.execute(text(
            "SELECT id, project_id, project_name, disabled FROM ext_projects WHERE account_id = :aid ORDER BY id"
        ), {"aid": account_id})
        projects = proj_result.fetchall()
        # Соединение пула не должно ждать ответов VK — опрос проектов занимает секунды
        await db.commit()

        email, password, proxy_str = acc[0], acc[1] or "", acc[2] or None

        def _collect() -> List[Dict[str, Any]]:
            session = _manual_create_session(proxy_str)
            try:
                return [_collect_project_ips(session, email, password, p) for p in projects]
            finally:
                session.close()

        loop = asyncio.get_running_loop()
        project_views = await loop.run_in_executor(None, _collect)
        return {
            "account_id": account_id,
            "checked_at": datetime.now().isoformat(),
            "projects": project_views,
        }


# ═══════════════════════════════════════════════════════════════════════
# MANUAL OBTAIN HELPERS (OpenStack API — blocking requests)
# ═══════════════════════════════════════════════════════════════════════

_OS_IDENTITY_URL = "https://infra.mail.ru:35357/v3"
_OS_NETWORK_URL = "https://infra.mail.ru:9696/v2.0"
_OS_COMPUTE_URL = "https://infra.mail.ru:8774/v2.1"

_MANUAL_USER_AGENTS = [
    "python-openstackclient/6.6.0 keystoneauth1/5.8.0 python-requests/2.32.3",
    "python-openstackclient/6.5.0 keystoneauth1/5.7.0 python-requests/2.32.2",
    "openstacksdk/3.3.0 keystoneauth1/5.8.0 python-requests/2.32.3",
    "openstacksdk/3.2.0 keystoneauth1/5.7.0 python-requests/2.32.2",
]


def _manual_parse_proxy(proxy_str: str) -> Optional[str]:
    """Парсит строку прокси → socks5h URL"""
    if not proxy_str or not proxy_str.strip():
        return None

    proxy_str = proxy_str.strip()

    if "://" in proxy_str:
        parsed = urlparse(proxy_str)
        if parsed.hostname and parsed.port:
            scheme = parsed.scheme or "socks5h"
            if scheme == "socks5":
                scheme = "socks5h"
            if parsed.username and parsed.password:
                return f"{scheme}://{parsed.username}:{parsed.password}@{parsed.hostname}:{parsed.port}"
            return f"{scheme}://{parsed.hostname}:{parsed.port}"
        return None

    if "@" in proxy_str:
        parsed = urlparse(f"socks5h://{proxy_str}")
        if parsed.hostname and parsed.port:
            if parsed.username and parsed.password:
                return f"socks5h://{parsed.username}:{parsed.password}@{parsed.hostname}:{parsed.port}"
            return f"socks5h://{parsed.hostname}:{parsed.port}"
        return None

    parts = proxy_str.split(":")
    if len(parts) == 4:
        # host:port:user:pass
        host, port, user, pw = parts
        try:
            int(port)
            return f"socks5h://{user}:{pw}@{host}:{port}"
        except ValueError:
            # user:pass:host:port
            user2, pw2, host2, port2 = parts
            try:
                int(port2)
                return f"socks5h://{user2}:{pw2}@{host2}:{port2}"
            except ValueError:
                return None
    elif len(parts) == 2:
        host, port = parts
        try:
            int(port)
            return f"socks5h://{host}:{port}"
        except ValueError:
            return None

    return None


def _manual_create_session(proxy_str: Optional[str] = None) -> requests.Session:
    import random
    session = requests.Session()

    if proxy_str:
        proxy_url = _manual_parse_proxy(proxy_str)
        if proxy_url:
            session.proxies = {"http": proxy_url, "https": proxy_url}

    ua = random.choice(_MANUAL_USER_AGENTS)
    session.headers.update({
        "User-Agent": ua,
        "Accept": "application/json",
        "Content-Type": "application/json",
    })

    retry = Retry(total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session


def _manual_authenticate(
    session: requests.Session, email: str, password: str, project_id: str,
    timeout: float = 30,
) -> tuple:
    """(ok, error_or_ok, token)"""
    auth_data = {
        "auth": {
            "identity": {
                "methods": ["password"],
                "password": {
                    "user": {
                        "name": email,
                        "domain": {"name": "users"},
                        "password": password,
                    }
                }
            },
            "scope": {
                "project": {"id": project_id}
            }
        }
    }

    try:
        resp = session.post(f"{_OS_IDENTITY_URL}/auth/tokens", json=auth_data, timeout=timeout)

        if resp.status_code == 201:
            token = resp.headers.get("X-Subject-Token")
            return True, "OK", token
        if resp.status_code == 401:
            return False, "AUTH_ERROR_401: неверный логин/пароль", None
        if resp.status_code == 403:
            return False, "ACCESS_DENIED_403: доступ запрещён", None
        return False, f"HTTP_{resp.status_code}", None

    except requests.exceptions.ProxyError as e:
        return False, f"PROXY_ERROR: {str(e)[:80]}", None
    except requests.exceptions.Timeout:
        return False, "TIMEOUT: таймаут подключения", None
    except requests.exceptions.ConnectionError as e:
        return False, f"CONNECTION_ERROR: {str(e)[:80]}", None
    except Exception as e:
        return False, f"ERROR: {str(e)[:80]}", None


def _manual_get_subnet_id(
    session: requests.Session, token: str, subnet_name: str, timeout: float = 30,
) -> tuple:
    """(subnet_id_or_none, error_or_ok)"""
    headers = {"X-Auth-Token": token}
    try:
        resp = session.get(
            f"{_OS_NETWORK_URL}/subnets", params={"name": subnet_name},
            headers=headers, timeout=timeout,
        )
        if resp.status_code == 200:
            subnets = resp.json().get("subnets", [])
            if subnets:
                sid = subnets[0].get("id")
                if sid:
                    return sid, "OK"
                return None, "NO_ID_IN_RESPONSE"
            return None, f"SUBNET_NOT_FOUND: подсеть '{subnet_name}' не найдена"
        return None, f"HTTP_{resp.status_code}"
    except requests.exceptions.ProxyError as e:
        return None, f"PROXY_ERROR: {str(e)[:80]}"
    except requests.exceptions.Timeout:
        return None, "TIMEOUT"
    except Exception as e:
        return None, f"ERROR: {str(e)[:80]}"


def _manual_get_network_id(
    session: requests.Session, token: str, timeout: float = 30,
) -> tuple:
    """(network_id_or_none, error_or_ok)"""
    headers = {"X-Auth-Token": token}
    try:
        resp = session.get(
            f"{_OS_NETWORK_URL}/networks", params={"name": "internet"},
            headers=headers, timeout=timeout,
        )
        if resp.status_code == 200:
            networks = resp.json().get("networks", [])
            if networks:
                return networks[0]["id"], "OK"
            return None, "NETWORK_NOT_FOUND"
        return None, f"HTTP_{resp.status_code}"
    except Exception as e:
        return None, f"ERROR: {str(e)[:80]}"


def _manual_create_floating_ip(
    session: requests.Session, token: str, network_id: str, subnet_id: str,
    timeout: float = 30,
) -> tuple:
    """(ok, error_or_msg, ip_or_none)"""
    headers = {"X-Auth-Token": token}
    body = {
        "floatingip": {
            "floating_network_id": network_id,
            "subnet_id": subnet_id,
        }
    }

    try:
        resp = session.post(f"{_OS_NETWORK_URL}/floatingips", json=body, headers=headers, timeout=timeout)

        if resp.status_code in (200, 201):
            ip = resp.json().get("floatingip", {}).get("floating_ip_address")
            if ip:
                return True, "SUCCESS", ip
            return False, "NO_IP_IN_RESPONSE", None

        try:
            error_data = resp.json()
            error_msg = str(error_data).lower()
            error_detail = ""
            if isinstance(error_data, dict):
                for key in ["NeutronError", "error", "message"]:
                    if key in error_data:
                        inner = error_data[key]
                        if isinstance(inner, dict):
                            error_detail = inner.get("message", "") or inner.get("detail", "")
                        elif isinstance(inner, str):
                            error_detail = inner
                        break
                if not error_detail:
                    error_detail = str(error_data)[:200]
            else:
                error_detail = str(error_data)[:200]
        except Exception:
            error_msg = resp.text.lower()
            error_detail = resp.text[:200]

        if resp.status_code == 401:
            return False, "AUTH_ERROR: токен истёк", None
        if resp.status_code == 403:
            return False, f"ACCESS_DENIED: {error_detail}", None
        if resp.status_code == 409:
            if "quota" in error_msg:
                return False, f"QUOTA_EXCEEDED: {error_detail}", None
            if "no more" in error_msg or "exhausted" in error_msg or "dike" in error_msg:
                return False, "NO_MORE_IP: свободных IP нет", None
            return False, f"CONFLICT: {error_detail}", None
        if "quota" in error_msg:
            return False, f"QUOTA_EXCEEDED: {error_detail}", None
        if "no more ip" in error_msg or "exhausted" in error_msg:
            return False, "NO_MORE_IP: свободных IP нет", None
        return False, f"HTTP_{resp.status_code}: {error_detail}", None

    except requests.exceptions.ProxyError as e:
        return False, f"PROXY_ERROR: {str(e)[:80]}", None
    except requests.exceptions.Timeout:
        return False, "TIMEOUT: таймаут запроса", None
    except requests.exceptions.ConnectionError as e:
        return False, f"CONNECTION_ERROR: {str(e)[:80]}", None
    except Exception as e:
        return False, f"ERROR: {str(e)[:80]}", None


def _manual_list(
    session: requests.Session, token: str, url: str, key: str, timeout: float = 30,
) -> tuple:
    """(items_or_none, error_or_ok) — список из ответа OpenStack по ключу коллекции"""
    try:
        resp = session.get(url, headers={"X-Auth-Token": token}, timeout=timeout)
    except requests.exceptions.ProxyError as e:
        return None, f"PROXY_ERROR: {str(e)[:80]}"
    except requests.exceptions.Timeout:
        return None, "TIMEOUT"
    except Exception as e:
        return None, f"ERROR: {str(e)[:80]}"

    if resp.status_code == 401:
        return None, "AUTH_ERROR: токен истёк"
    if resp.status_code == 403:
        return None, "ACCESS_DENIED_403: доступ запрещён"
    if resp.status_code != 200:
        return None, f"HTTP_{resp.status_code}"

    try:
        items = resp.json().get(key)
    except ValueError:
        items = None
    if not isinstance(items, list):
        return None, "BAD_RESPONSE: нет списка в ответе"
    return items, "OK"


def _is_public_ip(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


def _summarize_project_ips(servers: List[Dict[str, Any]], floating_ips: List[Dict[str, Any]]) -> Dict[str, Any]:
    """ВМ с их публичными адресами и флоатинги, которые не видны на ВМ.

    Флоатинг на ВМ Nova показывает в её адресах с типом floating — такой адрес
    выводится только у ВМ. Отдельным списком остаются свободные флоатинги и
    привязанные к портам без ВМ (балансировщик, маршрутизатор)."""
    vms = []
    vm_addresses = set()
    vm_by_fixed_ip = {}
    for server in servers:
        name = server.get("name") or server.get("id") or "?"
        ips = []
        for network_addrs in (server.get("addresses") or {}).values():
            for entry in network_addrs or []:
                address = entry.get("addr")
                if not address:
                    continue
                is_floating = entry.get("OS-EXT-IPS:type") == "floating"
                if not is_floating:
                    vm_by_fixed_ip[address] = name
                if not _is_public_ip(address):
                    continue
                ips.append({"address": address, "kind": "floating" if is_floating else "direct"})
                vm_addresses.add(address)
        vms.append({"id": server.get("id"), "name": name, "status": server.get("status"), "ips": ips})

    floating = []
    for fip in floating_ips:
        address = fip.get("floating_ip_address")
        if not address or address in vm_addresses:
            continue
        fixed_ip = fip.get("fixed_ip_address")
        floating.append({
            "address": address,
            "status": fip.get("status"),
            "bound": bool(fip.get("port_id")),
            "fixed_ip": fixed_ip,
            "attached_to": vm_by_fixed_ip.get(fixed_ip) if fixed_ip else None,
        })

    return {"servers": vms, "floating_ips": floating}


def _collect_project_ips(session: requests.Session, email: str, password: str, project_row) -> Dict[str, Any]:
    project_db_id, project_id, project_name, disabled = project_row
    view = {
        "project_db_id": project_db_id,
        "project_id": project_id,
        "project_name": project_name,
        "disabled": bool(disabled),
        "error": None,
        "servers": [],
        "floating_ips": [],
    }

    ok, err, token = _manual_authenticate(session, email, password, project_id)
    if not ok:
        view["error"] = f"Авторизация: {err}"
        return view

    servers, err = _manual_list(session, token, f"{_OS_COMPUTE_URL}/servers/detail", "servers")
    if servers is None:
        view["error"] = f"ВМ: {err}"
        return view

    floating_ips, err = _manual_list(session, token, f"{_OS_NETWORK_URL}/floatingips", "floatingips")
    if floating_ips is None:
        view["error"] = f"Флоатинги: {err}"
        return view

    view.update(_summarize_project_ips(servers, floating_ips))
    return view


# ═══════════════════════════════════════════════════════════════════════
# MULTIQUEUE — включение hw_vif_multiqueue_enabled на образе
# ═══════════════════════════════════════════════════════════════════════

async def enable_multiqueue(
    auth_url: str, project_id: str, image_id: str,
    auth_type: str = "password",
    username: str = "", user_domain_name: str = "users",
    password: str = "", api_token: str = "",
    proxy: str = "",
) -> Dict[str, Any]:
    import json as json_lib

    if proxy and not _manual_parse_proxy(proxy):
        return {"success": False, "error": "Неверный формат прокси. Ожидается host:port или host:port:user:pass"}

    def _do_enable() -> Dict[str, Any]:
        session = _manual_create_session(proxy)

        try:
            # 1. Аутентификация Keystone — password или token
            if auth_type == "v3token":
                auth_data = {
                    "auth": {
                        "identity": {
                            "methods": ["token"],
                            "token": {"id": api_token}
                        },
                        "scope": {"project": {"id": project_id}}
                    }
                }
            else:
                auth_data = {
                    "auth": {
                        "identity": {
                            "methods": ["password"],
                            "password": {
                                "user": {
                                    "name": username,
                                    "domain": {"name": user_domain_name},
                                    "password": password,
                                }
                            }
                        },
                        "scope": {"project": {"id": project_id}}
                    }
                }

            identity_url = auth_url.rstrip("/")
            resp = session.post(f"{identity_url}/auth/tokens", json=auth_data, timeout=30)

            if resp.status_code == 401:
                msg = "Неверный API токен" if auth_type == "v3token" else "Неверный логин или пароль"
                return {"success": False, "error": msg}
            if resp.status_code != 201:
                detail = ""
                try:
                    detail = resp.json().get("error", {}).get("message", resp.text[:200])
                except Exception:
                    detail = resp.text[:200]
                return {"success": False, "error": f"Авторизация HTTP {resp.status_code}: {detail}"}

            token = resp.headers.get("X-Subject-Token")
            if not token:
                return {"success": False, "error": "Токен не получен из ответа"}

            # 2. Glance endpoint — пробуем из каталога, потом из auth_url
            catalog = resp.json().get("token", {}).get("catalog", [])
            catalog_glance = None
            for svc in catalog:
                if svc.get("type") == "image":
                    for ep in svc.get("endpoints", []):
                        if ep.get("interface") == "public":
                            catalog_glance = ep.get("url")
                            break
                    break

            def _normalize_glance(url: str) -> str:
                url = url.rstrip("/")
                if url.startswith("http://"):
                    url = "https://" + url[7:]
                if not url.endswith("/v2"):
                    url += "/v2"
                return url

            # Формируем список URL для попыток: каталог + производный от auth_url
            glance_candidates = []
            if catalog_glance:
                glance_candidates.append(_normalize_glance(catalog_glance))

            # Производный URL из auth_url (для случаев когда каталог указывает не тот регион)
            from urllib.parse import urlparse as _urlparse
            parsed_auth = _urlparse(identity_url)
            if parsed_auth.hostname:
                derived = f"https://{parsed_auth.hostname}"
                if parsed_auth.port and parsed_auth.port not in (443, 80):
                    derived += f":{parsed_auth.port}"
                # Паттерн VK Cloud: /infra/identity/v3 → /infra/image
                if "/infra/identity" in parsed_auth.path:
                    derived_url = derived + "/infra/image/v2"
                else:
                    derived_url = derived + "/v2"
                if derived_url not in glance_candidates:
                    glance_candidates.append(derived_url)

            if not glance_candidates:
                return {"success": False, "error": "Glance endpoint не найден"}

            # 3. PATCH образа — пробуем каждый endpoint
            patch_body = json_lib.dumps([{"op": "add", "path": "/hw_vif_multiqueue_enabled", "value": "true"}])
            last_error = ""
            for glance_url in glance_candidates:
                patch_resp = session.patch(
                    f"{glance_url}/images/{image_id}",
                    headers={
                        "X-Auth-Token": token,
                        "Content-Type": "application/openstack-images-v2.1-json-patch",
                    },
                    data=patch_body,
                    timeout=30,
                )

                if patch_resp.status_code == 200:
                    img = patch_resp.json()
                    return {
                        "success": True,
                        "image_name": img.get("name", ""),
                        "image_id": img.get("id", image_id),
                        "multiqueue": img.get("hw_vif_multiqueue_enabled", "true"),
                    }

                # Если не 403/404 — значит ошибка не связана с endpoint, сразу возвращаем
                if patch_resp.status_code not in (403, 404):
                    detail = ""
                    try:
                        body = patch_resp.json()
                        detail = body.get("message", body.get("faultstring", patch_resp.text[:200]))
                    except Exception:
                        detail = patch_resp.text[:200]
                    return {"success": False, "error": f"HTTP {patch_resp.status_code}: {detail} (endpoint: {glance_url})"}

                # 403/404 — запоминаем и пробуем следующий endpoint
                try:
                    body = patch_resp.json()
                    last_error = body.get("message", body.get("faultstring", patch_resp.text[:200]))
                except Exception:
                    last_error = patch_resp.text[:200]

            tried = " → ".join(glance_candidates)
            return {"success": False, "error": f"HTTP {patch_resp.status_code}: {last_error} (пробовал: {tried})"}

        except requests.exceptions.ProxyError as e:
            return {"success": False, "error": f"Ошибка прокси: {str(e)[:100]}"}
        except requests.exceptions.Timeout:
            return {"success": False, "error": "Таймаут запроса"}
        except requests.exceptions.ConnectionError as e:
            return {"success": False, "error": f"Ошибка подключения: {str(e)[:100]}"}
        except Exception as e:
            return {"success": False, "error": f"Ошибка: {str(e)[:150]}"}
        finally:
            session.close()

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _do_enable)


async def get_all_subnets(db: AsyncSession) -> Dict[int, str]:
    merged = dict(AVAILABLE_SUBNETS)
    try:
        result = await db.execute(text("SELECT id, cidr FROM ext_custom_subnets ORDER BY id"))
        for row in result.fetchall():
            merged[row[0]] = row[1]
    except Exception:
        pass
    return merged


async def get_custom_subnets(db: AsyncSession) -> List[Dict[str, Any]]:
    try:
        result = await db.execute(text("SELECT id, cidr, created_at FROM ext_custom_subnets ORDER BY id"))
        return [{"id": r[0], "cidr": r[1], "created_at": r[2].isoformat() if r[2] else None} for r in result.fetchall()]
    except Exception:
        return []


async def add_custom_subnet(db: AsyncSession, subnet_id: int, cidr: str) -> Dict[str, Any]:
    if subnet_id in AVAILABLE_SUBNETS:
        return {"success": False, "error": f"ext-sub{subnet_id} is a built-in subnet"}
    
    try:
        await db.execute(
            text("INSERT INTO ext_custom_subnets (id, cidr) VALUES (:id, :cidr) ON CONFLICT (id) DO UPDATE SET cidr = :cidr"),
            {"id": subnet_id, "cidr": cidr}
        )
        await db.commit()
        return {"success": True, "id": subnet_id, "cidr": cidr}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def remove_custom_subnet(db: AsyncSession, subnet_id: int) -> Dict[str, Any]:
    if subnet_id in AVAILABLE_SUBNETS:
        return {"success": False, "error": f"ext-sub{subnet_id} is a built-in subnet, cannot remove"}

    try:
        result = await db.execute(text("DELETE FROM ext_custom_subnets WHERE id = :id"), {"id": subnet_id})
        await db.commit()
        if result.rowcount == 0:
            return {"success": False, "error": f"Subnet ext-sub{subnet_id} not found"}
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────
# УЗКИЙ ЦЕЛЕВОЙ ФИЛЬТР (precise CIDR)
# Держим только IP из этих диапазонов внутри выбранной подсети, остальные
# floating-ip воркер освобождает и перебирает дальше.
# ─────────────────────────────────────────────────────────────────────

async def get_precise_cidrs(db: AsyncSession) -> List[Dict[str, Any]]:
    try:
        result = await db.execute(text(
            "SELECT id, cidr, created_at FROM ext_precise_cidrs ORDER BY id"
        ))
        return [
            {"id": r[0], "cidr": r[1], "created_at": r[2].isoformat() if r[2] else None}
            for r in result.fetchall()
        ]
    except Exception:
        return []


async def get_precise_cidr_list(db: AsyncSession) -> List[str]:
    return [it["cidr"] for it in await get_precise_cidrs(db)]


async def add_precise_cidr(db: AsyncSession, cidr: str) -> Dict[str, Any]:
    cidr = (cidr or "").strip()
    if not cidr:
        return {"success": False, "error": "CIDR пуст"}

    try:
        cidr = str(ipaddress.ip_network(cidr, strict=False))
    except ValueError as e:
        return {"success": False, "error": f"Некорректный CIDR: {e}"}

    try:
        await db.execute(
            text("INSERT INTO ext_precise_cidrs (cidr) VALUES (:cidr)"),
            {"cidr": cidr},
        )
        await db.commit()
        result = await db.execute(text("SELECT id FROM ext_precise_cidrs WHERE cidr = :cidr"), {"cidr": cidr})
        row = result.fetchone()
        return {"success": True, "id": row[0] if row else None, "cidr": cidr}
    except Exception as e:
        if any(k in str(e).lower() for k in ("unique", "duplicate")):
            return {"success": False, "error": f"Диапазон {cidr} уже добавлен"}
        return {"success": False, "error": str(e)}


async def remove_precise_cidr(db: AsyncSession, cidr_id: int) -> Dict[str, Any]:
    try:
        result = await db.execute(text("DELETE FROM ext_precise_cidrs WHERE id = :id"), {"id": cidr_id})
        await db.commit()
        if result.rowcount == 0:
            return {"success": False, "error": f"CIDR id={cidr_id} not found"}
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
