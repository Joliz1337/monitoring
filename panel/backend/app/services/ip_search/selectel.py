"""
Selectel IP Service Module
Зеркало CloudEvoService для Selectel (OpenStack): выделяет floating-IP через
VPC resell v2 API, проверяет попадание в whitelist CIDR-подсетей, копит все
попадания. Авторизация — Keystone Identity v3 (account-scoped токен).
"""

import os
import json
import asyncio
import logging
import subprocess
import ipaddress
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

RECEIVED_IPS = received_stats.ReceivedIpsStore("selectel_received_ips")


# ═══════════════════════════════════════════════════════════════════════
# PROXY PARSER (зеркало selectel_worker)
# ═══════════════════════════════════════════════════════════════════════

def _parse_proxy(proxy_str: str) -> Optional[str]:
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
        host, port, user, pw = parts
        try:
            int(port)
            return f"socks5h://{user}:{pw}@{host}:{port}"
        except ValueError:
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


logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════

SELECTEL_IDENTITY_URL = "https://cloud.api.selcloud.ru/identity/v3"
SELECTEL_VPC_URL = "https://api.selectel.ru/vpc/resell/v2"
SELECTEL_RESOURCE = "floatingips"

SELECTEL_DEFAULT_REGIONS = ["ru-1", "ru-2", "ru-3", "ru-7", "ru-8", "ru-9"]

BUSY_STATUSES = {"DOWN", "PENDING", "PENDING_CREATE", "CREATING", "BUILD"}

# Маска для пароля в ответах API (никогда не отдаём наружу)
SECRET_MASK = "****"


# ═══════════════════════════════════════════════════════════════════════
# DB TABLES
# ═══════════════════════════════════════════════════════════════════════

TABLES_SQL = """
CREATE TABLE IF NOT EXISTS selectel_accounts (
    id SERIAL PRIMARY KEY,
    label VARCHAR(255),
    username VARCHAR(255) NOT NULL,
    password VARCHAR(500) NOT NULL,
    domain VARCHAR(100) NOT NULL,
    project_id VARCHAR(100) NOT NULL,
    proxy VARCHAR(500),
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS selectel_caught_ips (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL,
    ip_address VARCHAR(50) NOT NULL,
    fip_id VARCHAR(100),
    region VARCHAR(50),
    caught_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES selectel_accounts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS selectel_settings (
    id INTEGER PRIMARY KEY DEFAULT 1,
    delay_burst REAL DEFAULT 0.5,
    delay_round REAL DEFAULT 8.0,
    delay_delete REAL DEFAULT 3.0,
    jitter REAL DEFAULT 2.0,
    provision_timeout INTEGER DEFAULT 60,
    provision_poll REAL DEFAULT 2.0,
    regions TEXT DEFAULT '["ru-3"]',
    telegram_bot_token VARCHAR(500) DEFAULT '',
    telegram_chat_id VARCHAR(100) DEFAULT '',
    proxy_required BOOLEAN DEFAULT FALSE,
    log_level VARCHAR(10) DEFAULT 'info',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS selectel_subnets (
    id SERIAL PRIMARY KEY,
    cidr VARCHAR(50) NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_selectel_caught_account ON selectel_caught_ips(account_id);
""" + received_stats.received_ips_ddl("selectel_received_ips", "selectel_accounts")


async def init_tables(engine):
    SelectelService._engine = engine
    async with engine.begin() as conn:
        for statement in TABLES_SQL.strip().split(';'):
            statement = statement.strip()
            if not statement:
                continue
            try:
                await conn.execute(text(statement))
            except Exception as e:
                if "already exists" not in str(e).lower():
                    logger.debug(f"Selectel table creation note: {e}")

    SelectelService._tables_initialized = True
    logger.info("Selectel tables initialized")
    SelectelService._load_logs_from_file()


# ═══════════════════════════════════════════════════════════════════════
# ACCOUNT WORKER INFO
# ═══════════════════════════════════════════════════════════════════════

class AccountWorkerInfo:
    __slots__ = (
        'process', 'log_task', 'account_id', 'label', 'status',
        'error_type', 'error_message', 'started_at', 'attempts', 'caught',
        'current_round', 'logs', 'config_json', 'finish_reason',
    )

    def __init__(self, process: subprocess.Popen, log_task: asyncio.Task,
                 account_id: int, label: str, config_json: str = ""):
        self.process = process
        self.log_task = log_task
        self.account_id = account_id
        self.label = label
        self.status: str = "running"
        self.error_type: Optional[str] = None
        self.error_message: Optional[str] = None
        self.started_at: datetime = datetime.now()
        self.attempts: int = 0
        self.caught: int = 0
        self.current_round: int = 0
        self.logs: List[str] = []
        self.config_json: str = config_json
        self.finish_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        is_alive = self.process and self.process.poll() is None
        return {
            "account_id": self.account_id,
            "label": self.label,
            "status": self.status if is_alive else "stopped",
            "error_type": self.error_type,
            "error_message": self.error_message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "attempts": self.attempts,
            "caught": self.caught,
            "current_round": self.current_round,
            "pid": self.process.pid if is_alive else None,
        }


# ═══════════════════════════════════════════════════════════════════════
# SELECTEL SERVICE
# ═══════════════════════════════════════════════════════════════════════

class SelectelService:

    _account_workers: Dict[int, AccountWorkerInfo] = {}
    _logs: List[str] = []
    _max_logs: int = 1000
    _start_lock: Optional[asyncio.Lock] = None
    _account_errors: List[Dict[str, Any]] = []
    _max_errors: int = 200
    _log_seq: int = 0

    _tables_initialized: bool = False
    _init_lock: Optional[asyncio.Lock] = None

    LOG_FILE = Path("/app/data/selectel_worker.log")
    _LOG_MAX_SIZE: int = 5 * 1024 * 1024
    _LOG_ROTATE_KEEP: int = 3000

    _desired_running: set = set()
    _watchdog_task: Optional[asyncio.Task] = None
    _restart_counts: Dict[int, int] = {}
    _MAX_RESTARTS: int = 10
    _WATCHDOG_INTERVAL: int = 30
    _HEALTHY_UPTIME: int = 300
    _engine = None
    _log_executor: Optional[ThreadPoolExecutor] = None

    _status: str = "stopped"

    SCRIPT_DIR = Path("/app/ext")
    SCRIPT_PATH = SCRIPT_DIR / "selectel_worker.py"

    # ─────────────────────────────────────────────────────────────────
    # INTERNAL HELPERS
    # ─────────────────────────────────────────────────────────────────

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        if cls._start_lock is None:
            cls._start_lock = asyncio.Lock()
        return cls._start_lock

    @classmethod
    def _get_log_executor(cls) -> ThreadPoolExecutor:
        if cls._log_executor is None or cls._log_executor._shutdown:
            cls._log_executor = ThreadPoolExecutor(max_workers=500, thread_name_prefix="selectel-log-reader")
        return cls._log_executor

    @classmethod
    async def _ensure_tables(cls) -> None:
        if cls._tables_initialized:
            return
        if cls._init_lock is None:
            cls._init_lock = asyncio.Lock()
        async with cls._init_lock:
            if cls._tables_initialized:
                return
            engine = cls._engine
            if engine is None:
                try:
                    from app.database import engine as _eng
                    engine = _eng
                    cls._engine = engine
                except Exception as e:
                    logger.error(f"Selectel lazy init: engine unavailable: {e}")
                    return
            try:
                await init_tables(engine)
            except Exception as e:
                logger.error(f"Selectel lazy table init failed: {e}")

    # ─────────────────────────────────────────────────────────────────
    # ACCOUNTS
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    async def get_accounts(db: AsyncSession) -> List[Dict[str, Any]]:
        await SelectelService._ensure_tables()
        result = await db.execute(text(
            "SELECT id, label, username, password, domain, project_id, proxy, enabled, created_at "
            "FROM selectel_accounts ORDER BY id"
        ))
        accounts = result.fetchall()

        has_caught_ips_table = False
        try:
            table_check = await db.execute(text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'selectel_caught_ips' AND table_schema = 'public')"
            ))
            has_caught_ips_table = table_check.scalar()
        except Exception:
            pass

        output = []
        for acc in accounts:
            ip_count = 0
            if has_caught_ips_table:
                try:
                    ip_result = await db.execute(text(
                        "SELECT COUNT(*) FROM selectel_caught_ips WHERE account_id = :aid"
                    ), {"aid": acc[0]})
                    ip_count = ip_result.scalar() or 0
                except Exception:
                    pass

            output.append({
                "id": acc[0],
                "label": acc[1] or "",
                "username": acc[2],
                "password_masked": SECRET_MASK,
                "domain": acc[4],
                "project_id": acc[5],
                "proxy": acc[6] or "",
                "enabled": bool(acc[7]) if acc[7] is not None else True,
                "created_at": acc[8],
                "caught_ip_count": ip_count,
            })
        return output

    @staticmethod
    async def create_account(
        db: AsyncSession, username: str, password: str, domain: str, project_id: str,
        label: str = "", proxy: str = "",
    ) -> Dict[str, Any]:
        await SelectelService._ensure_tables()
        await db.execute(text(
            "INSERT INTO selectel_accounts (label, username, password, domain, project_id, proxy, enabled) "
            "VALUES (:label, :username, :password, :domain, :project_id, :proxy, TRUE)"
        ), {
            "label": label or None,
            "username": username,
            "password": password,
            "domain": domain,
            "project_id": project_id,
            "proxy": proxy or None,
        })
        await db.commit()

        result = await db.execute(text(
            "SELECT id, label, username, password, domain, project_id, proxy, enabled, created_at "
            "FROM selectel_accounts WHERE username = :u AND project_id = :pid "
            "ORDER BY id DESC LIMIT 1"
        ), {"u": username, "pid": project_id})
        acc = result.fetchone()

        return {
            "id": acc[0],
            "label": acc[1] or "",
            "username": acc[2],
            "password_masked": SECRET_MASK,
            "domain": acc[4],
            "project_id": acc[5],
            "proxy": acc[6] or "",
            "enabled": bool(acc[7]) if acc[7] is not None else True,
            "created_at": acc[8],
            "caught_ip_count": 0,
        }

    @staticmethod
    async def update_account(db: AsyncSession, account_id: int, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        await SelectelService._ensure_tables()
        updates = []
        params = {"id": account_id}

        field_map = {
            "label": "label",
            "username": "username",
            "password": "password",
            "domain": "domain",
            "project_id": "project_id",
            "proxy": "proxy",
            "enabled": "enabled",
        }

        for key, col in field_map.items():
            if key in data:
                updates.append(f"{col} = :{key}")
                if key == "enabled":
                    params[key] = bool(data[key])
                elif key == "proxy":
                    params[key] = data[key] or None
                else:
                    params[key] = data[key]

        if not updates:
            return None

        await db.execute(text(
            f"UPDATE selectel_accounts SET {', '.join(updates)} WHERE id = :id"
        ), params)
        await db.commit()

        result = await db.execute(text(
            "SELECT id, label, username, password, domain, project_id, proxy, enabled, created_at "
            "FROM selectel_accounts WHERE id = :id"
        ), {"id": account_id})
        acc = result.fetchone()
        if not acc:
            return None

        ip_result = await db.execute(text(
            "SELECT COUNT(*) FROM selectel_caught_ips WHERE account_id = :aid"
        ), {"aid": acc[0]})
        ip_count = ip_result.scalar() or 0

        return {
            "id": acc[0],
            "label": acc[1] or "",
            "username": acc[2],
            "password_masked": SECRET_MASK,
            "domain": acc[4],
            "project_id": acc[5],
            "proxy": acc[6] or "",
            "enabled": bool(acc[7]) if acc[7] is not None else True,
            "created_at": acc[8],
            "caught_ip_count": ip_count,
        }

    @staticmethod
    async def delete_account(db: AsyncSession, account_id: int) -> bool:
        await SelectelService._ensure_tables()
        result = await db.execute(text(
            "DELETE FROM selectel_accounts WHERE id = :aid"
        ), {"aid": account_id})
        await db.commit()
        return result.rowcount > 0

    # ─────────────────────────────────────────────────────────────────
    # CAUGHT IPS
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    async def get_caught_ips(db: AsyncSession, account_id: Optional[int] = None) -> List[Dict[str, Any]]:
        await SelectelService._ensure_tables()
        query = """SELECT c.id, c.account_id, c.ip_address, c.fip_id, c.region, c.caught_at, a.label
                   FROM selectel_caught_ips c
                   LEFT JOIN selectel_accounts a ON c.account_id = a.id"""
        params = {}

        if account_id is not None:
            query += " WHERE c.account_id = :account_id"
            params["account_id"] = account_id

        query += " ORDER BY c.caught_at DESC"

        result = await db.execute(text(query), params)
        return [{
            "id": r[0],
            "account_id": r[1],
            "ip_address": r[2],
            "fip_id": r[3] or "",
            "region": r[4] or "",
            "caught_at": r[5],
            "account_label": r[6] or "",
        } for r in result.fetchall()]

    @staticmethod
    async def save_caught_ip(
        db: AsyncSession, account_id: int, ip_address: str,
        fip_id: Optional[str] = None, region: Optional[str] = None,
    ) -> Dict[str, Any]:
        await SelectelService._ensure_tables()
        await db.execute(text(
            "INSERT INTO selectel_caught_ips (account_id, ip_address, fip_id, region) "
            "VALUES (:account_id, :ip_address, :fip_id, :region)"
        ), {
            "account_id": account_id,
            "ip_address": ip_address,
            "fip_id": fip_id,
            "region": region,
        })
        await db.commit()

        result = await db.execute(text(
            "SELECT id, caught_at FROM selectel_caught_ips WHERE ip_address = :ip ORDER BY id DESC LIMIT 1"
        ), {"ip": ip_address})
        row = result.fetchone()

        return {
            "id": row[0],
            "account_id": account_id,
            "ip_address": ip_address,
            "fip_id": fip_id,
            "region": region,
            "caught_at": row[1],
        }

    @staticmethod
    async def delete_caught_ip(db: AsyncSession, ip_id: int) -> bool:
        await SelectelService._ensure_tables()
        result = await db.execute(text(
            "DELETE FROM selectel_caught_ips WHERE id = :id"
        ), {"id": ip_id})
        await db.commit()
        return result.rowcount > 0

    @staticmethod
    async def clear_caught_ips(db: AsyncSession, account_id: Optional[int] = None) -> int:
        await SelectelService._ensure_tables()
        if account_id is not None:
            result = await db.execute(text(
                "DELETE FROM selectel_caught_ips WHERE account_id = :aid"
            ), {"aid": account_id})
        else:
            result = await db.execute(text("DELETE FROM selectel_caught_ips"))
        await db.commit()
        return result.rowcount

    # ─────────────────────────────────────────────────────────────────
    # RECEIVED IPS STATS
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    async def save_received_ips(db: AsyncSession, account_id: int, items: List[Dict[str, Any]]) -> int:
        await SelectelService._ensure_tables()
        return await RECEIVED_IPS.save(db, account_id, items)

    @staticmethod
    async def get_received_stats(db: AsyncSession, account_id: Optional[int] = None) -> Dict[str, Any]:
        await SelectelService._ensure_tables()
        return await RECEIVED_IPS.stats(db, account_id, await get_whitelist_cidrs(db))

    @staticmethod
    async def get_received_subnet_ips(
        db: AsyncSession, subnet: str, account_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        await SelectelService._ensure_tables()
        return await RECEIVED_IPS.subnet_ips(db, subnet, account_id)

    @staticmethod
    async def clear_received_ips(db: AsyncSession, account_id: Optional[int] = None) -> int:
        await SelectelService._ensure_tables()
        return await RECEIVED_IPS.clear(db, account_id)

    # ─────────────────────────────────────────────────────────────────
    # SETTINGS
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    async def get_settings(db: AsyncSession) -> Dict[str, Any]:
        await SelectelService._ensure_tables()
        result = await db.execute(text(
            """SELECT delay_burst, delay_round, delay_delete, jitter,
                      provision_timeout, provision_poll, regions,
                      telegram_bot_token, telegram_chat_id,
                      proxy_required, log_level
               FROM selectel_settings WHERE id = 1"""
        ))
        row = result.fetchone()

        if not row:
            await db.execute(text(
                "INSERT INTO selectel_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING"
            ))
            await db.commit()
            return await SelectelService.get_settings(db)

        regions_raw = row[6]
        try:
            regions = json.loads(regions_raw) if regions_raw else ["ru-3"]
        except (json.JSONDecodeError, TypeError):
            regions = ["ru-3"]

        whitelist_cidrs = await get_whitelist_cidrs(db)

        return {
            "delay_burst": float(row[0]) if row[0] is not None else 0.5,
            "delay_round": float(row[1]) if row[1] is not None else 8.0,
            "delay_delete": float(row[2]) if row[2] is not None else 3.0,
            "jitter": float(row[3]) if row[3] is not None else 2.0,
            "provision_timeout": int(row[4]) if row[4] is not None else 60,
            "provision_poll": float(row[5]) if row[5] is not None else 2.0,
            "regions": regions,
            "telegram_bot_token": row[7] or "",
            "telegram_chat_id": row[8] or "",
            "proxy_required": bool(row[9]) if row[9] is not None else False,
            "log_level": row[10] if row[10] else "info",
            "available_regions": list(SELECTEL_DEFAULT_REGIONS),
            "whitelist_cidrs": whitelist_cidrs,
        }

    @staticmethod
    async def update_settings(db: AsyncSession, data: Dict[str, Any]) -> Dict[str, Any]:
        await SelectelService._ensure_tables()
        await db.execute(text(
            "INSERT INTO selectel_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING"
        ))

        updates = []
        params = {}

        float_fields = {"delay_burst", "delay_round", "delay_delete", "jitter", "provision_poll"}
        int_fields = {"provision_timeout"}
        boolean_fields = {"proxy_required"}
        string_fields = {"telegram_bot_token", "telegram_chat_id", "log_level"}

        for key in float_fields | int_fields | boolean_fields | string_fields:
            if key in data:
                updates.append(f"{key} = :{key}")
                value = data[key]
                if key in boolean_fields:
                    params[key] = bool(value)
                elif key in float_fields:
                    params[key] = float(value)
                elif key in int_fields:
                    params[key] = int(value)
                else:
                    params[key] = value

        if "regions" in data:
            regions_value = data["regions"]
            if not isinstance(regions_value, list):
                regions_value = ["ru-3"]
            regions_value = [r for r in regions_value if r in SELECTEL_DEFAULT_REGIONS]
            if not regions_value:
                regions_value = ["ru-3"]
            updates.append("regions = :regions")
            params["regions"] = json.dumps(regions_value)

        updates.append("updated_at = CURRENT_TIMESTAMP")

        if updates:
            await db.execute(text(
                f"UPDATE selectel_settings SET {', '.join(updates)} WHERE id = 1"
            ), params)
            await db.commit()

        return await SelectelService.get_settings(db)

    # ─────────────────────────────────────────────────────────────────
    # WORKER CONFIG BUILDER
    # ─────────────────────────────────────────────────────────────────

    @classmethod
    async def _get_account_config_json(cls, db: AsyncSession, account_id: int) -> Optional[str]:
        await cls._ensure_tables()
        acc_result = await db.execute(text(
            "SELECT id, label, username, password, domain, project_id, proxy "
            "FROM selectel_accounts WHERE id = :id AND enabled = TRUE"
        ), {"id": account_id})
        acc = acc_result.fetchone()
        if not acc:
            return None

        settings = await cls.get_settings(db)
        whitelist_cidrs = settings.get("whitelist_cidrs", [])

        config_data = {
            "account_id": acc[0],
            "label": acc[1] or f"selectel-{acc[0]}",
            "username": acc[2],
            "password": acc[3],
            "domain": acc[4],
            "project_id": acc[5],
            "proxy": acc[6] or None,
            "regions": settings.get("regions", ["ru-3"]),
            "whitelist_cidrs": whitelist_cidrs,
            "panel_base_url": "http://localhost:8000/_int",
            "config": {
                "delay_burst": settings.get("delay_burst", 0.5),
                "delay_round": settings.get("delay_round", 8.0),
                "delay_delete": settings.get("delay_delete", 3.0),
                "jitter": settings.get("jitter", 2.0),
                "provision_timeout": settings.get("provision_timeout", 60),
                "provision_poll": settings.get("provision_poll", 2.0),
                "proxy_required": settings.get("proxy_required", False),
                "log_level": settings.get("log_level", "info"),
                "telegram_bot_token": settings.get("telegram_bot_token", ""),
                "telegram_chat_id": settings.get("telegram_chat_id", ""),
            },
        }

        return json.dumps(config_data, ensure_ascii=False)

    # ─────────────────────────────────────────────────────────────────
    # WORKER LIFECYCLE
    # ─────────────────────────────────────────────────────────────────

    @classmethod
    async def _launch_worker(cls, account_id: int, label: str, config_json: str) -> Dict[str, Any]:
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
                label=label,
                config_json=config_json,
            )
            cls._account_workers[account_id] = worker_info
            cls._status = "running"

            ts = datetime.now().strftime("%H:%M:%S")
            cls._append_log(
                f"{ts} [SYSTEM] Started Selectel account {account_id} ({label}) PID={process.pid}"
            )
            logger.info(f"Selectel account {account_id} ({label}) started with PID {process.pid}")

            return {"success": True, "pid": process.pid, "account_id": account_id}
        except Exception as e:
            logger.error(f"Failed to start Selectel account {account_id}: {e}")
            return {"success": False, "error": str(e)}

    @classmethod
    async def start_account(cls, db: AsyncSession, account_id: int) -> Dict[str, Any]:
        async with cls._get_lock():
            if account_id in cls._account_workers:
                info = cls._account_workers[account_id]
                if info.process and info.process.poll() is None:
                    return {"success": False, "error": "Account already running"}
                del cls._account_workers[account_id]

            config_json = await cls._get_account_config_json(db, account_id)
            if not config_json:
                return {"success": False, "error": "Account not found or disabled"}

            config_data = json.loads(config_json)
            label = config_data.get("label", "unknown")

            result = await cls._launch_worker(account_id, label, config_json)
            if result.get("success"):
                cls._desired_running.add(account_id)
                cls._restart_counts.pop(account_id, None)
                cls._ensure_watchdog()

            return result

    @classmethod
    async def stop_account(cls, account_id: int) -> Dict[str, Any]:
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
                    await loop.run_in_executor(None, lambda: info.process.wait(timeout=5))
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
                cls._append_log(
                    f"{ts} [SYSTEM] Stopped Selectel account {account_id} ({info.label}) PID={pid}"
                )

                logger.info(f"Selectel account {account_id} ({info.label}) stopped")
                return {"success": True}

            except Exception as e:
                logger.error(f"Failed to stop Selectel account {account_id}: {e}")
                return {"success": False, "error": str(e)}

    @classmethod
    async def start_worker(cls, db: AsyncSession) -> Dict[str, Any]:
        await cls._ensure_tables()
        result = await db.execute(text(
            "SELECT id FROM selectel_accounts WHERE enabled = TRUE ORDER BY id"
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
                label = config_data.get("label", "unknown")
                return aid, await cls._launch_worker(aid, label, cfg)

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
    async def stop_worker(cls, grace_seconds: float = 5) -> Dict[str, Any]:
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
        cls._append_log(f"{ts} [SYSTEM] Stopped all Selectel workers ({len(alive)} accounts)")
        cls._status = "stopped"
        return {"success": True, "stopped": list(alive.keys())}

    @classmethod
    def _update_global_status(cls):
        has_running = any(
            info.process and info.process.poll() is None
            for info in cls._account_workers.values()
        )
        cls._status = "running" if has_running else "stopped"

    # ─────────────────────────────────────────────────────────────────
    # LOG READER + EVENT HANDLER
    # ─────────────────────────────────────────────────────────────────

    @classmethod
    async def _read_account_logs(cls, account_id: int, process: subprocess.Popen):
        loop = asyncio.get_event_loop()
        executor = cls._get_log_executor()

        def read_line():
            try:
                if process and process.stdout:
                    return process.stdout.readline()
            except Exception:
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
            logger.error(f"Selectel log reader error for account {account_id}: {e}")
        finally:
            exit_code = process.returncode if process else "?"
            ts = datetime.now().strftime("%H:%M:%S")
            info = cls._account_workers.get(account_id)
            label = info.label if info else "?"
            reason = info.finish_reason if info else None

            if reason:
                cls._append_log(
                    f"{ts} [SYSTEM] Selectel account {account_id} ({label}) "
                    f"exited (code={exit_code}, reason={reason})"
                )
            else:
                cls._append_log(
                    f"{ts} [SYSTEM] Selectel account {account_id} ({label}) "
                    f"died unexpectedly (exit_code={exit_code})"
                )
            logger.info(
                f"Selectel account {account_id} process ended: "
                f"exit_code={exit_code}, reason={reason}"
            )

            if account_id in cls._account_workers:
                cls._account_workers[account_id].status = "stopped"
            cls._update_global_status()

    @classmethod
    def _handle_event(cls, account_id: int, event: Dict[str, Any]):
        event_type = event.get("type", "")
        info = cls._account_workers.get(account_id)

        if event_type == "ip_caught":
            if info:
                info.caught = event.get("total_caught", info.caught + 1)
                info.attempts = event.get("total_attempts", info.attempts)
            logger.info(f"Selectel IP caught for account {account_id}: {event.get('ip')}")

        elif event_type == "ip_attempt":
            if info:
                info.attempts += 1

        elif event_type == "round_start":
            if info:
                info.current_round = event.get("round_no", info.current_round + 1)

        elif event_type == "round_complete":
            if info:
                info.attempts = event.get("total_attempts", info.attempts)
                info.caught = event.get("total_caught", info.caught)

        elif event_type == "quota_hit":
            if info:
                info.error_type = None
                info.error_message = None

        elif event_type == "error":
            error_type = event.get("error_type", "UNKNOWN")
            error_msg = event.get("message", "")

            if info:
                info.error_type = error_type
                info.error_message = error_msg
                if error_type in ("AUTH_FAILED", "ACCOUNT_BLOCKED", "NO_PROXY"):
                    info.status = "error"
                    cls._desired_running.discard(account_id)

            cls._account_errors.append({
                "id": len(cls._account_errors) + 1,
                "account_id": account_id,
                "error_type": error_type,
                "error_message": error_msg,
                "created_at": datetime.now().isoformat(),
                "resolved": False,
            })
            if len(cls._account_errors) > cls._max_errors:
                cls._account_errors = cls._account_errors[-cls._max_errors:]

            logger.warning(
                f"Selectel worker error for account {account_id}: {error_type} - {error_msg}"
            )

        elif event_type == "proxy_waiting":
            if info:
                info.status = "proxy_waiting"
                info.error_type = "PROXY_DEAD"

        elif event_type == "proxy_recovered":
            if info:
                info.status = "running"
                info.error_type = None
                info.error_message = None
            for e in cls._account_errors:
                if e["account_id"] == account_id and e["error_type"] == "PROXY_DEAD" and not e.get("resolved"):
                    e["resolved"] = True

        elif event_type == "account_finished":
            reason = event.get("reason", "")
            if info:
                info.attempts = event.get("attempts", info.attempts)
                info.caught = event.get("caught", info.caught)
                info.status = "stopped"
                info.finish_reason = reason

            no_restart_reasons = ("NO_PROXY", "FATAL_CRASH", "AUTH_FAILED", "INVALID_CONFIG")
            if reason in no_restart_reasons:
                cls._desired_running.discard(account_id)

            logger.info(f"Selectel account {account_id} finished: {reason}")

    # ─────────────────────────────────────────────────────────────────
    # WATCHDOG
    # ─────────────────────────────────────────────────────────────────

    @classmethod
    def _ensure_watchdog(cls):
        if cls._watchdog_task is None or cls._watchdog_task.done():
            cls._watchdog_task = asyncio.create_task(cls._watchdog_loop())

    @classmethod
    async def _watchdog_loop(cls):
        _NO_RESTART_REASONS = frozenset({
            "NO_PROXY", "FATAL_CRASH", "AUTH_FAILED", "INVALID_CONFIG",
        })
        _NO_RESTART_ERRORS = frozenset({
            "AUTH_FAILED", "ACCOUNT_BLOCKED",
        })

        while True:
            await asyncio.sleep(cls._WATCHDOG_INTERVAL)

            if not cls._desired_running:
                continue

            try:
                await cls._watchdog_tick(_NO_RESTART_REASONS, _NO_RESTART_ERRORS)
            except Exception as e:
                logger.error(f"Selectel watchdog tick error: {e}", exc_info=True)
                ts = datetime.now().strftime("%H:%M:%S")
                cls._append_log(f"{ts} [WATCHDOG] Internal error: {e}")

    @classmethod
    async def _watchdog_tick(cls, _NO_RESTART_REASONS: frozenset, _NO_RESTART_ERRORS: frozenset):
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

            if info.finish_reason in _NO_RESTART_REASONS:
                ts = datetime.now().strftime("%H:%M:%S")
                cls._append_log(
                    f"{ts} [WATCHDOG] Selectel account {account_id} ({info.label}): "
                    f"{info.finish_reason}, not restarting"
                )
                cls._desired_running.discard(account_id)
                continue

            if info.error_type in _NO_RESTART_ERRORS:
                ts = datetime.now().strftime("%H:%M:%S")
                cls._append_log(
                    f"{ts} [WATCHDOG] Selectel account {account_id} ({info.label}): "
                    f"error {info.error_type}, not restarting"
                )
                cls._desired_running.discard(account_id)
                continue

            count = cls._restart_counts.get(account_id, 0)
            if count >= cls._MAX_RESTARTS:
                ts = datetime.now().strftime("%H:%M:%S")
                cls._append_log(
                    f"{ts} [WATCHDOG] Selectel account {account_id} ({info.label}): "
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
                    logger.warning(
                        f"Selectel watchdog: DB refresh failed for account {account_id}: {e}"
                    )

            if not config_json:
                config_json = info.config_json

            if not config_json:
                ts = datetime.now().strftime("%H:%M:%S")
                cls._append_log(
                    f"{ts} [WATCHDOG] Selectel account {account_id}: "
                    f"no config available, cannot restart"
                )
                cls._desired_running.discard(account_id)
                continue

            if info.log_task and not info.log_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(info.log_task), timeout=10.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        f"Selectel watchdog: log_task for account {account_id} "
                        f"still running, skipping this tick"
                    )
                    continue
                except (asyncio.CancelledError, Exception):
                    pass

            if account_id not in cls._desired_running:
                continue

            if info.finish_reason in _NO_RESTART_REASONS:
                cls._desired_running.discard(account_id)
                continue

            if info.error_type in _NO_RESTART_ERRORS:
                cls._desired_running.discard(account_id)
                continue

            exit_code = info.process.returncode if info.process else "?"
            cls._restart_counts[account_id] = count + 1

            ts = datetime.now().strftime("%H:%M:%S")
            cls._append_log(
                f"{ts} [WATCHDOG] Selectel account {account_id} ({info.label}) "
                f"died (exit={exit_code}), restarting #{count + 1}..."
            )

            result = await cls._launch_worker(account_id, info.label, config_json)
            ts = datetime.now().strftime("%H:%M:%S")
            if result.get("success"):
                cls._append_log(
                    f"{ts} [WATCHDOG] Selectel account {account_id} restarted OK "
                    f"(PID={result['pid']})"
                )
                logger.info(
                    f"Selectel watchdog restarted account {account_id} (PID={result['pid']})"
                )
            else:
                cls._append_log(
                    f"{ts} [WATCHDOG] Selectel account {account_id} restart FAILED: "
                    f"{result.get('error')}"
                )
                logger.error(
                    f"Selectel watchdog failed to restart account {account_id}: "
                    f"{result.get('error')}"
                )

    # ─────────────────────────────────────────────────────────────────
    # STATUS / ERRORS
    # ─────────────────────────────────────────────────────────────────

    @classmethod
    def get_status(cls) -> Dict[str, Any]:
        running_workers = {
            aid: info for aid, info in cls._account_workers.items()
            if info.process and info.process.poll() is None
        }
        return {
            "status": "running" if running_workers else "stopped",
            "pid": None,
            "running": len(running_workers) > 0,
            "active_count": len(running_workers),
            "total_workers": len(cls._account_workers),
        }

    @classmethod
    def get_worker_statuses(cls) -> Dict[str, Any]:
        statuses = {}
        for aid, info in cls._account_workers.items():
            statuses[aid] = info.to_dict()

        errors = [e for e in cls._account_errors if not e.get("resolved")]
        return {"workers": statuses, "errors": errors}

    @classmethod
    def get_account_errors(cls, account_id: Optional[int] = None) -> List[Dict[str, Any]]:
        if account_id is not None:
            return [
                e for e in cls._account_errors
                if e["account_id"] == account_id and not e.get("resolved")
            ]
        return [e for e in cls._account_errors if not e.get("resolved")]

    @classmethod
    def clear_account_errors(cls, account_id: Optional[int] = None):
        if account_id is not None:
            for e in cls._account_errors:
                if e["account_id"] == account_id:
                    e["resolved"] = True
            if account_id in cls._account_workers:
                info = cls._account_workers[account_id]
                info.error_type = None
                info.error_message = None
        else:
            for e in cls._account_errors:
                e["resolved"] = True

    # ─────────────────────────────────────────────────────────────────
    # LOGGING (in-memory + persistent file)
    # ─────────────────────────────────────────────────────────────────

    @classmethod
    def _append_log(cls, entry: str):
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
        try:
            if not cls.LOG_FILE.exists():
                return
            if cls.LOG_FILE.stat().st_size <= cls._LOG_MAX_SIZE:
                return
            lines = cls._tail_file(cls._LOG_ROTATE_KEEP)
            with open(cls.LOG_FILE, "w", encoding="utf-8") as f:
                for line in lines:
                    f.write(line + "\n")
            logger.info(f"Selectel log file rotated, kept {len(lines)} lines")
        except Exception as e:
            logger.warning(f"Selectel log rotation failed: {e}")

    @classmethod
    def _load_logs_from_file(cls):
        try:
            lines = cls._tail_file(cls._max_logs)
            if lines:
                cls._logs = lines
                cls._log_seq = len(lines)
                logger.info(f"Restored {len(lines)} Selectel log entries from {cls.LOG_FILE}")
        except Exception as e:
            logger.warning(f"Failed to load Selectel logs from file: {e}")

    @classmethod
    def get_logs(cls, lines: int = 100) -> List[str]:
        return cls._logs[-lines:] if cls._logs else []

    @classmethod
    def get_log_seq(cls) -> int:
        return cls._log_seq

    @classmethod
    def get_logs_after(cls, after_seq: int) -> tuple:
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
    # MANUAL IP OBTAINMENT (Selectel VPC resell — blocking)
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    async def manual_obtain_ip(
        db: AsyncSession, account_id: int, region: str,
    ) -> Dict[str, Any]:
        await SelectelService._ensure_tables()
        acc_result = await db.execute(text(
            "SELECT id, label, username, password, domain, project_id, proxy "
            "FROM selectel_accounts WHERE id = :id"
        ), {"id": account_id})
        acc = acc_result.fetchone()
        if not acc:
            return {"success": False, "error": "Аккаунт не найден"}

        username = acc[2]
        password = acc[3]
        domain = acc[4]
        project_id = acc[5]
        proxy_str = acc[6] or None

        if region not in SELECTEL_DEFAULT_REGIONS:
            return {"success": False, "error": f"Неизвестный регион: {region}"}

        def _do_obtain() -> Dict[str, Any]:
            session = _selectel_create_session(proxy_str)
            try:
                token, auth_err = _selectel_auth(session, username, password, domain)
                if not token:
                    return {"success": False, "error": f"Авторизация: {auth_err}"}

                fip, create_err = _selectel_create_fip(session, token, project_id, region)
                if not fip:
                    return {"success": False, "error": f"Выделение floating-ip: {create_err}"}

                ip_address = fip.get("floating_ip_address")
                fip_id = fip.get("id")
                if not ip_address:
                    return {
                        "success": False,
                        "error": "Адрес не пришёл в ответе",
                        "fip_id": fip_id,
                    }

                return {
                    "success": True,
                    "ip_address": ip_address,
                    "fip_id": fip_id,
                    "region": region,
                }
            finally:
                session.close()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _do_obtain)


# ═══════════════════════════════════════════════════════════════════════
# SELECTEL API HELPERS (blocking requests)
# ═══════════════════════════════════════════════════════════════════════

def _selectel_create_session(proxy_str: Optional[str] = None) -> requests.Session:
    session = requests.Session()

    if proxy_str:
        proxy_url = _parse_proxy(proxy_str)
        if proxy_url:
            session.proxies = {"http": proxy_url, "https": proxy_url}

    session.headers.update({
        "Accept": "application/json",
        "Content-Type": "application/json",
    })

    retry = Retry(total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session


def _selectel_auth(
    session: requests.Session, username: str, password: str, domain: str,
    project_id: Optional[str] = None, timeout: float = 30,
) -> tuple:
    """(token_or_none, error_or_none). Без project_id — account-scoped."""
    if project_id:
        scope = {"project": {"id": project_id}}
    else:
        scope = {"domain": {"name": domain}}

    body = {
        "auth": {
            "identity": {
                "methods": ["password"],
                "password": {
                    "user": {
                        "name": username,
                        "domain": {"name": domain},
                        "password": password,
                    }
                },
            },
            "scope": scope,
        }
    }
    try:
        resp = session.post(
            f"{SELECTEL_IDENTITY_URL}/auth/tokens",
            json=body, timeout=timeout,
        )
        if resp.status_code == 201:
            token = resp.headers.get("X-Subject-Token")
            if token:
                return token, None
            return None, "X-Subject-Token отсутствует в ответе"
        if resp.status_code == 401:
            return None, "Неверный логин, пароль или account id"
        try:
            detail = resp.json().get("error", {}).get("message", resp.text[:200])
        except Exception:
            detail = resp.text[:200]
        return None, f"HTTP_{resp.status_code}: {detail}"
    except requests.exceptions.ProxyError as e:
        return None, f"PROXY_ERROR: {str(e)[:80]}"
    except requests.exceptions.Timeout:
        return None, "TIMEOUT: таймаут подключения"
    except requests.exceptions.ConnectionError as e:
        return None, f"CONNECTION_ERROR: {str(e)[:80]}"
    except Exception as e:
        return None, f"ERROR: {str(e)[:80]}"


def _selectel_create_fip(
    session: requests.Session, token: str, project_id: str, region: str,
    timeout: float = 30,
) -> tuple:
    """(fip_dict_or_none, error_or_none)"""
    body = {"floatingips": [{"region": region, "quantity": 1}]}
    headers = {"X-Auth-Token": token}
    try:
        resp = session.post(
            f"{SELECTEL_VPC_URL}/{SELECTEL_RESOURCE}/projects/{project_id}",
            json=body, headers=headers, timeout=timeout,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            items = data.get("floatingips", data) if isinstance(data, dict) else data
            if items:
                return items[0], None
            return None, "пустой ответ создания"
        try:
            detail = resp.json().get("error", resp.text[:200])
        except Exception:
            detail = resp.text[:200]
        if resp.status_code == 401:
            return None, "AUTH_ERROR: токен истёк или невалиден"
        if resp.status_code == 429:
            return None, f"QUOTA_EXCEEDED: {detail}"
        return None, f"HTTP_{resp.status_code}: {detail}"
    except requests.exceptions.ProxyError as e:
        return None, f"PROXY_ERROR: {str(e)[:80]}"
    except requests.exceptions.Timeout:
        return None, "TIMEOUT"
    except requests.exceptions.ConnectionError as e:
        return None, f"CONNECTION_ERROR: {str(e)[:80]}"
    except Exception as e:
        return None, f"ERROR: {str(e)[:80]}"


# ═══════════════════════════════════════════════════════════════════════
# MULTIQUEUE — включение hw_vif_multiqueue_enabled на образе (Glance)
# ═══════════════════════════════════════════════════════════════════════

async def enable_multiqueue(
    auth_url: str, project_id: str, image_id: str,
    auth_type: str = "password",
    username: str = "", user_domain_name: str = "",
    password: str = "", api_token: str = "",
    region: str = "", proxy: str = "",
) -> Dict[str, Any]:
    import json as json_lib

    if proxy and not _parse_proxy(proxy):
        return {"success": False, "error": "Неверный формат прокси. Ожидается host:port или host:port:user:pass"}

    def _do_enable() -> Dict[str, Any]:
        session = _selectel_create_session(proxy)
        try:
            # 1. Аутентификация Keystone (project-scoped — Glance требует scope проекта)
            if auth_type == "v3token":
                auth_data = {
                    "auth": {
                        "identity": {"methods": ["token"], "token": {"id": api_token}},
                        "scope": {"project": {"id": project_id}},
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
                            },
                        },
                        "scope": {"project": {"id": project_id}},
                    }
                }

            identity_url = auth_url.rstrip("/")
            resp = session.post(f"{identity_url}/auth/tokens", json=auth_data, timeout=30)

            if resp.status_code == 401:
                msg = "Неверный API токен" if auth_type == "v3token" else "Неверный логин или пароль"
                return {"success": False, "error": msg}
            if resp.status_code != 201:
                try:
                    detail = resp.json().get("error", {}).get("message", resp.text[:200])
                except Exception:
                    detail = resp.text[:200]
                return {"success": False, "error": f"Авторизация HTTP {resp.status_code}: {detail}"}

            token = resp.headers.get("X-Subject-Token")
            if not token:
                return {"success": False, "error": "Токен не получен из ответа"}

            # 2. Glance endpoint — регион (если задан) + каталог
            def _normalize_glance(url: str) -> str:
                url = url.rstrip("/")
                if url.startswith("http://"):
                    url = "https://" + url[7:]
                if not url.endswith("/v2"):
                    url += "/v2"
                return url

            glance_candidates = []
            if region:
                glance_candidates.append(f"https://{region}.cloud.api.selcloud.ru/image/v2")

            catalog = resp.json().get("token", {}).get("catalog", [])
            for svc in catalog:
                if svc.get("type") == "image":
                    for ep in svc.get("endpoints", []):
                        if ep.get("interface") == "public":
                            if region and ep.get("region") and ep.get("region") != region:
                                continue
                            cand = _normalize_glance(ep.get("url", ""))
                            if cand and cand not in glance_candidates:
                                glance_candidates.append(cand)
                    break

            if not glance_candidates:
                return {"success": False, "error": "Glance endpoint не найден (укажите регион в RC-файле)"}

            # 3. PATCH образа — пробуем каждый endpoint
            patch_body = json_lib.dumps([{"op": "add", "path": "/hw_vif_multiqueue_enabled", "value": "true"}])
            last_error = ""
            patch_resp = None
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

                if patch_resp.status_code not in (403, 404):
                    try:
                        body = patch_resp.json()
                        detail = body.get("message", body.get("faultstring", patch_resp.text[:200]))
                    except Exception:
                        detail = patch_resp.text[:200]
                    return {"success": False, "error": f"HTTP {patch_resp.status_code}: {detail} (endpoint: {glance_url})"}

                try:
                    body = patch_resp.json()
                    last_error = body.get("message", body.get("faultstring", patch_resp.text[:200]))
                except Exception:
                    last_error = patch_resp.text[:200]

            tried = " → ".join(glance_candidates)
            code = patch_resp.status_code if patch_resp is not None else "?"
            return {"success": False, "error": f"HTTP {code}: {last_error} (пробовал: {tried})"}

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


# ═══════════════════════════════════════════════════════════════════════
# WHITELIST CIDR
# ═══════════════════════════════════════════════════════════════════════

async def get_whitelist(db: AsyncSession) -> List[Dict[str, Any]]:
    await SelectelService._ensure_tables()
    try:
        result = await db.execute(text(
            "SELECT id, cidr, created_at FROM selectel_subnets ORDER BY id"
        ))
        return [
            {
                "id": r[0],
                "cidr": r[1],
                "created_at": r[2].isoformat() if r[2] else None,
            }
            for r in result.fetchall()
        ]
    except Exception:
        return []


async def get_whitelist_cidrs(db: AsyncSession) -> List[str]:
    items = await get_whitelist(db)
    return [it["cidr"] for it in items]


async def add_whitelist_cidr(db: AsyncSession, cidr: str) -> Dict[str, Any]:
    await SelectelService._ensure_tables()
    cidr = (cidr or "").strip()
    if not cidr:
        return {"success": False, "error": "CIDR пуст"}

    try:
        net = ipaddress.ip_network(cidr, strict=False)
        cidr = str(net)
    except ValueError as e:
        return {"success": False, "error": f"Некорректный CIDR: {e}"}

    try:
        await db.execute(
            text("INSERT INTO selectel_subnets (cidr) VALUES (:cidr)"),
            {"cidr": cidr},
        )
        await db.commit()

        result = await db.execute(text(
            "SELECT id FROM selectel_subnets WHERE cidr = :cidr"
        ), {"cidr": cidr})
        row = result.fetchone()
        return {"success": True, "id": row[0] if row else None, "cidr": cidr}
    except Exception as e:
        error_msg = str(e).lower()
        if "unique" in error_msg or "duplicate" in error_msg:
            return {"success": False, "error": f"Подсеть {cidr} уже существует"}
        return {"success": False, "error": str(e)}


async def add_whitelist_cidrs(db: AsyncSession, cidrs: List[str]) -> Dict[str, Any]:
    """Пачка подсетей и/или одиночных IP за один запрос. Простой IP хранится как /32,
    новое ловится дедупликацией и ON CONFLICT, повторы не считаются ошибкой."""
    await SelectelService._ensure_tables()
    valid, invalid, seen = [], [], set()
    for raw in cidrs or []:
        s = (raw or "").strip()
        if not s:
            continue
        try:
            norm = str(ipaddress.ip_network(s, strict=False))
        except ValueError:
            invalid.append(s)
            continue
        if norm not in seen:
            seen.add(norm)
            valid.append(norm)

    added = 0
    if valid:
        params = {f"c{i}": c for i, c in enumerate(valid)}
        placeholders = ",".join(f"(:c{i})" for i in range(len(valid)))
        result = await db.execute(text(
            f"INSERT INTO selectel_subnets (cidr) VALUES {placeholders} ON CONFLICT (cidr) DO NOTHING"
        ), params)
        await db.commit()
        added = result.rowcount or 0

    return {"success": True, "added": added, "skipped": len(valid) - added, "invalid": invalid}


async def remove_whitelist_cidr(db: AsyncSession, subnet_id: int) -> Dict[str, Any]:
    await SelectelService._ensure_tables()
    try:
        result = await db.execute(
            text("DELETE FROM selectel_subnets WHERE id = :id"),
            {"id": subnet_id},
        )
        await db.commit()
        if result.rowcount == 0:
            return {"success": False, "error": f"Subnet id={subnet_id} not found"}
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def clear_whitelist(db: AsyncSession) -> Dict[str, Any]:
    await SelectelService._ensure_tables()
    result = await db.execute(text("DELETE FROM selectel_subnets"))
    await db.commit()
    return {"success": True, "deleted": result.rowcount or 0}
