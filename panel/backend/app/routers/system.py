"""System management endpoints - version info, updates, SSL certificate"""

import asyncio
import logging
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

import docker
import httpx
import psutil
from docker.errors import DockerException, ImageNotFound
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.config import get_settings
from app.database import get_db
from app.models import Server

router = APIRouter(prefix="/system", tags=["system"])
logger = logging.getLogger(__name__)


def get_panel_ip() -> str | None:
    """Get panel's IP address by resolving the configured domain"""
    settings = get_settings()
    domain = settings.domain
    
    if not domain:
        return None
    
    try:
        ip = socket.gethostbyname(domain)
        return ip
    except socket.gaierror:
        logger.warning(f"Failed to resolve domain: {domain}")
        return None


@router.get("/panel-ip")
async def get_panel_ip_endpoint(_: dict = Depends(verify_auth)):
    """Get panel's IP address"""
    ip = get_panel_ip()
    settings = get_settings()
    return {
        "ip": ip,
        "domain": settings.domain
    }

VERSION_FILE = Path("/app/VERSION")
UPDATER_CONTAINER_NAME = "panel-updater"
UPDATER_IMAGE = "docker:cli"
GITHUB_PANEL_VERSION_URL = "https://raw.githubusercontent.com/Joliz1337/monitoring/main/panel/VERSION"
GITHUB_NODE_VERSION_URL = "https://raw.githubusercontent.com/Joliz1337/monitoring/main/node/VERSION"
GITHUB_CONFIGS_VERSION_URL = "https://raw.githubusercontent.com/Joliz1337/monitoring/main/configs/VERSION"
GITHUB_SYSCTL_URL = "https://raw.githubusercontent.com/Joliz1337/monitoring/main/configs/sysctl.conf"
GITHUB_LIMITS_URL = "https://raw.githubusercontent.com/Joliz1337/monitoring/main/configs/limits.conf"
GITHUB_SYSTEMD_LIMITS_URL = "https://raw.githubusercontent.com/Joliz1337/monitoring/main/configs/systemd-limits.conf"
GITHUB_NETWORK_TUNE_URL = "https://raw.githubusercontent.com/Joliz1337/monitoring/main/configs/network-tune.sh"
GITHUB_NETWORK_TUNE_SERVICE_URL = "https://raw.githubusercontent.com/Joliz1337/monitoring/main/configs/network-tune.service"

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
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(GITHUB_PANEL_VERSION_URL)
            
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
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(GITHUB_NODE_VERSION_URL)
            
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
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(GITHUB_CONFIGS_VERSION_URL)
            
            if response.status_code == 200:
                version = response.text.strip()
                return version if version else None
            
            return None
    except Exception as e:
        logger.error(f"Failed to fetch latest optimizations version from GitHub: {e}")
        return None


async def get_node_all_versions(url: str, api_key: str) -> Optional[dict]:
    """
    Fetch all versions from a node using combined endpoint.
    Returns dict with node_version and optimizations, or None if unreachable.
    """
    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            response = await client.get(
                f"{url}/api/system/versions",
                headers={"X-API-Key": api_key}
            )
            
            if response.status_code == 200:
                return response.json()
            
            # Fallback to old endpoint if new one not available
            response = await client.get(
                f"{url}/api/version",
                headers={"X-API-Key": api_key}
            )
            
            if response.status_code == 200:
                data = response.json()
                return {
                    "node_version": data.get("version"),
                    "optimizations": {"installed": False, "version": None}
                }
            
            return None
    except Exception as e:
        logger.debug(f"Failed to get versions from {url}: {e}")
        return None


async def get_node_version(url: str, api_key: str) -> Optional[str]:
    """Fetch version from a node (legacy, prefer get_node_all_versions)"""
    result = await get_node_all_versions(url, api_key)
    if result:
        return result.get("node_version")
    return None


def get_docker_client():
    """Get Docker client via socket"""
    try:
        return docker.from_env()
    except DockerException as e:
        logger.error(f"Failed to connect to Docker: {e}")
        raise


@router.get("/version")
async def get_version_info(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """
    Get comprehensive version information:
    - Current panel version and latest available
    - Latest available node version from GitHub
    - Latest optimizations version from GitHub
    - Versions of all connected nodes (including optimizations)
    
    All node requests are made in parallel for faster response.
    """
    current_version = get_current_version()
    
    # Get all servers from DB
    result = await db.execute(
        select(Server).where(Server.is_active == True).order_by(Server.position)
    )
    servers = result.scalars().all()
    
    # Fetch GitHub versions and all node versions in parallel
    async def fetch_node_data(server: Server) -> dict:
        """Fetch version data from a single node"""
        versions_data = await get_node_all_versions(server.url, server.api_key)
        
        # Determine online/offline based on whether we got a response
        is_online = versions_data is not None
        
        return {
            "id": server.id,
            "name": server.name,
            "url": server.url,
            "version": versions_data.get("node_version") if versions_data else None,
            "status": "online" if is_online else "offline",
            "optimizations": versions_data.get("optimizations", {"installed": False, "version": None}) if versions_data else {"installed": False, "version": None}
        }
    
    # Execute all requests in parallel
    github_panel_task = get_latest_version_from_github()
    github_node_task = get_latest_node_version_from_github()
    github_opt_task = get_latest_optimizations_version_from_github()
    node_tasks = [fetch_node_data(server) for server in servers]
    
    # Gather all results
    results = await asyncio.gather(
        github_panel_task,
        github_node_task,
        github_opt_task,
        *node_tasks,
        return_exceptions=True
    )
    
    # Parse results
    latest_version = results[0] if not isinstance(results[0], Exception) else None
    latest_node_version = results[1] if not isinstance(results[1], Exception) else None
    latest_opt_version = results[2] if not isinstance(results[2], Exception) else None
    
    # Process node results
    nodes_versions = []
    for i, node_result in enumerate(results[3:]):
        if isinstance(node_result, Exception):
            server = servers[i]
            nodes_versions.append({
                "id": server.id,
                "name": server.name,
                "url": server.url,
                "version": None,
                "status": "offline",
                "optimizations": {"installed": False, "version": None}
            })
        else:
            nodes_versions.append(node_result)
    
    # Check if panel update is available
    panel_update_available = False
    if latest_version and current_version != "unknown":
        panel_update_available = latest_version != current_version
    
    return {
        "panel": {
            "version": current_version,
            "latest_version": latest_version,
            "update_available": panel_update_available
        },
        "node": {
            "latest_version": latest_node_version
        },
        "optimizations": {
            "latest_version": latest_opt_version
        },
        "nodes": nodes_versions,
        "update_in_progress": _update_status["in_progress"]
    }


async def run_panel_update_in_container(target_ref: str | None = None):
    """
    Run panel update in separate Docker container.
    
    Args:
        target_ref: Git reference (branch/tag/commit). Default: 'main'
    """
    global _update_status
    
    _update_status["in_progress"] = True
    _update_status["last_error"] = None
    
    try:
        client = get_docker_client()
        
        # Remove old updater container if exists
        try:
            old_container = client.containers.get(UPDATER_CONTAINER_NAME)
            old_container.remove(force=True)
            logger.info("Removed old updater container")
        except docker.errors.NotFound:
            pass
        
        # Pull docker:cli image if needed
        try:
            client.images.get(UPDATER_IMAGE)
        except ImageNotFound:
            logger.info(f"Pulling {UPDATER_IMAGE}...")
            client.images.pull(UPDATER_IMAGE)
        
        ref_arg = target_ref if target_ref else "main"
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
if timeout 30 git clone --depth 1 --branch {ref_arg} "https://github.com/Joliz1337/monitoring.git" $TMP_CLONE 2>&1; then
    CLONE_SUCCESS=1
else
    rm -rf $TMP_CLONE
    echo "[WARN] GitHub timeout/error, trying mirror (ghfast.top)..."
    if timeout 120 git clone --depth 1 --branch {ref_arg} "$GITHUB_MIRROR/https://github.com/Joliz1337/monitoring.git" $TMP_CLONE 2>&1; then
        CLONE_SUCCESS=1
    fi
fi

if [ $CLONE_SUCCESS -eq 0 ]; then
    echo "[ERROR] Failed to clone repository"
    exit 1
fi

echo "[INFO] Running update script..."
chmod +x $TMP_CLONE/panel/update.sh
bash $TMP_CLONE/panel/update.sh {ref_arg}

echo "[INFO] Cleanup..."
rm -rf $TMP_CLONE

echo "[SUCCESS] Panel update completed!"
"""
        
        container = client.containers.run(
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
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: container.wait(timeout=600)
        )
        
        exit_code = result.get("StatusCode", -1)
        logs = container.logs().decode("utf-8", errors="replace")
        
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
            container.remove(force=True)
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
    target_ref: str | None = None,
    _: dict = Depends(verify_auth)
):
    """
    Trigger panel update from GitHub.
    
    Creates a separate updater container that:
    1. Clones fresh repository
    2. Runs update.sh from the cloned version
    3. update.sh stops containers, copies files, rebuilds, restarts
    
    Args:
        target_ref: Git reference (branch/tag/commit). Default: 'main' (latest)
    
    Note: The panel will restart after update, connection will be lost temporarily.
    """
    if _update_status["in_progress"]:
        raise HTTPException(status_code=409)
    
    asyncio.create_task(run_panel_update_in_container(target_ref))
    
    return {
        "success": True,
        "message": "Panel update started. The panel will restart shortly.",
        "target": target_ref or "main"
    }


@router.get("/update/status")
async def get_update_status(_: dict = Depends(verify_auth)):
    """Get current panel update status"""
    container_running = False
    try:
        client = get_docker_client()
        container = client.containers.get(UPDATER_CONTAINER_NAME)
        container_running = container.status == "running"
    except Exception:
        pass
    
    return {
        "in_progress": _update_status["in_progress"] or container_running,
        "last_result": _update_status["last_result"],
        "last_error": _update_status["last_error"],
        "last_update_time": _update_status.get("last_update_time")
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


@router.get("/certificate")
async def get_certificate_info(_: dict = Depends(verify_auth)):
    """Get SSL certificate information for the panel"""
    cert_info = get_panel_certificate_info()
    cert_info["renewal_in_progress"] = _cert_renewal_status["in_progress"]
    return cert_info


async def run_certificate_renewal(force: bool = False):
    """Execute certificate renewal script in background"""
    global _cert_renewal_status
    
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

async def get_optimizations_from_github() -> dict:
    """Fetch optimization configs and version from GitHub"""
    result = {
        "version": None,
        "sysctl_content": None,
        "limits_content": None,
        "systemd_content": None,
        "network_tune_content": None,
        "network_tune_service_content": None
    }
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Fetch version and all configs in parallel
            responses = await asyncio.gather(
                client.get(GITHUB_CONFIGS_VERSION_URL),
                client.get(GITHUB_SYSCTL_URL),
                client.get(GITHUB_LIMITS_URL),
                client.get(GITHUB_SYSTEMD_LIMITS_URL),
                client.get(GITHUB_NETWORK_TUNE_URL),
                client.get(GITHUB_NETWORK_TUNE_SERVICE_URL),
                return_exceptions=True
            )
            
            version_resp, sysctl_resp, limits_resp, systemd_resp, network_tune_resp, service_resp = responses
            
            # Parse version from configs/VERSION file
            if isinstance(version_resp, Exception):
                logger.error(f"Failed to fetch configs version: {version_resp}")
            elif version_resp.status_code == 200:
                result["version"] = version_resp.text.strip()
            
            if isinstance(sysctl_resp, Exception):
                logger.error(f"Failed to fetch sysctl config: {sysctl_resp}")
            elif sysctl_resp.status_code == 200:
                result["sysctl_content"] = sysctl_resp.text
            
            if isinstance(limits_resp, Exception):
                logger.error(f"Failed to fetch limits config: {limits_resp}")
            elif limits_resp.status_code == 200:
                result["limits_content"] = limits_resp.text
            
            if isinstance(systemd_resp, Exception):
                logger.error(f"Failed to fetch systemd config: {systemd_resp}")
            elif systemd_resp.status_code == 200:
                result["systemd_content"] = systemd_resp.text
            
            if isinstance(network_tune_resp, Exception):
                logger.error(f"Failed to fetch network-tune.sh: {network_tune_resp}")
            elif network_tune_resp.status_code == 200:
                result["network_tune_content"] = network_tune_resp.text
            
            if isinstance(service_resp, Exception):
                logger.error(f"Failed to fetch network-tune.service: {service_resp}")
            elif service_resp.status_code == 200:
                result["network_tune_service_content"] = service_resp.text
                
    except Exception as e:
        logger.error(f"Failed to fetch optimizations from GitHub: {e}")
    
    return result


@router.get("/optimizations/version")
async def get_optimizations_version_info(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """
    Get system optimizations version information:
    - Latest version from GitHub
    - Versions installed on all nodes
    
    Note: This data is also available in /system/version endpoint.
    All node requests are made in parallel for faster response.
    """
    # Get all servers from DB
    result = await db.execute(
        select(Server).where(Server.is_active == True).order_by(Server.position)
    )
    servers = result.scalars().all()
    
    async def fetch_node_opt_data(server: Server) -> dict:
        """Fetch optimizations data from a single node"""
        versions_data = await get_node_all_versions(server.url, server.api_key)
        is_online = versions_data is not None
        
        opt_data = versions_data.get("optimizations", {}) if versions_data else {}
        installed = opt_data.get("installed", False)
        node_version = opt_data.get("version")
        
        return {
            "id": server.id,
            "name": server.name,
            "installed": installed,
            "version": node_version,
            "status": "online" if is_online else "offline",
            "_online": is_online  # internal flag for update_available calculation
        }
    
    # Execute all requests in parallel
    github_task = get_latest_optimizations_version_from_github()
    node_tasks = [fetch_node_opt_data(server) for server in servers]
    
    results = await asyncio.gather(
        github_task,
        *node_tasks,
        return_exceptions=True
    )
    
    latest_version = results[0] if not isinstance(results[0], Exception) else None
    
    nodes_info = []
    for i, node_result in enumerate(results[1:]):
        if isinstance(node_result, Exception):
            server = servers[i]
            nodes_info.append({
                "id": server.id,
                "name": server.name,
                "installed": False,
                "version": None,
                "status": "offline",
                "update_available": False
            })
        else:
            # Calculate update_available
            installed = node_result.get("installed", False)
            node_version = node_result.get("version")
            is_online = node_result.get("_online", False)
            
            update_available = False
            if is_online and latest_version:
                if installed and node_version:
                    update_available = node_version != latest_version
                elif not installed:
                    update_available = True
            
            nodes_info.append({
                "id": node_result["id"],
                "name": node_result["name"],
                "installed": installed,
                "version": node_version,
                "status": node_result["status"],
                "update_available": update_available
            })
    
    return {
        "latest_version": latest_version,
        "nodes": nodes_info
    }


@router.get("/optimizations/configs")
async def get_optimizations_configs(_: dict = Depends(verify_auth)):
    """
    Get optimization config contents from GitHub.
    Used by proxy endpoint to apply configs to nodes.
    """
    github_data = await get_optimizations_from_github()
    
    if not github_data.get("sysctl_content"):
        raise HTTPException(status_code=502, detail="Failed to fetch configs from GitHub")
    
    return {
        "version": github_data.get("version"),
        "sysctl_content": github_data.get("sysctl_content"),
        "limits_content": github_data.get("limits_content"),
        "systemd_content": github_data.get("systemd_content"),
        "network_tune_content": github_data.get("network_tune_content"),
        "network_tune_service_content": github_data.get("network_tune_service_content")
    }


# ==================== Panel Server Statistics ====================

@router.get("/stats")
async def get_panel_server_stats(_: dict = Depends(verify_auth)):
    """
    Get panel server statistics:
    - CPU usage and load
    - Memory (RAM) usage
    - Disk usage for main partition
    """
    try:
        # CPU
        cpu_percent = psutil.cpu_percent(interval=0.5)
        cpu_count = psutil.cpu_count()
        load_avg = psutil.getloadavg()
        
        # Memory
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        
        # Disk - get root partition (/)
        disk = psutil.disk_usage('/')
        
        # Additional disk info for /var (where PostgreSQL data usually is)
        var_disk = None
        try:
            var_disk = psutil.disk_usage('/var')
        except Exception:
            pass
        
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
