"""System management endpoints - version info, updates, and host command execution"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import docker
from docker.errors import DockerException, ImageNotFound
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services.host_executor import get_host_executor, MAX_TIMEOUT, DEFAULT_TIMEOUT

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

echo "[INFO] Docker Compose version:"
docker compose version

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


class ExecuteRequest(BaseModel):
    """Request model for command execution"""
    command: str = Field(..., min_length=1, max_length=10000, description="Shell command to execute")
    timeout: int = Field(DEFAULT_TIMEOUT, ge=1, le=MAX_TIMEOUT, description="Timeout in seconds")
    shell: str = Field("sh", pattern="^(sh|bash)$", description="Shell to use (sh or bash)")


class ExecuteResponse(BaseModel):
    """Response model for command execution"""
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    execution_time_ms: int
    error: Optional[str] = None


@router.post("/execute", response_model=ExecuteResponse)
async def execute_command(request: ExecuteRequest):
    """
    Execute a shell command on the host system.
    
    Uses nsenter to run commands in the host namespace from Docker container.
    Requires container to have privileged: true and pid: host.
    
    Args:
        command: Shell command to execute
        timeout: Timeout in seconds (1-600, default 30)
        shell: Shell to use (sh or bash)
    
    Returns:
        ExecuteResponse with stdout, stderr, exit_code and execution time
    
    Examples:
        - sysctl -p /etc/sysctl.d/99-network-tuning.conf
        - systemctl restart nginx
        - cat /etc/os-release
    """
    executor = get_host_executor()
    
    result = await executor.execute(
        command=request.command,
        timeout=request.timeout,
        shell=request.shell
    )
    
    return ExecuteResponse(
        success=result.success,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        execution_time_ms=result.execution_time_ms,
        error=result.error
    )


@router.post("/execute-stream")
async def execute_command_stream(request: ExecuteRequest):
    """
    Execute a shell command on the host system with streaming output (SSE).
    
    Returns Server-Sent Events with real-time stdout/stderr output.
    
    SSE Event types:
        - stdout: {"line": "output line"}
        - stderr: {"line": "error line"}
        - done: {"exit_code": 0, "execution_time_ms": 1234, "success": true}
        - error: {"message": "error description"}
    
    Args:
        command: Shell command to execute
        timeout: Timeout in seconds (1-600, default 30)
        shell: Shell to use (sh or bash)
    """
    executor = get_host_executor()
    
    async def event_generator():
        async for event in executor.execute_stream(
            command=request.command,
            timeout=request.timeout,
            shell=request.shell
        ):
            yield event
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


# ==================== System Optimizations ====================

SYSCTL_CONFIG_PATH = "/etc/sysctl.d/99-vless-tuning.conf"
LIMITS_CONFIG_PATH = "/etc/security/limits.d/99-nofile.conf"
SYSTEMD_LIMITS_PATH = "/etc/systemd/system.conf.d/limits.conf"
SYSTEMD_USER_LIMITS_PATH = "/etc/systemd/system/user-.slice.d/limits.conf"
OPTIMIZATIONS_VERSION_PATH = "/opt/monitoring-node/configs/VERSION"


async def read_host_file(path: str) -> Optional[str]:
    """Read file from host filesystem via nsenter"""
    executor = get_host_executor()
    result = await executor.execute(f"cat {path}", timeout=5)
    if result.success and result.exit_code == 0:
        return result.stdout
    return None


async def write_host_file(path: str, content: str) -> bool:
    """Write file to host filesystem via nsenter"""
    executor = get_host_executor()
    
    escaped_content = content.replace("'", "'\"'\"'")
    
    result = await executor.execute(
        f"mkdir -p $(dirname {path}) && cat > {path} << 'EOFCONFIG'\n{content}\nEOFCONFIG",
        timeout=10,
        shell="bash"
    )
    return result.success and result.exit_code == 0


@router.get("/versions")
async def get_all_versions():
    """
    Combined endpoint: returns node version and optimizations version in one request.
    
    This reduces the number of API calls from panel (1 instead of 2 per node).
    """
    # Get node version
    node_version = get_current_version()
    
    # Get optimizations info (parallel reads)
    opt_version_task = read_host_file(OPTIMIZATIONS_VERSION_PATH)
    sysctl_task = read_host_file(SYSCTL_CONFIG_PATH)
    
    opt_version_raw, sysctl_content = await asyncio.gather(
        opt_version_task, sysctl_task
    )
    
    opt_version = opt_version_raw.strip() if opt_version_raw else None
    opt_installed = sysctl_content is not None
    
    return {
        "node_version": node_version if node_version != "unknown" else None,
        "optimizations": {
            "installed": opt_installed,
            "version": opt_version
        }
    }


@router.get("/optimizations/version")
async def get_optimizations_version():
    """
    Get current system optimizations version from the node.
    
    Reads version from /opt/monitoring-node/configs/VERSION file.
    Falls back to checking if sysctl config exists for installed status.
    
    Note: Prefer using /api/system/versions which combines node + optimizations.
    """
    # Read version from dedicated VERSION file
    version = await read_host_file(OPTIMIZATIONS_VERSION_PATH)
    if version:
        version = version.strip()
    
    # Check if optimizations are installed (sysctl config exists)
    sysctl_content = await read_host_file(SYSCTL_CONFIG_PATH)
    installed = sysctl_content is not None
    
    return {
        "installed": installed,
        "version": version if version else None
    }


class ApplyOptimizationsRequest(BaseModel):
    """Request model for applying optimizations"""
    sysctl_content: str = Field(..., min_length=10, description="Sysctl config content")
    limits_content: str = Field(..., min_length=10, description="Limits config content")
    systemd_content: str = Field(..., min_length=10, description="Systemd limits content")
    version: Optional[str] = Field(None, description="Optimizations version")


@router.post("/optimizations/apply")
async def apply_optimizations(request: ApplyOptimizationsRequest):
    """
    Apply system optimizations to the node.
    
    Writes config files and applies sysctl settings.
    """
    executor = get_host_executor()
    errors = []
    
    # Write sysctl config
    if not await write_host_file(SYSCTL_CONFIG_PATH, request.sysctl_content):
        errors.append("Failed to write sysctl config")
    
    # Write limits config
    if not await write_host_file(LIMITS_CONFIG_PATH, request.limits_content):
        errors.append("Failed to write limits config")
    
    # Write systemd limits
    if not await write_host_file(SYSTEMD_LIMITS_PATH, request.systemd_content):
        errors.append("Failed to write systemd limits")
    
    # Write systemd user slice limits
    user_slice_content = request.systemd_content.replace("[Manager]", "[Slice]")
    result = await executor.execute(
        f"mkdir -p /etc/systemd/system/user-.slice.d",
        timeout=5
    )
    if not await write_host_file(SYSTEMD_USER_LIMITS_PATH, user_slice_content):
        errors.append("Failed to write systemd user slice limits")
    
    # Save optimizations version to file
    if request.version:
        await executor.execute("mkdir -p /opt/monitoring-node/configs", timeout=5)
        if not await write_host_file(OPTIMIZATIONS_VERSION_PATH, request.version + "\n"):
            errors.append("Failed to write version file")
    
    # Apply sysctl settings
    apply_result = await executor.execute(
        f"sysctl -p {SYSCTL_CONFIG_PATH}",
        timeout=30
    )
    if not apply_result.success or apply_result.exit_code != 0:
        if apply_result.stderr:
            logger.warning(f"Some sysctl settings may not be applied: {apply_result.stderr}")
    
    # Reload systemd
    await executor.execute("systemctl daemon-reload", timeout=10)
    
    # Load conntrack module
    await executor.execute("modprobe nf_conntrack", timeout=5)
    
    if errors:
        raise HTTPException(
            status_code=500,
            detail={"message": "Partial failure", "errors": errors}
        )
    
    return {
        "success": True,
        "message": "Optimizations applied successfully",
        "version": request.version
    }
