"""
Yandex Cloud IP Service Module
Зеркало ExtService (backend_service.py) для Yandex Cloud API
"""

import os
import json
import asyncio
import logging
import subprocess
import time
import random
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from urllib.parse import urlparse

from app.services.ip_search import received_stats

RECEIVED_IPS = received_stats.ReceivedIpsStore("yc_received_ips")


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

YC_DEFAULT_ZONES = ["ru-central1-a", "ru-central1-b", "ru-central1-c", "ru-central1-d"]

YC_BUILTIN_SUBNETS = [
    "31.44.9.0/24",
    "51.250.48.0/24",
    "51.250.126.0/24",
    "84.201.128.0/18",
]

YC_IAM_URL = "https://iam.api.cloud.yandex.net/iam/v1/tokens"
YC_VPC_URL = "https://vpc.api.cloud.yandex.net/vpc/v1"
YC_OP_URL = "https://operation.api.cloud.yandex.net/operations"

# ═══════════════════════════════════════════════════════════════════════
# DB TABLES
# ═══════════════════════════════════════════════════════════════════════

TABLES_SQL = """
CREATE TABLE IF NOT EXISTS yc_accounts (
    id SERIAL PRIMARY KEY,
    label VARCHAR(255),
    oauth_token VARCHAR(500) NOT NULL,
    folder_id VARCHAR(100) NOT NULL,
    proxy VARCHAR(500),
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS yc_caught_ips (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL,
    ip_address VARCHAR(50) NOT NULL,
    address_id VARCHAR(100),
    zone VARCHAR(50),
    caught_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES yc_accounts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS yc_settings (
    id INTEGER PRIMARY KEY DEFAULT 1,
    delay_min INTEGER DEFAULT 5,
    delay_max INTEGER DEFAULT 15,
    start_delay_max INTEGER DEFAULT 30,
    stop_on_catch BOOLEAN DEFAULT TRUE,
    telegram_bot_token VARCHAR(500) DEFAULT '',
    telegram_chat_id VARCHAR(100) DEFAULT '',
    target_subnets TEXT DEFAULT '[]',
    zones TEXT DEFAULT '["ru-central1-b"]',
    proxy_required BOOLEAN DEFAULT FALSE,
    proxy_check_interval INTEGER DEFAULT 300,
    log_level VARCHAR(10) DEFAULT 'info',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS yc_custom_subnets (
    id SERIAL PRIMARY KEY,
    cidr VARCHAR(50) NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_yc_caught_ips_account ON yc_caught_ips(account_id);
""" + received_stats.received_ips_ddl("yc_received_ips", "yc_accounts")


# ═══════════════════════════════════════════════════════════════════════
# DB HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _resolve_target_cidrs(settings: Dict[str, Any]) -> List[str]:
    """Целевые подсети в CIDR: в настройках хранятся индексы доступных, пусто — все доступные."""
    all_subnets = settings.get("available_subnets", list(YC_BUILTIN_SUBNETS))
    subnet_indices = settings.get("target_subnets", [])
    resolved = [all_subnets[i] for i in subnet_indices if i < len(all_subnets)]
    return resolved or list(all_subnets)


async def _get_pg_columns(conn, table: str) -> set:
    result = await conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :table AND table_schema = 'public'"
    ), {"table": table})
    return {row[0] for row in result.fetchall()}


async def _table_exists(conn, table: str) -> bool:
    result = await conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = :table AND table_schema = 'public')"
    ), {"table": table})
    return result.scalar()


async def init_tables(engine):
    YcService._engine = engine
    async with engine.begin() as conn:
        for statement in TABLES_SQL.strip().split(';'):
            statement = statement.strip()
            if statement:
                try:
                    await conn.execute(text(statement))
                except Exception as e:
                    if "already exists" not in str(e).lower():
                        logger.debug(f"YC table creation note: {e}")

    logger.info("Yandex Cloud tables initialized")
    YcService._load_logs_from_file()


# ═══════════════════════════════════════════════════════════════════════
# ACCOUNT WORKER INFO
# ═══════════════════════════════════════════════════════════════════════

class AccountWorkerInfo:
    __slots__ = (
        'process', 'log_task', 'account_id', 'label', 'status',
        'error_type', 'error_message', 'started_at', 'attempts', 'caught', 'logs',
        'config_json', 'finish_reason',
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
            "pid": self.process.pid if is_alive else None,
        }


# ═══════════════════════════════════════════════════════════════════════
# YC SERVICE
# ═══════════════════════════════════════════════════════════════════════

class YcService:

    _account_workers: Dict[int, AccountWorkerInfo] = {}
    _logs: List[str] = []
    _max_logs: int = 1000
    _start_lock: asyncio.Lock = None
    _account_errors: List[Dict[str, Any]] = []
    _max_errors: int = 200
    _log_seq: int = 0

    LOG_FILE = Path("/app/data/yc_worker.log")
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

    _process: Optional[subprocess.Popen] = None
    _log_task: Optional[asyncio.Task] = None
    _status: str = "stopped"

    SCRIPT_DIR = Path("/app/ext")
    SCRIPT_PATH = SCRIPT_DIR / "yc_worker.py"

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
            cls._log_executor = ThreadPoolExecutor(max_workers=500, thread_name_prefix="yc-log-reader")
        return cls._log_executor

    # ─────────────────────────────────────────────────────────────────
    # ACCOUNTS
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    async def get_accounts(db: AsyncSession) -> List[Dict[str, Any]]:
        result = await db.execute(text(
            "SELECT id, label, oauth_token, folder_id, proxy, enabled, created_at FROM yc_accounts ORDER BY id"
        ))
        accounts = result.fetchall()

        has_caught_ips_table = False
        try:
            table_check = await db.execute(text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'yc_caught_ips' AND table_schema = 'public')"
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
                        "SELECT COUNT(*) FROM yc_caught_ips WHERE account_id = :aid"
                    ), {"aid": acc[0]})
                    ip_count = ip_result.scalar() or 0
                except Exception:
                    pass

            output.append({
                "id": acc[0],
                "label": acc[1] or "",
                "oauth_token": "****",
                "folder_id": acc[3],
                "proxy": acc[4] or "",
                "enabled": bool(acc[5]) if acc[5] is not None else True,
                "created_at": acc[6],
                "caught_ip_count": ip_count,
            })
        return output

    @staticmethod
    async def create_account(
        db: AsyncSession, oauth_token: str, folder_id: str,
        label: str = "", proxy: str = "",
    ) -> Dict[str, Any]:
        await db.execute(text(
            "INSERT INTO yc_accounts (label, oauth_token, folder_id, proxy, enabled) "
            "VALUES (:label, :oauth_token, :folder_id, :proxy, TRUE)"
        ), {"label": label or None, "oauth_token": oauth_token, "folder_id": folder_id, "proxy": proxy or None})
        await db.commit()

        result = await db.execute(text(
            "SELECT id, label, oauth_token, folder_id, proxy, enabled, created_at "
            "FROM yc_accounts WHERE oauth_token = :tok AND folder_id = :fid ORDER BY id DESC LIMIT 1"
        ), {"tok": oauth_token, "fid": folder_id})
        acc = result.fetchone()

        return {
            "id": acc[0],
            "label": acc[1] or "",
            "oauth_token": "****",
            "folder_id": acc[3],
            "proxy": acc[4] or "",
            "enabled": bool(acc[5]) if acc[5] is not None else True,
            "created_at": acc[6],
            "caught_ip_count": 0,
        }

    @staticmethod
    async def update_account(db: AsyncSession, account_id: int, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        updates = []
        params = {"id": account_id}

        field_map = {
            "label": "label",
            "oauth_token": "oauth_token",
            "folder_id": "folder_id",
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
            f"UPDATE yc_accounts SET {', '.join(updates)} WHERE id = :id"
        ), params)
        await db.commit()

        result = await db.execute(text(
            "SELECT id, label, oauth_token, folder_id, proxy, enabled, created_at FROM yc_accounts WHERE id = :id"
        ), {"id": account_id})
        acc = result.fetchone()
        if not acc:
            return None

        ip_result = await db.execute(text(
            "SELECT COUNT(*) FROM yc_caught_ips WHERE account_id = :aid"
        ), {"aid": acc[0]})
        ip_count = ip_result.scalar() or 0

        return {
            "id": acc[0],
            "label": acc[1] or "",
            "oauth_token": "****",
            "folder_id": acc[3],
            "proxy": acc[4] or "",
            "enabled": bool(acc[5]) if acc[5] is not None else True,
            "created_at": acc[6],
            "caught_ip_count": ip_count,
        }

    @staticmethod
    async def delete_account(db: AsyncSession, account_id: int) -> bool:
        result = await db.execute(text(
            "DELETE FROM yc_accounts WHERE id = :aid"
        ), {"aid": account_id})
        await db.commit()
        return result.rowcount > 0

    # ─────────────────────────────────────────────────────────────────
    # CAUGHT IPS
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    async def get_caught_ips(db: AsyncSession, account_id: int = None) -> List[Dict[str, Any]]:
        query = """SELECT c.id, c.account_id, c.ip_address, c.address_id, c.zone, c.caught_at, a.label
                   FROM yc_caught_ips c
                   LEFT JOIN yc_accounts a ON c.account_id = a.id"""
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
            "address_id": r[3],
            "zone": r[4],
            "caught_at": r[5],
            "account_label": r[6] or "",
        } for r in result.fetchall()]

    @staticmethod
    async def save_caught_ip(
        db: AsyncSession, account_id: int, ip_address: str,
        address_id: str = None, zone: str = None,
    ) -> Dict[str, Any]:
        await db.execute(text(
            "INSERT INTO yc_caught_ips (account_id, ip_address, address_id, zone) "
            "VALUES (:account_id, :ip_address, :address_id, :zone)"
        ), {
            "account_id": account_id,
            "ip_address": ip_address,
            "address_id": address_id,
            "zone": zone,
        })
        await db.commit()

        result = await db.execute(text(
            "SELECT id, caught_at FROM yc_caught_ips WHERE ip_address = :ip ORDER BY id DESC LIMIT 1"
        ), {"ip": ip_address})
        row = result.fetchone()

        return {
            "id": row[0],
            "account_id": account_id,
            "ip_address": ip_address,
            "address_id": address_id,
            "zone": zone,
            "caught_at": row[1],
        }

    @staticmethod
    async def delete_caught_ip(db: AsyncSession, ip_id: int) -> bool:
        result = await db.execute(text(
            "DELETE FROM yc_caught_ips WHERE id = :id"
        ), {"id": ip_id})
        await db.commit()
        return result.rowcount > 0

    @staticmethod
    async def clear_caught_ips(db: AsyncSession, account_id: int = None) -> int:
        if account_id is not None:
            result = await db.execute(text(
                "DELETE FROM yc_caught_ips WHERE account_id = :aid"
            ), {"aid": account_id})
        else:
            result = await db.execute(text("DELETE FROM yc_caught_ips"))
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
        return await RECEIVED_IPS.stats(db, account_id, _resolve_target_cidrs(await YcService.get_settings(db)))

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
        result = await db.execute(text(
            """SELECT delay_min, delay_max, start_delay_max, stop_on_catch,
                      telegram_bot_token, telegram_chat_id, target_subnets, zones,
                      proxy_required, proxy_check_interval, log_level
               FROM yc_settings WHERE id = 1"""
        ))
        row = result.fetchone()

        if not row:
            await db.execute(text("INSERT INTO yc_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING"))
            await db.commit()
            return await YcService.get_settings(db)

        target_subnets_raw = row[6]
        zones_raw = row[7]

        try:
            target_subnets = json.loads(target_subnets_raw) if target_subnets_raw else []
        except (json.JSONDecodeError, TypeError):
            target_subnets = []

        try:
            zones = json.loads(zones_raw) if zones_raw else ["ru-central1-b"]
        except (json.JSONDecodeError, TypeError):
            zones = ["ru-central1-b"]

        custom_subnets = await get_custom_subnets(db)
        all_subnets = list(YC_BUILTIN_SUBNETS) + [s["cidr"] for s in custom_subnets]

        return {
            "delay_min": row[0] if row[0] is not None else 5,
            "delay_max": row[1] if row[1] is not None else 15,
            "start_delay_max": row[2] if row[2] is not None else 30,
            "stop_on_catch": bool(row[3]) if row[3] is not None else True,
            "telegram_bot_token": row[4] or "",
            "telegram_chat_id": row[5] or "",
            "target_subnets": target_subnets,
            "zones": zones,
            "proxy_required": bool(row[8]) if row[8] is not None else False,
            "proxy_check_interval": row[9] if row[9] is not None else 300,
            "log_level": row[10] if row[10] else "info",
            "available_subnets": all_subnets,
            "available_zones": list(YC_DEFAULT_ZONES),
        }

    @staticmethod
    async def update_settings(db: AsyncSession, data: Dict[str, Any]) -> Dict[str, Any]:
        await db.execute(text("INSERT INTO yc_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING"))

        updates = []
        params = {}

        field_map = {
            "delay_min": "delay_min",
            "delay_max": "delay_max",
            "start_delay_max": "start_delay_max",
            "stop_on_catch": "stop_on_catch",
            "telegram_bot_token": "telegram_bot_token",
            "telegram_chat_id": "telegram_chat_id",
            "proxy_required": "proxy_required",
            "proxy_check_interval": "proxy_check_interval",
            "log_level": "log_level",
        }

        boolean_fields = {"stop_on_catch", "proxy_required"}

        for key, col in field_map.items():
            if key in data:
                updates.append(f"{col} = :{key}")
                params[key] = bool(data[key]) if key in boolean_fields else data[key]

        if "target_subnets" in data:
            updates.append("target_subnets = :target_subnets")
            params["target_subnets"] = json.dumps(data["target_subnets"])

        if "zones" in data:
            updates.append("zones = :zones")
            params["zones"] = json.dumps(data["zones"])

        updates.append("updated_at = CURRENT_TIMESTAMP")

        if updates:
            await db.execute(text(
                f"UPDATE yc_settings SET {', '.join(updates)} WHERE id = 1"
            ), params)
            await db.commit()

        return await YcService.get_settings(db)

    # ─────────────────────────────────────────────────────────────────
    # WORKER CONTROL
    # ─────────────────────────────────────────────────────────────────

    @classmethod
    async def _get_account_config_json(cls, db: AsyncSession, account_id: int) -> Optional[str]:
        acc_result = await db.execute(text(
            "SELECT id, label, oauth_token, folder_id, proxy FROM yc_accounts WHERE id = :id AND enabled = TRUE"
        ), {"id": account_id})
        acc = acc_result.fetchone()
        if not acc:
            return None

        settings = await cls.get_settings(db)

        config_data = {
            "account_id": acc[0],
            "label": acc[1] or f"account-{acc[0]}",
            "oauth_token": acc[2],
            "folder_id": acc[3],
            "proxy": acc[4] or None,
            "target_subnets": _resolve_target_cidrs(settings),
            "zones": settings.get("zones", ["ru-central1-b"]),
            "panel_base_url": "http://localhost:8000/_int",
            "config": {
                "delay_min": settings.get("delay_min", 5),
                "delay_max": settings.get("delay_max", 15),
                "start_delay_max": settings.get("start_delay_max", 30),
                "stop_on_catch": settings.get("stop_on_catch", True),
                "proxy_required": settings.get("proxy_required", False),
                "proxy_check_interval": settings.get("proxy_check_interval", 300),
                "log_level": settings.get("log_level", "info"),
                "telegram_bot_token": settings.get("telegram_bot_token", ""),
                "telegram_chat_id": settings.get("telegram_chat_id", ""),
            },
        }

        return json.dumps(config_data, ensure_ascii=False)

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
            cls._append_log(f"{ts} [SYSTEM] Started YC account {account_id} ({label}) PID={process.pid}")
            logger.info(f"YC account {account_id} ({label}) started with PID {process.pid}")

            return {"success": True, "pid": process.pid, "account_id": account_id}
        except Exception as e:
            logger.error(f"Failed to start YC account {account_id}: {e}")
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
                cls._append_log(f"{ts} [SYSTEM] Stopped YC account {account_id} ({info.label}) PID={pid}")

                logger.info(f"YC account {account_id} ({info.label}) stopped")
                return {"success": True}

            except Exception as e:
                logger.error(f"Failed to stop YC account {account_id}: {e}")
                return {"success": False, "error": str(e)}

    @classmethod
    async def start_worker(cls, db: AsyncSession) -> Dict[str, Any]:
        result = await db.execute(text(
            "SELECT id FROM yc_accounts WHERE enabled = TRUE ORDER BY id"
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
    async def stop_worker(cls, grace_seconds: float = 3) -> Dict[str, Any]:
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
        cls._append_log(f"{ts} [SYSTEM] Stopped all YC workers ({len(alive)} accounts)")
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
    # LOG READER
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
            logger.error(f"YC log reader error for account {account_id}: {e}")
        finally:
            exit_code = process.returncode if process else "?"
            ts = datetime.now().strftime("%H:%M:%S")
            info = cls._account_workers.get(account_id)
            label = info.label if info else "?"
            reason = info.finish_reason if info else None

            if reason:
                cls._append_log(
                    f"{ts} [SYSTEM] YC account {account_id} ({label}) "
                    f"exited (code={exit_code}, reason={reason})"
                )
            else:
                cls._append_log(
                    f"{ts} [SYSTEM] YC account {account_id} ({label}) "
                    f"died unexpectedly (exit_code={exit_code})"
                )
            logger.info(f"YC account {account_id} process ended: exit_code={exit_code}, reason={reason}")

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
            logger.info(f"YC IP caught for account {account_id}: {event.get('ip')}")

        elif event_type == "error":
            error_type = event.get("error_type", "UNKNOWN")
            error_msg = event.get("message", "")

            if info:
                info.error_type = error_type
                info.error_message = error_msg
                if error_type in ("QUOTA_EXCEEDED", "ACCOUNT_BLOCKED"):
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

            logger.warning(f"YC worker error for account {account_id}: {error_type} - {error_msg}")

        elif event_type == "proxy_waiting":
            if info:
                info.status = "proxy_waiting"
                info.error_type = "PROXY_DEAD"
            logger.warning(f"YC account {account_id}: proxy down, waiting for recovery")

        elif event_type == "proxy_recovered":
            if info:
                info.status = "running"
                info.error_type = None
                info.error_message = None
            for e in cls._account_errors:
                if e["account_id"] == account_id and e["error_type"] == "PROXY_DEAD" and not e.get("resolved"):
                    e["resolved"] = True
            logger.info(f"YC account {account_id}: proxy recovered")

        elif event_type == "status":
            if info:
                info.attempts = event.get("attempts", info.attempts)
                info.caught = event.get("caught", info.caught)

        elif event_type == "account_finished":
            reason = event.get("reason", "")
            caught = event.get("caught", 0)
            if info:
                info.attempts = event.get("attempts", info.attempts)
                info.caught = event.get("caught", info.caught)
                info.status = "stopped"
                info.finish_reason = reason

            no_restart_reasons = ("ALL_ZONES_EXHAUSTED", "PROXY_FAILED", "NO_PROXY", "FATAL_CRASH", "QUOTA_EXCEEDED")
            if reason in no_restart_reasons or caught > 0:
                cls._desired_running.discard(account_id)

            logger.info(f"YC account {account_id} finished: {reason}")

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
            "ALL_ZONES_EXHAUSTED", "PROXY_FAILED", "NO_PROXY", "FATAL_CRASH", "QUOTA_EXCEEDED",
        })
        _NO_RESTART_ERRORS = frozenset({
            "QUOTA_EXCEEDED", "ACCOUNT_BLOCKED",
        })

        while True:
            await asyncio.sleep(cls._WATCHDOG_INTERVAL)

            if not cls._desired_running:
                continue

            try:
                await cls._watchdog_tick(_NO_RESTART_REASONS, _NO_RESTART_ERRORS)
            except Exception as e:
                logger.error(f"YC watchdog tick error: {e}", exc_info=True)
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
                    f"{ts} [WATCHDOG] YC account {account_id} ({info.label}): "
                    f"{info.finish_reason}, not restarting"
                )
                cls._desired_running.discard(account_id)
                continue

            if info.error_type in _NO_RESTART_ERRORS:
                ts = datetime.now().strftime("%H:%M:%S")
                cls._append_log(
                    f"{ts} [WATCHDOG] YC account {account_id} ({info.label}): "
                    f"error {info.error_type}, not restarting"
                )
                cls._desired_running.discard(account_id)
                continue

            if info.caught > 0:
                ts = datetime.now().strftime("%H:%M:%S")
                cls._append_log(
                    f"{ts} [WATCHDOG] YC account {account_id} ({info.label}): "
                    f"caught {info.caught} IPs, not restarting"
                )
                cls._desired_running.discard(account_id)
                continue

            count = cls._restart_counts.get(account_id, 0)
            if count >= cls._MAX_RESTARTS:
                ts = datetime.now().strftime("%H:%M:%S")
                cls._append_log(
                    f"{ts} [WATCHDOG] YC account {account_id} ({info.label}): "
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
                    logger.warning(f"YC watchdog: DB refresh failed for account {account_id}: {e}")

            if not config_json:
                config_json = info.config_json

            if not config_json:
                ts = datetime.now().strftime("%H:%M:%S")
                cls._append_log(
                    f"{ts} [WATCHDOG] YC account {account_id}: no config available, cannot restart"
                )
                cls._desired_running.discard(account_id)
                continue

            if info.log_task and not info.log_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(info.log_task), timeout=10.0)
                except asyncio.TimeoutError:
                    logger.warning(f"YC watchdog: log_task for account {account_id} still running, skipping this tick")
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

            if info.caught > 0:
                cls._desired_running.discard(account_id)
                continue

            exit_code = info.process.returncode if info.process else "?"
            cls._restart_counts[account_id] = count + 1

            ts = datetime.now().strftime("%H:%M:%S")
            cls._append_log(
                f"{ts} [WATCHDOG] YC account {account_id} ({info.label}) "
                f"died (exit={exit_code}), restarting #{count + 1}..."
            )

            result = await cls._launch_worker(account_id, info.label, config_json)
            ts = datetime.now().strftime("%H:%M:%S")
            if result.get("success"):
                cls._append_log(
                    f"{ts} [WATCHDOG] YC account {account_id} restarted OK "
                    f"(PID={result['pid']})"
                )
                logger.info(f"YC watchdog restarted account {account_id} (PID={result['pid']})")
            else:
                cls._append_log(
                    f"{ts} [WATCHDOG] YC account {account_id} restart FAILED: "
                    f"{result.get('error')}"
                )
                logger.error(f"YC watchdog failed to restart account {account_id}: {result.get('error')}")

    # ─────────────────────────────────────────────────────────────────
    # STATUS
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
    def get_account_errors(cls, account_id: int = None) -> List[Dict[str, Any]]:
        if account_id is not None:
            return [e for e in cls._account_errors if e["account_id"] == account_id and not e.get("resolved")]
        return [e for e in cls._account_errors if not e.get("resolved")]

    @classmethod
    def clear_account_errors(cls, account_id: int = None):
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
            logger.info(f"YC log file rotated, kept {len(lines)} lines")
        except Exception as e:
            logger.warning(f"YC log rotation failed: {e}")

    @classmethod
    def _load_logs_from_file(cls):
        try:
            lines = cls._tail_file(cls._max_logs)
            if lines:
                cls._logs = lines
                cls._log_seq = len(lines)
                logger.info(f"Restored {len(lines)} YC log entries from {cls.LOG_FILE}")
        except Exception as e:
            logger.warning(f"Failed to load YC logs from file: {e}")

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
    # MANUAL IP OBTAINMENT (Yandex Cloud API — blocking)
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    async def manual_obtain_ip(
        db: AsyncSession,
        account_id: int,
        zone: str = "ru-central1-b",
    ) -> Dict[str, Any]:
        acc_result = await db.execute(text(
            "SELECT id, label, oauth_token, folder_id, proxy FROM yc_accounts WHERE id = :id"
        ), {"id": account_id})
        acc = acc_result.fetchone()
        if not acc:
            return {"success": False, "error": "Аккаунт не найден"}

        oauth_token = acc[2]
        folder_id = acc[3]
        proxy_str = acc[4] or None

        if zone not in YC_DEFAULT_ZONES:
            return {"success": False, "error": f"Неизвестная зона: {zone}"}

        def _do_obtain() -> Dict[str, Any]:
            session = _yc_create_session(proxy_str)
            try:
                iam_token, iam_err = _yc_get_iam_token(session, oauth_token)
                if not iam_token:
                    return {"success": False, "error": f"IAM-токен: {iam_err}"}

                op_id, reserve_err = _yc_reserve_address(session, iam_token, folder_id, zone)
                if not op_id:
                    return {"success": False, "error": f"Резервирование: {reserve_err}"}

                op_result, poll_err = _yc_wait_operation(session, iam_token, op_id)
                if not op_result:
                    return {"success": False, "error": f"Операция: {poll_err}"}

                ip_address = op_result.get("externalIpv4Address", {}).get("address", "")
                address_id = op_result.get("id", "")

                if not ip_address:
                    return {"success": False, "error": "IP не найден в ответе операции"}

                return {
                    "success": True,
                    "ip_address": ip_address,
                    "address_id": address_id,
                    "zone": zone,
                }
            finally:
                session.close()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _do_obtain)


# ═══════════════════════════════════════════════════════════════════════
# YANDEX CLOUD API HELPERS (blocking requests)
# ═══════════════════════════════════════════════════════════════════════

def _yc_create_session(proxy_str: Optional[str] = None) -> requests.Session:
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


def _yc_get_iam_token(
    session: requests.Session, oauth_token: str, timeout: float = 30,
) -> tuple:
    """(iam_token_or_none, error_or_none)"""
    try:
        resp = session.post(
            YC_IAM_URL,
            json={"yandexPassportOauthToken": oauth_token},
            timeout=timeout,
        )

        if resp.status_code == 200:
            iam_token = resp.json().get("iamToken")
            if iam_token:
                return iam_token, None
            return None, "iamToken отсутствует в ответе"

        if resp.status_code == 401:
            return None, "Неверный OAuth-токен"

        try:
            detail = resp.json().get("message", resp.text[:200])
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


def _yc_reserve_address(
    session: requests.Session, iam_token: str, folder_id: str, zone: str,
    timeout: float = 30,
) -> tuple:
    """(operation_id_or_none, error_or_none)"""
    name = f"ip-pick-{random.randint(1000, 9999)}"
    body = {
        "folderId": folder_id,
        "name": name,
        "externalIpv4AddressSpec": {
            "zoneId": zone,
        },
    }

    headers = {"Authorization": f"Bearer {iam_token}"}

    try:
        resp = session.post(
            f"{YC_VPC_URL}/addresses",
            json=body,
            headers=headers,
            timeout=timeout,
        )

        if resp.status_code == 200:
            op_id = resp.json().get("id")
            if op_id:
                return op_id, None
            return None, "operation id отсутствует в ответе"

        try:
            detail = resp.json().get("message", resp.text[:200])
        except Exception:
            detail = resp.text[:200]

        if resp.status_code == 401:
            return None, "IAM-токен истёк или невалиден"
        if resp.status_code == 403:
            return None, f"ACCESS_DENIED: {detail}"
        if resp.status_code == 429:
            return None, f"RATE_LIMITED: {detail}"
        return None, f"HTTP_{resp.status_code}: {detail}"

    except requests.exceptions.ProxyError as e:
        return None, f"PROXY_ERROR: {str(e)[:80]}"
    except requests.exceptions.Timeout:
        return None, "TIMEOUT: таймаут запроса"
    except requests.exceptions.ConnectionError as e:
        return None, f"CONNECTION_ERROR: {str(e)[:80]}"
    except Exception as e:
        return None, f"ERROR: {str(e)[:80]}"


def _yc_wait_operation(
    session: requests.Session, iam_token: str, operation_id: str,
    timeout: float = 30, max_attempts: int = 30, poll_interval: float = 2.0,
) -> tuple:
    """(response_dict_or_none, error_or_none) — polling операции до done == true"""
    headers = {"Authorization": f"Bearer {iam_token}"}
    url = f"{YC_OP_URL}/{operation_id}"

    for _ in range(max_attempts):
        try:
            resp = session.get(url, headers=headers, timeout=timeout)

            if resp.status_code != 200:
                try:
                    detail = resp.json().get("message", resp.text[:200])
                except Exception:
                    detail = resp.text[:200]
                return None, f"HTTP_{resp.status_code}: {detail}"

            data = resp.json()

            if data.get("done"):
                if "error" in data:
                    err = data["error"]
                    return None, f"Операция завершилась ошибкой: {err.get('message', str(err))}"
                return data.get("response", {}), None

        except requests.exceptions.Timeout:
            return None, "TIMEOUT при polling операции"
        except requests.exceptions.ConnectionError as e:
            return None, f"CONNECTION_ERROR: {str(e)[:80]}"
        except Exception as e:
            return None, f"ERROR: {str(e)[:80]}"

        time.sleep(poll_interval)

    return None, f"Операция не завершилась за {max_attempts * poll_interval}s"


def _yc_release_address(
    session: requests.Session, iam_token: str, address_id: str,
    timeout: float = 30,
) -> tuple:
    """(ok, error_or_none)"""
    headers = {"Authorization": f"Bearer {iam_token}"}

    try:
        resp = session.delete(
            f"{YC_VPC_URL}/addresses/{address_id}",
            headers=headers,
            timeout=timeout,
        )

        if resp.status_code == 200:
            return True, None

        try:
            detail = resp.json().get("message", resp.text[:200])
        except Exception:
            detail = resp.text[:200]
        return False, f"HTTP_{resp.status_code}: {detail}"

    except requests.exceptions.Timeout:
        return False, "TIMEOUT"
    except Exception as e:
        return False, f"ERROR: {str(e)[:80]}"


# ═══════════════════════════════════════════════════════════════════════
# CUSTOM SUBNETS
# ═══════════════════════════════════════════════════════════════════════

async def get_all_subnets(db: AsyncSession) -> List[str]:
    merged = list(YC_BUILTIN_SUBNETS)
    try:
        result = await db.execute(text("SELECT cidr FROM yc_custom_subnets ORDER BY id"))
        for row in result.fetchall():
            if row[0] not in merged:
                merged.append(row[0])
    except Exception:
        pass
    return merged


async def get_custom_subnets(db: AsyncSession) -> List[Dict[str, Any]]:
    try:
        result = await db.execute(text("SELECT id, cidr, created_at FROM yc_custom_subnets ORDER BY id"))
        return [{"id": r[0], "cidr": r[1], "created_at": r[2].isoformat() if r[2] else None} for r in result.fetchall()]
    except Exception:
        return []


async def add_custom_subnet(db: AsyncSession, cidr: str) -> Dict[str, Any]:
    cidr = cidr.strip()
    if cidr in YC_BUILTIN_SUBNETS:
        return {"success": False, "error": f"{cidr} is a built-in subnet"}

    try:
        await db.execute(
            text("INSERT INTO yc_custom_subnets (cidr) VALUES (:cidr)"),
            {"cidr": cidr},
        )
        await db.commit()

        result = await db.execute(text(
            "SELECT id FROM yc_custom_subnets WHERE cidr = :cidr"
        ), {"cidr": cidr})
        row = result.fetchone()
        return {"success": True, "id": row[0] if row else None, "cidr": cidr}
    except Exception as e:
        error_msg = str(e).lower()
        if "unique" in error_msg or "duplicate" in error_msg:
            return {"success": False, "error": f"Подсеть {cidr} уже существует"}
        return {"success": False, "error": str(e)}


async def remove_custom_subnet(db: AsyncSession, subnet_id: int) -> Dict[str, Any]:
    try:
        result = await db.execute(text("DELETE FROM yc_custom_subnets WHERE id = :id"), {"id": subnet_id})
        await db.commit()
        if result.rowcount == 0:
            return {"success": False, "error": f"Subnet id={subnet_id} not found"}
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
