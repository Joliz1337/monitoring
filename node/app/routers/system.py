"""System management endpoints - version info and updates"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

import docker
from docker.errors import DockerException, ImageNotFound
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/system", tags=["system"])
logger = logging.getLogger(__name__)

VERSION_FILE = Path("/app/VERSION")
UPDATER_CONTAINER_NAME = "monitoring-updater"
UPDATER_IMAGE = "docker:cli"

_update_status = {
    "in_progress": False,
    "last_result": None,
    "last_error": None,
    "last_update_time": None
}


def get_current_version() -> str:
    """Read current version from VERSION file"""
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text().strip()
    return "unknown"


def get_docker_client():
    """Get Docker client via socket"""
    try:
        return docker.from_env()
    except DockerException as e:
        logger.error(f"Failed to connect to Docker: {e}")
        raise


@router.get("/version")
async def get_version():
    """Get current node version"""
    return {
        "version": get_current_version(),
        "component": "node"
    }


async def run_update_in_container(target_ref: str | None = None):
    """
    Run update in separate Docker container.
    
    Args:
        target_ref: Git reference (commit hash, tag, or branch). Default: 'main'
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
        logger.info(f"Starting update to: {ref_arg}")
        
        # Updater script:
        # 1. Install dependencies
        # 2. Detect country and select mirror
        # 3. Clone repo with fallback mirrors
        # 4. Run update.sh from cloned repo
        updater_script = f"""#!/bin/sh
set -e

echo "[INFO] Installing dependencies..."
apk add --no-cache git curl rsync bash >/dev/null 2>&1

echo "[INFO] Installing Docker Compose..."
mkdir -p /usr/local/lib/docker/cli-plugins
COMPOSE_URL="https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64"
COMPOSE_BIN="/usr/local/lib/docker/cli-plugins/docker-compose"
COMPOSE_OK=0
for method in "direct" "mirror" "direct-k" "mirror-k"; do
    case $method in
        direct)   echo "  Trying GitHub..."; curl -fsSL --connect-timeout 15 --max-time 180 -o "$COMPOSE_BIN" "$COMPOSE_URL" 2>/dev/null ;;
        mirror)   echo "  Trying ghfast.top..."; curl -fsSL --connect-timeout 15 --max-time 180 -o "$COMPOSE_BIN" "https://ghfast.top/$COMPOSE_URL" 2>/dev/null ;;
        direct-k) echo "  Trying GitHub (skip SSL)..."; curl -fsSLk --connect-timeout 15 --max-time 180 -o "$COMPOSE_BIN" "$COMPOSE_URL" 2>/dev/null ;;
        mirror-k) echo "  Trying ghfast.top (skip SSL)..."; curl -fsSLk --connect-timeout 15 --max-time 180 -o "$COMPOSE_BIN" "https://ghfast.top/$COMPOSE_URL" 2>/dev/null ;;
    esac
    if [ -f "$COMPOSE_BIN" ] && chmod +x "$COMPOSE_BIN" && "$COMPOSE_BIN" version >/dev/null 2>&1; then
        echo "  Docker Compose installed"; COMPOSE_OK=1; break
    fi
done
[ $COMPOSE_OK -eq 0 ] && echo "  WARNING: Docker Compose installation failed"

echo "[INFO] Cloning repository..."

# GitHub mirror
GITHUB_MIRROR="https://ghfast.top"

TMP_CLONE=/tmp/monitoring-fresh
rm -rf $TMP_CLONE
CLONE_SUCCESS=0

echo "[INFO] Trying GitHub (direct)..."
if timeout 180 git clone --depth 1 --branch {ref_arg} "https://github.com/Joliz1337/monitoring.git" $TMP_CLONE 2>&1; then
    CLONE_SUCCESS=1
else
    rm -rf $TMP_CLONE
    echo "[WARN] GitHub failed, trying ghfast.top..."
    if timeout 180 git clone --depth 1 --branch {ref_arg} "$GITHUB_MIRROR/https://github.com/Joliz1337/monitoring.git" $TMP_CLONE 2>&1; then
        CLONE_SUCCESS=1
    fi
fi

if [ $CLONE_SUCCESS -eq 0 ]; then
    echo "[ERROR] Failed to clone repository from all sources"
    exit 1
fi

echo "[INFO] Running update script..."
chmod +x $TMP_CLONE/node/update.sh
bash $TMP_CLONE/node/update.sh {ref_arg}

echo "[INFO] Cleanup..."
rm -rf $TMP_CLONE

echo "[SUCCESS] Update completed!"
"""
        
        container = client.containers.run(
            image=UPDATER_IMAGE,
            command=["sh", "-c", updater_script],
            name=UPDATER_CONTAINER_NAME,
            volumes={
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
                "/opt/monitoring-node": {"bind": "/opt/monitoring-node", "mode": "rw"},
            },
            network_mode="host",
            privileged=True,
            detach=True,
            remove=False,
        )
        
        logger.info(f"Updater started: {container.id[:12]}")
        
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
            logger.info(f"Update completed successfully\n{logs[-1000:]}")
        else:
            _update_status["last_result"] = "failed"
            _update_status["last_error"] = f"Exit code: {exit_code}\n{logs[-1000:]}"
            logger.error(f"Update failed: {logs[-1000:]}")
        
        # Cleanup
        try:
            container.remove(force=True)
        except Exception:
            pass
            
    except asyncio.TimeoutError:
        _update_status["last_result"] = "failed"
        _update_status["last_error"] = "Update timed out (10 minutes)"
        logger.error("Update timed out")
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
async def trigger_update(target_ref: str | None = None):
    """
    Trigger node update from GitHub.
    
    Creates a separate updater container that:
    1. Clones fresh repository
    2. Runs update.sh from the cloned version
    3. update.sh stops containers, copies files, rebuilds, restarts
    
    Args:
        target_ref: Git reference (branch/tag/commit). Default: 'main' (latest)
    """
    if _update_status["in_progress"]:
        raise HTTPException(
            status_code=409,
            detail="Update already in progress"
        )
    
    asyncio.create_task(run_update_in_container(target_ref))
    
    return {
        "success": True,
        "message": "Update started",
        "target": target_ref or "main"
    }


@router.get("/update/status")
async def get_update_status():
    """Get current update status"""
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
        "last_update_time": _update_status["last_update_time"]
    }
