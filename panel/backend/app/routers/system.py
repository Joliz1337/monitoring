"""System management endpoints - version info, updates, SSL certificate"""

import asyncio
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

import docker
import httpx
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

VERSION_FILE = Path("/app/VERSION")
UPDATER_CONTAINER_NAME = "panel-updater"
UPDATER_IMAGE = "docker:cli"
GITHUB_PANEL_VERSION_URL = "https://raw.githubusercontent.com/Joliz1337/monitoring/main/panel/VERSION"
GITHUB_NODE_VERSION_URL = "https://raw.githubusercontent.com/Joliz1337/monitoring/main/node/VERSION"

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


async def get_node_version(url: str, api_key: str) -> Optional[str]:
    """Fetch version from a node"""
    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            response = await client.get(
                f"{url}/api/version",
                headers={"X-API-Key": api_key}
            )
            
            if response.status_code == 200:
                data = response.json()
                return data.get("version")
            
            return None
    except Exception as e:
        logger.debug(f"Failed to get version from {url}: {e}")
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
    - Versions of all connected nodes
    """
    current_version = get_current_version()
    
    # Fetch both versions in parallel
    latest_version, latest_node_version = await asyncio.gather(
        get_latest_version_from_github(),
        get_latest_node_version_from_github()
    )
    
    # Get all servers and their versions
    result = await db.execute(
        select(Server).where(Server.is_active == True).order_by(Server.position)
    )
    servers = result.scalars().all()
    
    nodes_versions = []
    for server in servers:
        version = await get_node_version(server.url, server.api_key)
        nodes_versions.append({
            "id": server.id,
            "name": server.name,
            "url": server.url,
            "version": version,
            "status": "online" if version else "offline"
        })
    
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
COMPOSE_FILE="docker/compose/releases/latest/download/docker-compose-linux-x86_64"
COMPOSE_MIRRORS="https://kkgithub.com/$COMPOSE_FILE https://hub.gitmirror.com/$COMPOSE_FILE https://ghproxy.com/https://github.com/$COMPOSE_FILE https://github.com/$COMPOSE_FILE"
for mirror in $COMPOSE_MIRRORS; do
    echo "  Trying: $(echo $mirror | sed 's|https://||' | cut -d'/' -f1)..."
    if curl -fsSL --connect-timeout 15 --max-time 180 -o /usr/local/lib/docker/cli-plugins/docker-compose "$mirror" 2>/dev/null; then
        chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
        echo "  Docker Compose installed"
        break
    fi
done

echo "[INFO] Detecting server location..."
COUNTRY=""
COUNTRY=$(curl -fsSL --connect-timeout 5 --max-time 10 "http://ip-api.com/json?fields=countryCode" 2>/dev/null | grep -o '"countryCode":"[^"]*"' | cut -d'"' -f4 || echo "")
if [ -z "$COUNTRY" ]; then
    COUNTRY=$(curl -fsSL --connect-timeout 5 --max-time 10 "https://ipapi.co/country_code/" 2>/dev/null | tr -d '[:space:]' | head -c 2 || echo "")
fi
echo "[INFO] Server location: $COUNTRY"

# GitHub mirrors for Russia (replace-type mirrors that work with git clone)
MIRRORS_RU="https://kkgithub.com https://hub.gitmirror.com"

TMP_CLONE=/tmp/monitoring-fresh
rm -rf $TMP_CLONE
CLONE_SUCCESS=0

clone_repo() {{
    local mirror="$1"
    local repo_url
    if [ "$mirror" = "direct" ]; then
        repo_url="https://github.com/Joliz1337/monitoring.git"
        echo "[INFO] Trying direct GitHub..."
    else
        repo_url="${{mirror}}/Joliz1337/monitoring.git"
        echo "[INFO] Trying mirror: $(echo $mirror | sed 's|https://||')..."
    fi
    timeout 180 git clone --depth 1 --branch {ref_arg} "$repo_url" $TMP_CLONE 2>&1
}}

if [ "$COUNTRY" = "RU" ]; then
    echo "[INFO] Russia detected - using GitHub mirrors"
    for mirror in $MIRRORS_RU; do
        if clone_repo "$mirror"; then
            CLONE_SUCCESS=1
            break
        fi
        rm -rf $TMP_CLONE
        echo "[WARN] Mirror failed, trying next..."
    done
    if [ $CLONE_SUCCESS -eq 0 ]; then
        echo "[INFO] All mirrors failed, trying direct GitHub..."
        if clone_repo "direct"; then
            CLONE_SUCCESS=1
        fi
    fi
else
    if clone_repo "direct"; then
        CLONE_SUCCESS=1
    fi
fi

if [ $CLONE_SUCCESS -eq 0 ]; then
    echo "[ERROR] Failed to clone repository from all sources"
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
