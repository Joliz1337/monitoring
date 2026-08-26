"""System management endpoints - version info, updates, SSL certificate"""

import asyncio
import logging
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import docker
import psutil
from app.services.http_client import get_node_client, get_external_client, node_auth_headers
from docker.errors import DockerException, ImageNotFound
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.config import get_settings
from app.database import get_db, async_session
from app.models import Server, PanelSettings
from app.services import update_channel
from app.services.net_utils import resolve_host
from app.services.panel_host_metrics import HostHistoryPeriod, load_host_history
from app.services.wildcard_ssl import USE_FOR_PANEL_SETTING

router = APIRouter(prefix="/system", tags=["system"])
logger = logging.getLogger(__name__)

NODE_VERSIONS_CACHE_TTL_SEC = 30.0
NODE_NIC_INFO_CACHE_TTL_SEC = 20.0

# Ссылка подставляется в текст shell-скрипта, который апдейтер исполняет в
# privileged-контейнере с docker.sock — то есть это прямой путь к произвольной
# команде на хосте панели. В наборе символов нет ни метасимвола shell, ни пробела;
# ведущий символ алфавитно-цифровой, иначе ссылка сошла бы у git за опцию вида
# --upload-pack=. Кавычки в самом скрипте — второй рубеж, не замена проверке.
GIT_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"

_node_versions_cache: dict[int, tuple[float, Optional[dict]]] = {}
_node_versions_locks: dict[int, asyncio.Lock] = {}

_node_nic_info_cache: dict[int, tuple[float, Any]] = {}
_node_nic_info_locks: dict[int, asyncio.Lock] = {}


def _get_per_server_lock(locks: dict[int, asyncio.Lock], server_id: int) -> asyncio.Lock:
    lock = locks.get(server_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[server_id] = lock
    return lock


def invalidate_node_versions_cache(server_id: int) -> None:
    """Сбросить кэш версий конкретной ноды (вызывать после apply/remove)."""
    _node_versions_cache.pop(server_id, None)


def invalidate_node_nic_info_cache(server_id: int) -> None:
    """Сбросить кэш nic-info конкретной ноды (вызывать после apply/remove)."""
    _node_nic_info_cache.pop(server_id, None)


def invalidate_node_cache(server_id: int) -> None:
    """Сбросить весь per-node кэш (версии + nic-info) — например, после apply/remove."""
    invalidate_node_versions_cache(server_id)
    invalidate_node_nic_info_cache(server_id)


async def get_panel_ip() -> str | None:
    """Get panel's IP address by resolving the configured domain"""
    settings = get_settings()
    domain = settings.domain

    if not domain:
        return None

    ip = await resolve_host(domain)
    if ip is None:
        logger.warning(f"Failed to resolve domain: {domain}")
    return ip


@router.get("/panel-ip")
async def get_panel_ip_endpoint(_: dict = Depends(verify_auth)):
    """Get panel's IP address"""
    ip = await get_panel_ip()
    settings = get_settings()
    return {
        "ip": ip,
        "domain": settings.domain
    }

VERSION_FILE = Path("/app/VERSION")
UPDATER_CONTAINER_NAME = "panel-updater"
UPDATER_IMAGE = "docker:cli"


# URL строятся на каждый запрос: базовая ветка зависит от выбранного канала
# обновлений (main/dev) и может меняться в рантайме через настройки панели.
def _github_panel_version_url() -> str:
    return f"{update_channel.github_raw_base()}/panel/VERSION"


def _github_node_version_url() -> str:
    return f"{update_channel.github_raw_base()}/node/VERSION"


def _github_configs_url(filename: str) -> str:
    return f"{update_channel.github_configs_base()}/{filename}"

# Node agents older than this cannot run the renderer: they would write the
# base files verbatim, leaving literal @@TOKEN@@ lines that sysctl rejects.
MIN_NODE_VERSION_FOR_RENDER = "10.6.0"


def _profiles_url(filename: str) -> str:
    """Build GitHub URL for a file under configs/profiles/."""
    return _github_configs_url(f"profiles/{filename}")


def _version_tuple(v: str) -> tuple:
    """Parse a dotted version into a comparable tuple; unknown sorts lowest."""
    parts = []
    for chunk in (v or "").strip().split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def node_supports_renderer(node_version: str) -> bool:
    """True if the node agent is new enough to run tune-sysctl.sh itself."""
    if not node_version:
        return False
    return _version_tuple(node_version) >= _version_tuple(MIN_NODE_VERSION_FOR_RENDER)

_update_status = {
    "in_progress": False,
    "last_result": None,
    "last_error": None,
    "last_update_time": None
}

_cert_renewal_status = {
    "in_progress": False,
    "last_result": None,
    "last_error": None,
    "output": None,
    "started_at": None,
    "completed_at": None
}


def get_current_version() -> str:
    """Read current version from VERSION file"""
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text().strip()
    return "unknown"


async def get_latest_version_from_github() -> Optional[str]:
    """Fetch latest panel version from panel/VERSION file on GitHub"""
    try:
        client = get_external_client()
        response = await client.get(_github_panel_version_url(), timeout=10.0)

        if response.status_code == 200:
            version = response.text.strip()
            return version if version else None

        return None
    except Exception as e:
        logger.error(f"Failed to fetch latest version from GitHub: {e}")
        return None


async def get_latest_node_version_from_github() -> Optional[str]:
    """Fetch latest node version from node/VERSION file on GitHub"""
    try:
        client = get_external_client()
        response = await client.get(_github_node_version_url(), timeout=10.0)

        if response.status_code == 200:
            version = response.text.strip()
            return version if version else None

        return None
    except Exception as e:
        logger.error(f"Failed to fetch latest node version from GitHub: {e}")
        return None


async def get_latest_optimizations_version_from_github() -> Optional[str]:
    """Fetch latest optimizations version from configs/VERSION file on GitHub"""
    try:
        client = get_external_client()
        response = await client.get(_github_configs_url("VERSION"), timeout=10.0)

        if response.status_code == 200:
            version = response.text.strip()
            return version if version else None

        return None
    except Exception as e:
        logger.error(f"Failed to fetch latest optimizations version from GitHub: {e}")
        return None


async def _fetch_node_all_versions(server: Server) -> Optional[dict]:
    """Реальный запрос к ноде — без кэша."""
    try:
        client = get_node_client(server)
        headers = node_auth_headers(server)
        response = await client.get(
            f"{server.url}/api/system/versions",
            headers=headers,
            timeout=10.0,
        )

        if response.status_code == 200:
            return response.json()

        response = await client.get(
            f"{server.url}/api/version",
            headers=headers,
            timeout=10.0,
        )

        if response.status_code == 200:
            data = response.json()
            return {
                "node_version": data.get("version"),
                "optimizations": {"installed": False, "version": None}
            }

        return None
    except Exception as e:
        logger.debug(f"Failed to get versions from {server.url}: {e}")
        return None


async def get_node_all_versions(server: Server) -> Optional[dict]:
    """
    Получить версию ноды + статус оптимизаций одним запросом.
    Кэш per-server TTL=20s — стабилизирует повторные Refresh'и страницы Оптимизаций.
    Per-server lock не блокирует другие ноды, но защищает от дублирующих запросов к одной.
    """
    now = time.monotonic()
    cached = _node_versions_cache.get(server.id)
    if cached and now < cached[0]:
        return cached[1]

    lock = _get_per_server_lock(_node_versions_locks, server.id)
    async with lock:
        cached = _node_versions_cache.get(server.id)
        now = time.monotonic()
        if cached and now < cached[0]:
            return cached[1]

        data = await _fetch_node_all_versions(server)
        _node_versions_cache[server.id] = (
            time.monotonic() + NODE_VERSIONS_CACHE_TTL_SEC,
            data,
        )
        return data


async def get_cached_nic_info(server_id: int, fetcher) -> Any:
    """
    Кэш per-server для nic-info; fetcher — async-функция, возвращающая значение.
    Используется из proxy.py чтобы повторные Refresh'и не дёргали ноду чаще, чем раз в 20 сек.
    """
    now = time.monotonic()
    cached = _node_nic_info_cache.get(server_id)
    if cached and now < cached[0]:
        return cached[1]

    lock = _get_per_server_lock(_node_nic_info_locks, server_id)
    async with lock:
        cached = _node_nic_info_cache.get(server_id)
        now = time.monotonic()
        if cached and now < cached[0]:
            return cached[1]

        data = await fetcher()
        _node_nic_info_cache[server_id] = (
            time.monotonic() + NODE_NIC_INFO_CACHE_TTL_SEC,
            data,
        )
        return data


def get_docker_client():
    """Get Docker client via socket"""
    try:
        return docker.from_env()
    except DockerException as e:
        logger.error(f"Failed to connect to Docker: {e}")
        raise


@router.get("/version/base")
async def get_version_base(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """
    Быстрый эндпоинт: панель + GitHub-версии + список нод из БД (без запросов к нодам).
    Загружается мгновенно, ноды приходят без version/status — их грузит фронт поштучно.
    """
    current_version = get_current_version()

    result = await db.execute(
        select(Server).where(Server.is_active == True).order_by(Server.position)
    )
    servers = result.scalars().all()

    latest_version, latest_node_version, latest_opt_version = await asyncio.gather(
        get_latest_version_from_github(),
        get_latest_node_version_from_github(),
        get_latest_optimizations_version_from_github(),
    )

    panel_update_available = False
    if latest_version and current_version != "unknown":
        panel_update_available = latest_version != current_version

    nodes = [
        {"id": s.id, "name": s.name, "url": s.url}
        for s in servers
    ]

    return {
        "panel": {
            "version": current_version,
            "latest_version": latest_version,
            "update_available": panel_update_available,
        },
        "node": {"latest_version": latest_node_version},
        "optimizations": {"latest_version": latest_opt_version},
        "nodes": nodes,
        "update_in_progress": _update_status["in_progress"],
    }


@router.get("/nodes/{node_id}/version")
async def get_single_node_version(
    node_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Получить версию одной ноды (SSH-запрос к серверу)."""
    result = await db.execute(
        select(Server).where(Server.id == node_id, Server.is_active == True)
    )
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    versions_data = await get_node_all_versions(server)
    is_online = versions_data is not None

    return {
        "id": server.id,
        "name": server.name,
        "url": server.url,
        "version": versions_data.get("node_version") if versions_data else None,
        "status": "online" if is_online else "offline",
        "optimizations": (
            versions_data.get("optimizations", {"installed": False, "version": None})
            if versions_data
            else {"installed": False, "version": None}
        ),
    }


async def run_panel_update_in_container(target_ref: str | None = None):
    """
    Run panel update in separate Docker container.

    Args:
        target_ref: Git reference (branch/tag/commit). Default: выбранный канал обновлений.
    """
    
    _update_status["in_progress"] = True
    _update_status["last_error"] = None
    
    try:
        client = await asyncio.to_thread(get_docker_client)

        # Remove old updater container if exists
        try:
            old_container = await asyncio.to_thread(
                client.containers.get, UPDATER_CONTAINER_NAME
            )
            await asyncio.to_thread(old_container.remove, force=True)
            logger.info("Removed old updater container")
        except docker.errors.NotFound:
            pass

        # Pull docker:cli image if needed
        try:
            await asyncio.to_thread(client.images.get, UPDATER_IMAGE)
        except ImageNotFound:
            logger.info(f"Pulling {UPDATER_IMAGE}...")
            await asyncio.to_thread(client.images.pull, UPDATER_IMAGE)
        
        ref_arg = target_ref if target_ref else update_channel.current_branch()
        logger.info(f"Starting panel update to: {ref_arg}")
        
        # Updater script:
        # 1. Install dependencies
        # 2. Detect country and select mirror
        # 3. Clone fresh repo with fallback mirrors
        # 4. Run update.sh from cloned repo
        updater_script = f"""#!/bin/sh
set -e

echo "[INFO] Installing dependencies..."
apk add --no-cache git curl rsync bash gettext >/dev/null 2>&1

echo "[INFO] Installing Docker Compose..."
mkdir -p /usr/local/lib/docker/cli-plugins
COMPOSE_URL="https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64"
COMPOSE_BIN="/usr/local/lib/docker/cli-plugins/docker-compose"
COMPOSE_OK=0

# Try GitHub first (30s timeout for large binary)
echo "  Trying GitHub (30s timeout)..."
if curl -fsSL --connect-timeout 10 --max-time 30 -o "$COMPOSE_BIN" "$COMPOSE_URL" 2>/dev/null && \
   [ -f "$COMPOSE_BIN" ] && chmod +x "$COMPOSE_BIN" && "$COMPOSE_BIN" version >/dev/null 2>&1; then
    echo "  Docker Compose installed"
    COMPOSE_OK=1
else
    # Try mirror with longer timeout
    echo "  Trying mirror (ghfast.top)..."
    rm -f "$COMPOSE_BIN" 2>/dev/null
    if curl -fsSL --connect-timeout 10 --max-time 120 -o "$COMPOSE_BIN" "https://ghfast.top/$COMPOSE_URL" 2>/dev/null && \
       [ -f "$COMPOSE_BIN" ] && chmod +x "$COMPOSE_BIN" && "$COMPOSE_BIN" version >/dev/null 2>&1; then
        echo "  Docker Compose installed"
        COMPOSE_OK=1
    fi
fi
[ $COMPOSE_OK -eq 0 ] && echo "  WARNING: Docker Compose installation failed"

echo "[INFO] Cloning repository..."

# GitHub mirror
GITHUB_MIRROR="https://ghfast.top"

TMP_CLONE=/tmp/monitoring-fresh
rm -rf $TMP_CLONE
CLONE_SUCCESS=0

echo "[INFO] Trying GitHub (30s timeout)..."
if timeout 30 git clone --depth 1 --branch "{ref_arg}" "https://github.com/Joliz1337/monitoring.git" $TMP_CLONE 2>&1; then
    CLONE_SUCCESS=1
else
    rm -rf $TMP_CLONE
    echo "[WARN] GitHub timeout/error, trying mirror (ghfast.top)..."
    if timeout 120 git clone --depth 1 --branch "{ref_arg}" "$GITHUB_MIRROR/https://github.com/Joliz1337/monitoring.git" $TMP_CLONE 2>&1; then
        CLONE_SUCCESS=1
    fi
fi

if [ $CLONE_SUCCESS -eq 0 ]; then
    echo "[ERROR] Failed to clone repository"
    exit 1
fi

echo "[INFO] Running update script..."
chmod +x $TMP_CLONE/panel/update.sh
bash $TMP_CLONE/panel/update.sh "{ref_arg}"

echo "[INFO] Cleanup..."
rm -rf $TMP_CLONE

echo "[SUCCESS] Panel update completed!"
"""
        
        container = await asyncio.to_thread(
            client.containers.run,
            image=UPDATER_IMAGE,
            command=["sh", "-c", updater_script],
            name=UPDATER_CONTAINER_NAME,
            volumes={
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
                "/opt/monitoring-panel": {"bind": "/opt/monitoring-panel", "mode": "rw"},
                "/etc/letsencrypt": {"bind": "/etc/letsencrypt", "mode": "ro"},
            },
            network_mode="host",
            privileged=True,
            detach=True,
            remove=False,
        )
        
        logger.info(f"Panel updater started: {container.id[:12]}")
        
        # Wait for completion (10 min timeout)
        result = await asyncio.to_thread(container.wait, timeout=600)

        exit_code = result.get("StatusCode", -1)
        logs = (await asyncio.to_thread(container.logs)).decode("utf-8", errors="replace")
        
        if exit_code == 0:
            _update_status["last_result"] = "success"
            _update_status["last_update_time"] = datetime.now().isoformat()
            logger.info(f"Panel update completed\n{logs[-1000:]}")
        else:
            _update_status["last_result"] = "failed"
            _update_status["last_error"] = f"Exit code: {exit_code}\n{logs[-1000:]}"
            logger.error(f"Panel update failed:\n{logs[-1000:]}")
        
        # Cleanup
        try:
            await asyncio.to_thread(container.remove, force=True)
        except Exception:
            pass
            
    except asyncio.TimeoutError:
        _update_status["last_result"] = "failed"
        _update_status["last_error"] = "Update timed out (10 minutes)"
        logger.error("Panel update timed out")
    except ImageNotFound as e:
        _update_status["last_result"] = "failed"
        _update_status["last_error"] = f"Image not found: {e}"
        logger.error(f"Image not found: {e}")
    except DockerException as e:
        _update_status["last_result"] = "failed"
        _update_status["last_error"] = f"Docker error: {e}"
        logger.error(f"Docker error: {e}")
    except Exception as e:
        _update_status["last_result"] = "failed"
        _update_status["last_error"] = str(e)
        logger.error(f"Unexpected error: {e}")
    finally:
        _update_status["in_progress"] = False


@router.post("/update")
async def trigger_panel_update(
    target_ref: str | None = Query(None, max_length=100, pattern=GIT_REF_PATTERN),
    _: dict = Depends(verify_auth)
):
    """
    Trigger panel update from GitHub.
    
    Creates a separate updater container that:
    1. Clones fresh repository
    2. Runs update.sh from the cloned version
    3. update.sh stops containers, copies files, rebuilds, restarts
    
    Args:
        target_ref: Git reference (branch/tag/commit). Default: выбранный канал обновлений.

    Note: The panel will restart after update, connection will be lost temporarily.
    """
    if _update_status["in_progress"]:
        raise HTTPException(status_code=409)

    asyncio.create_task(run_panel_update_in_container(target_ref))

    return {
        "success": True,
        "message": "Panel update started. The panel will restart shortly.",
        "target": target_ref or update_channel.current_branch()
    }


# SSL Certificate management
def get_panel_certificate_info() -> dict:
    """Read SSL certificate information for the panel domain"""
    settings = get_settings()
    domain = settings.domain
    
    if not domain:
        return {
            "domain": None,
            "error": "Domain not configured"
        }
    
    cert_path = Path(f"/etc/letsencrypt/live/{domain}/fullchain.pem")
    
    if not cert_path.exists():
        return {
            "domain": domain,
            "error": "Certificate not found"
        }
    
    try:
        result = subprocess.run(
            ["openssl", "x509", "-enddate", "-noout", "-in", str(cert_path)],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode != 0:
            return {
                "domain": domain,
                "error": f"Failed to read certificate: {result.stderr}"
            }
        
        # Parse expiry date from "notAfter=Dec 31 23:59:59 2024 GMT"
        expiry_str = result.stdout.strip().replace("notAfter=", "")
        expiry_date = datetime.strptime(expiry_str, "%b %d %H:%M:%S %Y %Z")
        
        now = datetime.utcnow()
        days_left = (expiry_date - now).days
        
        return {
            "domain": domain,
            "expiry_date": expiry_date.isoformat(),
            "days_left": days_left,
            "expired": days_left <= 0
        }
        
    except subprocess.TimeoutExpired:
        return {
            "domain": domain,
            "error": "Timeout reading certificate"
        }
    except Exception as e:
        logger.error(f"Error reading certificate: {e}")
        return {
            "domain": domain,
            "error": str(e)
        }


async def _panel_cert_managed_by_wildcard() -> bool:
    async with async_session() as db:
        row = (await db.execute(
            select(PanelSettings).where(PanelSettings.key == USE_FOR_PANEL_SETTING)
        )).scalar_one_or_none()
        return bool(row and row.value == "true")


@router.get("/certificate")
async def get_certificate_info(_: dict = Depends(verify_auth)):
    """Get SSL certificate information for the panel"""
    cert_info = get_panel_certificate_info()
    cert_info["renewal_in_progress"] = _cert_renewal_status["in_progress"]
    cert_info["managed_by_wildcard"] = await _panel_cert_managed_by_wildcard()
    return cert_info


async def run_certificate_renewal(force: bool = False):
    """Execute certificate renewal script in background"""
    
    _cert_renewal_status["in_progress"] = True
    _cert_renewal_status["last_error"] = None
    _cert_renewal_status["output"] = None
    _cert_renewal_status["started_at"] = datetime.now().isoformat()
    _cert_renewal_status["completed_at"] = None
    
    RENEW_CERT_SCRIPT = Path("/opt/monitoring-panel/renew-cert.sh")
    
    try:
        if not RENEW_CERT_SCRIPT.exists():
            error_msg = f"Renewal script not found: {RENEW_CERT_SCRIPT}"
            logger.error(error_msg)
            raise FileNotFoundError(error_msg)
        
        # Build command with optional --force flag
        cmd = ["/bin/bash", str(RENEW_CERT_SCRIPT)]
        if force:
            cmd.append("--force")
            logger.info("Starting FORCED certificate renewal")
        else:
            logger.info("Starting certificate renewal")
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/opt/monitoring-panel"
        )
        
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=300  # 5 minutes max
        )
        
        output = stdout.decode() if stdout else ""
        error_output = stderr.decode() if stderr else ""
        full_output = f"{output}\n{error_output}".strip()
        
        # Log output
        for line in full_output.split('\n'):
            if line.strip():
                logger.info(f"  | {line}")
        
        _cert_renewal_status["output"] = full_output
        _cert_renewal_status["completed_at"] = datetime.now().isoformat()
        
        if process.returncode == 0:
            _cert_renewal_status["last_result"] = "success"
            _cert_renewal_status["last_error"] = None
            logger.info("Certificate renewal completed successfully")
        elif process.returncode == 2:
            _cert_renewal_status["last_result"] = "not_due"
            _cert_renewal_status["last_error"] = "Certificate is not due for renewal yet"
            logger.info("Certificate not due for renewal")
        else:
            _cert_renewal_status["last_result"] = "failed"
            _cert_renewal_status["last_error"] = full_output or "Unknown error"
            logger.error(f"Certificate renewal failed (exit code: {process.returncode})")
            
    except asyncio.TimeoutError:
        _cert_renewal_status["last_result"] = "failed"
        _cert_renewal_status["last_error"] = "Renewal timed out after 5 minutes"
        _cert_renewal_status["completed_at"] = datetime.now().isoformat()
        logger.error("Certificate renewal timed out")
    except Exception as e:
        _cert_renewal_status["last_result"] = "failed"
        _cert_renewal_status["last_error"] = str(e)
        _cert_renewal_status["completed_at"] = datetime.now().isoformat()
        logger.error(f"Certificate renewal error: {e}")
    finally:
        _cert_renewal_status["in_progress"] = False


@router.post("/certificate/renew")
async def renew_certificate(
    background_tasks: BackgroundTasks,
    force: bool = False,
    _: dict = Depends(verify_auth)
):
    """
    Trigger SSL certificate renewal.
    
    Query params:
    - force: bool - Force renewal even if certificate is not due (default: false)
    """
    if _cert_renewal_status["in_progress"]:
        raise HTTPException(status_code=409)

    # Сертификат панели ведёт wildcard-модуль: standalone-продление здесь создало
    # бы параллельную certbot-линию и увело nginx с wildcard-сертификата
    if await _panel_cert_managed_by_wildcard():
        raise HTTPException(status_code=400, detail="Certificate is managed by Wildcard SSL")

    # Check if certificate exists
    cert_info = get_panel_certificate_info()
    if "error" in cert_info and cert_info.get("error") == "Certificate not found":
        raise HTTPException(status_code=400)
    
    background_tasks.add_task(run_certificate_renewal, force)
    
    return {
        "success": True,
        "message": "Certificate renewal started" + (" (forced)" if force else "")
    }


@router.get("/certificate/renew/status")
async def get_renewal_status(_: dict = Depends(verify_auth)):
    """Get current certificate renewal status with detailed output"""
    return {
        "in_progress": _cert_renewal_status["in_progress"],
        "last_result": _cert_renewal_status["last_result"],
        "last_error": _cert_renewal_status["last_error"],
        "output": _cert_renewal_status.get("output"),
        "started_at": _cert_renewal_status.get("started_at"),
        "completed_at": _cert_renewal_status.get("completed_at")
    }


# ==================== System Optimizations ====================

async def get_optimizations_from_github(profile: str = "vpn") -> dict:
    """Fetch the renderer INPUTS for a profile plus the shared NIC scripts.

    The node no longer receives a finished sysctl.conf: it receives the base
    files and the renderer, and computes every size-dependent value from its own
    MemTotal/nproc. That is the only way one config can be correct on both a
    2 CPU / 4 GB VPS and a 64 CPU / 248 GB server.
    """
    if profile not in ("vpn", "panel"):
        profile = "vpn"

    result = {
        "version": None,
        "profile": profile,
        "renderer_content": None,
        "common_base_content": None,
        "profile_base_content": None,
        "limits_tmpl_content": None,
        "systemd_limits_tmpl_content": None,
        "network_tune_content": None,
        "network_tune_service_content": None,
        "multiqueue_tune_content": None,
        "multiqueue_tune_service_content": None,
        "hybrid_tune_content": None,
        "hybrid_tune_service_content": None,
    }

    try:
        client = get_external_client()
        responses = await asyncio.gather(
            client.get(_github_configs_url("VERSION"), timeout=15.0),
            client.get(_github_configs_url("tune-sysctl.sh"), timeout=15.0),
            client.get(_profiles_url("common.base.conf"), timeout=15.0),
            client.get(_profiles_url(f"{profile}.base.conf"), timeout=15.0),
            client.get(_profiles_url("limits.tmpl"), timeout=15.0),
            client.get(_profiles_url("systemd-limits.tmpl"), timeout=15.0),
            client.get(_github_configs_url("network-tune.sh"), timeout=15.0),
            client.get(_github_configs_url("network-tune.service"), timeout=15.0),
            client.get(_github_configs_url("multiqueue-tune.sh"), timeout=15.0),
            client.get(_github_configs_url("multiqueue-tune.service"), timeout=15.0),
            client.get(_github_configs_url("hybrid-tune.sh"), timeout=15.0),
            client.get(_github_configs_url("hybrid-tune.service"), timeout=15.0),
            return_exceptions=True
        )

        keys = [
            ("version", "configs version"),
            ("renderer_content", "tune-sysctl.sh"),
            ("common_base_content", "profiles/common.base.conf"),
            ("profile_base_content", f"profiles/{profile}.base.conf"),
            ("limits_tmpl_content", "profiles/limits.tmpl"),
            ("systemd_limits_tmpl_content", "profiles/systemd-limits.tmpl"),
            ("network_tune_content", "network-tune.sh"),
            ("network_tune_service_content", "network-tune.service"),
            ("multiqueue_tune_content", "multiqueue-tune.sh"),
            ("multiqueue_tune_service_content", "multiqueue-tune.service"),
            ("hybrid_tune_content", "hybrid-tune.sh"),
            ("hybrid_tune_service_content", "hybrid-tune.service"),
        ]

        for (key, label), resp in zip(keys, responses):
            if isinstance(resp, Exception):
                logger.error(f"Failed to fetch {label}: {resp}")
            elif resp.status_code == 200:
                result[key] = resp.text.strip() if key == "version" else resp.text

    except Exception as e:
        logger.error(f"Failed to fetch optimizations from GitHub: {e}")

    return result


# NOTE: GET /optimizations/configs used to exist here and returned the finished
# sysctl/limits contents. It had no callers (the apply flow in proxy.py calls
# get_optimizations_from_github directly) and its response shape described the
# pre-renderer contract, so it was removed rather than ported.


# ==================== Panel Server Statistics ====================

CPU_SAMPLE_INTERVAL_SEC = 0.5


def _collect_host_stats() -> tuple:
    var_disk = None
    try:
        var_disk = psutil.disk_usage('/var')
    except Exception:
        pass
    return (
        psutil.cpu_percent(interval=CPU_SAMPLE_INTERVAL_SEC),
        psutil.cpu_count(),
        psutil.getloadavg(),
        psutil.virtual_memory(),
        psutil.swap_memory(),
        psutil.disk_usage('/'),
        var_disk,
    )


@router.get("/stats")
async def get_panel_server_stats(_: dict = Depends(verify_auth)):
    """
    Get panel server statistics:
    - CPU usage and load
    - Memory (RAM) usage
    - Disk usage for main partition
    """
    try:
        # cpu_percent(interval=...) спит ровно столько же, сколько измеряет,
        # а остальное читает /proc и /sys — весь блок уходит в поток целиком
        (
            cpu_percent, cpu_count, load_avg,
            memory, swap, disk, var_disk,
        ) = await asyncio.to_thread(_collect_host_stats)

        return {
            "cpu": {
                "percent": cpu_percent,
                "cores": cpu_count,
                "load_avg_1": load_avg[0],
                "load_avg_5": load_avg[1],
                "load_avg_15": load_avg[2]
            },
            "memory": {
                "total": memory.total,
                "used": memory.used,
                "available": memory.available,
                "percent": memory.percent,
                "swap_total": swap.total,
                "swap_used": swap.used,
                "swap_percent": swap.percent
            },
            "disk": {
                "total": disk.total,
                "used": disk.used,
                "free": disk.free,
                "percent": disk.percent
            },
            "disk_var": {
                "total": var_disk.total if var_disk else None,
                "used": var_disk.used if var_disk else None,
                "free": var_disk.free if var_disk else None,
                "percent": var_disk.percent if var_disk else None
            } if var_disk else None
        }
    except Exception as e:
        logger.error(f"Error getting server stats: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get server stats: {str(e)}")


@router.get("/stats/history")
async def get_panel_host_history(
    period: HostHistoryPeriod = Query(default="1h"),
    _: dict = Depends(verify_auth),
    db: AsyncSession = Depends(get_db),
):
    """История нагрузки хоста панели: среднее и пик за интервал точки."""
    return await load_host_history(db, period)
