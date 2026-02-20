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


async def run_update_in_container(target_ref: str | None = None, proxy: str | None = None):
    """
    Run update in separate Docker container.
    
    Args:
        target_ref: Git reference (commit hash, tag, or branch). Default: 'main'
        proxy: HTTP proxy for git (e.g., http://127.0.0.1:3128)
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
        proxy_info = f" (via proxy: {proxy})" if proxy else ""
        logger.info(f"Starting update to: {ref_arg}{proxy_info}")
        
        # Build git proxy args
        git_proxy_args = ""
        if proxy:
            git_proxy_args = f'-c http.proxy={proxy} -c https.proxy={proxy}'
        
        # Updater script:
        # 1. Install dependencies (git for clone, nsenter for host access)
        # 2. Clone repo inside Alpine container
        # 3. Stage files to bind-mounted volume
        # 4. Run apply-update.sh ON THE HOST via nsenter (apt-get, systemctl work natively)
        updater_script = f"""#!/bin/sh
set -e

echo "[INFO] Installing dependencies..."
apk add --no-cache git curl rsync bash >/dev/null 2>&1
command -v nsenter >/dev/null 2>&1 || apk add --no-cache util-linux-misc >/dev/null 2>&1 || apk add --no-cache util-linux >/dev/null 2>&1

echo "[INFO] Docker Compose version:"
docker compose version

echo "[INFO] Cloning repository..."

GIT_PROXY_ARGS="{git_proxy_args}"
GITHUB_MIRROR="https://ghfast.top"

TMP_CLONE=/tmp/monitoring-fresh
rm -rf $TMP_CLONE
CLONE_SUCCESS=0

if [ -n "$GIT_PROXY_ARGS" ]; then
    echo "[INFO] Using proxy for git: {proxy}"
    echo "[INFO] Trying GitHub via proxy (60s timeout)..."
    if timeout 60 git $GIT_PROXY_ARGS clone --depth 1 --branch {ref_arg} "https://github.com/Joliz1337/monitoring.git" $TMP_CLONE 2>&1; then
        CLONE_SUCCESS=1
    fi
else
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
fi

if [ $CLONE_SUCCESS -eq 0 ]; then
    echo "[ERROR] Failed to clone repository"
    exit 1
fi

echo "[INFO] Staging update files..."
STAGING=/opt/monitoring-node/.update-staging
rm -rf $STAGING
cp -r $TMP_CLONE $STAGING
rm -rf $TMP_CLONE

CURRENT_VERSION="unknown"
if [ -f /opt/monitoring-node/VERSION ]; then
    CURRENT_VERSION=$(cat /opt/monitoring-node/VERSION)
fi
echo "[INFO] Current version: $CURRENT_VERSION"

if [ -f /opt/monitoring-node/.env ]; then
    cp /opt/monitoring-node/.env /opt/monitoring-node/.env.backup
    echo "[INFO] Configuration backed up"
fi

echo "[INFO] Preparing host filesystem..."
nsenter -t 1 -m -u -n -i -p -- sh -c "rm -rf /tmp/monitoring-staging && mv /opt/monitoring-node/.update-staging /tmp/monitoring-staging 2>/dev/null || (cp -r /opt/monitoring-node/.update-staging /tmp/monitoring-staging && rm -rf /opt/monitoring-node/.update-staging)"

nsenter -t 1 -m -u -n -i -p -- chmod +x /tmp/monitoring-staging/node/scripts/apply-update.sh 2>/dev/null || true

echo "[INFO] Running update on host via nsenter..."
set +e
nsenter -t 1 -m -u -n -i -p -- bash /tmp/monitoring-staging/node/scripts/apply-update.sh /tmp/monitoring-staging /opt/monitoring-node "$CURRENT_VERSION"
UPDATE_RC=$?
set -e

echo "[INFO] Cleanup..."
nsenter -t 1 -m -u -n -i -p -- rm -rf /tmp/monitoring-staging 2>/dev/null || true

if [ $UPDATE_RC -ne 0 ]; then
    echo "[ERROR] Update failed (exit code: $UPDATE_RC)"
    exit $UPDATE_RC
fi

echo "[SUCCESS] Update completed!"
"""
        
        # Build environment variables for container
        env_vars = {}
        if proxy:
            env_vars = {
                "http_proxy": proxy,
                "https_proxy": proxy,
                "HTTP_PROXY": proxy,
                "HTTPS_PROXY": proxy,
                "ALL_PROXY": proxy,
                "all_proxy": proxy,
                "UPDATE_PROXY": proxy,
            }
        
        container = client.containers.run(
            image=UPDATER_IMAGE,
            command=["sh", "-c", updater_script],
            name=UPDATER_CONTAINER_NAME,
            volumes={
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
                "/opt/monitoring-node": {"bind": "/opt/monitoring-node", "mode": "rw"},
            },
            environment=env_vars if env_vars else None,
            network_mode="host",
            privileged=True,
            pid_mode="host",
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


class UpdateRequest(BaseModel):
    """Request model for node update"""
    target_version: Optional[str] = Field(None, description="Git reference (branch/tag/commit). Default: 'main'")
    proxy: Optional[str] = Field(None, description="HTTP proxy for downloads (e.g., http://127.0.0.1:3128)")


@router.post("/update")
async def trigger_update(data: UpdateRequest = None):
    """
    Trigger node update from GitHub.
    
    Creates a separate updater container that:
    1. Clones fresh repository (using proxy if provided)
    2. Runs update.sh from the cloned version
    3. update.sh stops containers, copies files, rebuilds, restarts
    
    Request body:
        target_version: Git reference (branch/tag/commit). Default: 'main' (latest)
        proxy: HTTP proxy for git clone (e.g., http://127.0.0.1:3128)
    """
    if _update_status["in_progress"]:
        raise HTTPException(
            status_code=409,
            detail="Update already in progress"
        )
    
    target_ref = data.target_version if data else None
    proxy = data.proxy if data else None
    
    asyncio.create_task(run_update_in_container(target_ref, proxy))
    
    return {
        "success": True,
        "message": "Update started",
        "target": target_ref or "main",
        "proxy": proxy or "none"
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
NETWORK_TUNE_SCRIPT_PATH = "/opt/monitoring/scripts/network-tune.sh"
NETWORK_TUNE_SCRIPT_PATH_OLD = "/opt/monitoring-node/scripts/network-tune.sh"
NETWORK_TUNE_SERVICE_PATH = "/etc/systemd/system/network-tune.service"
PAM_SESSION_PATH = "/etc/pam.d/common-session"
OPTIMIZATIONS_VERSION_PATH = "/opt/monitoring/configs/VERSION"
OPTIMIZATIONS_VERSION_PATH_OLD = "/opt/monitoring-node/configs/VERSION"

# Expected values for verification
EXPECTED_SYSCTL_VALUES = {
    "net.netfilter.nf_conntrack_tcp_timeout_syn_sent": "10",
    "net.netfilter.nf_conntrack_tcp_timeout_syn_recv": "10",
    "net.ipv4.tcp_syn_retries": "3",
    "net.ipv4.tcp_synack_retries": "3",
    "net.ipv4.tcp_keepalive_time": "600",
    "net.ipv4.tcp_keepalive_probes": "6",
    "net.ipv4.tcp_keepalive_intvl": "15",
    "net.ipv4.tcp_congestion_control": "bbr",
    "net.core.default_qdisc": "fq",
    "net.ipv4.ip_forward": "1",
    "net.ipv4.conf.all.rp_filter": "2",
}


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
    
    result = await executor.execute(
        f"mkdir -p $(dirname {path}) && cat > {path} << 'EOFCONFIG'\n{content}\nEOFCONFIG",
        timeout=10,
        shell="bash"
    )
    return result.success and result.exit_code == 0


async def read_optimizations_version() -> Optional[str]:
    """Read optimizations VERSION, checking new path first, then legacy."""
    version = await read_host_file(OPTIMIZATIONS_VERSION_PATH)
    if not version:
        version = await read_host_file(OPTIMIZATIONS_VERSION_PATH_OLD)
    return version.strip() if version else None


@router.get("/versions")
async def get_all_versions():
    """
    Combined endpoint: returns node version and optimizations version in one request.
    
    This reduces the number of API calls from panel (1 instead of 2 per node).
    """
    node_version = get_current_version()
    
    opt_version_task = read_optimizations_version()
    sysctl_task = read_host_file(SYSCTL_CONFIG_PATH)
    
    opt_version, sysctl_content = await asyncio.gather(
        opt_version_task, sysctl_task
    )
    
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
    
    Reads version from /opt/monitoring/configs/VERSION (new path)
    with fallback to /opt/monitoring-node/configs/VERSION (legacy).
    
    Note: Prefer using /api/system/versions which combines node + optimizations.
    """
    version = await read_optimizations_version()
    
    sysctl_content = await read_host_file(SYSCTL_CONFIG_PATH)
    installed = sysctl_content is not None
    
    return {
        "installed": installed,
        "version": version
    }


class ApplyOptimizationsRequest(BaseModel):
    """Request model for applying optimizations"""
    sysctl_content: str = Field(..., min_length=10, description="Sysctl config content")
    limits_content: str = Field(..., min_length=10, description="Limits config content")
    systemd_content: str = Field(..., min_length=10, description="Systemd limits content")
    network_tune_content: Optional[str] = Field(None, description="Network tune script content")
    network_tune_service_content: Optional[str] = Field(None, description="Network tune service unit")
    version: Optional[str] = Field(None, description="Optimizations version")


async def verify_sysctl_values(executor) -> dict:
    """Verify that critical sysctl values are applied correctly"""
    verification = {"success": True, "checked": {}, "failed": []}
    
    for param, expected in EXPECTED_SYSCTL_VALUES.items():
        result = await executor.execute(f"sysctl -n {param}", timeout=5)
        if result.success and result.exit_code == 0:
            actual = result.stdout.strip()
            verification["checked"][param] = {"expected": expected, "actual": actual}
            if actual != expected:
                verification["failed"].append(f"{param}: expected {expected}, got {actual}")
                verification["success"] = False
        else:
            verification["failed"].append(f"{param}: failed to read")
            verification["success"] = False
    
    # Check conntrack hashsize (should be >= 524288 for 2M conntrack_max)
    result = await executor.execute("cat /sys/module/nf_conntrack/parameters/hashsize", timeout=5)
    if result.success and result.exit_code == 0:
        hashsize = int(result.stdout.strip())
        verification["checked"]["conntrack_hashsize"] = {"expected": ">=524288", "actual": str(hashsize)}
        if hashsize < 524288:
            verification["failed"].append(f"conntrack_hashsize: expected >=524288, got {hashsize}")
            verification["success"] = False
    
    return verification


SYSTEM_SYSCTL_PATTERNS = {"10-", "99-sysctl.conf", "99-cloudimg-", "README"}
OUR_SYSCTL_CONFIG = "99-vless-tuning.conf"
THIRD_PARTY_SERVICES = [
    "3x-ui-tuning", "xray-tuning", "marzban-tuning",
    "network-optimize", "sysctl-tuning", "tcp-tuning", "tcp-bbr",
]
THIRD_PARTY_SCRIPTS = [
    "/usr/local/bin/network-tuning.sh", "/usr/local/bin/tcp-tuning.sh",
    "/usr/local/bin/sysctl-tuning.sh", "/opt/3x-ui/tuning.sh", "/opt/marzban/tuning.sh",
]


def _is_system_sysctl(filename: str) -> bool:
    return any(filename.startswith(p) for p in SYSTEM_SYSCTL_PATTERNS) or filename == OUR_SYSCTL_CONFIG


async def cleanup_conflicting_configs(executor) -> list[str]:
    """Remove ALL non-system sysctl/limits configs and third-party tuning services"""
    cleaned = []
    
    # ---- sysctl.d: remove all non-system configs ----
    ls_result = await executor.execute("ls /etc/sysctl.d/ 2>/dev/null", timeout=5)
    if ls_result.success and ls_result.stdout:
        for fname in ls_result.stdout.strip().split("\n"):
            fname = fname.strip()
            if not fname.endswith(".conf"):
                continue
            if _is_system_sysctl(fname):
                continue
            await executor.execute(f"rm -f /etc/sysctl.d/{fname}", timeout=5)
            cleaned.append(f"sysctl.d/{fname}")
    
    # ---- /etc/sysctl.conf: remove all active parameter lines ----
    result = await executor.execute(
        r"sed -i '/^net\./d; /^fs\./d; /^vm\./d; /^kernel\./d; /^precedence/d' /etc/sysctl.conf",
        timeout=5,
    )
    if result.success and result.exit_code == 0:
        cleaned.append("sysctl.conf (cleaned)")
    
    # ---- limits.d: remove all non-system configs ----
    ls_result = await executor.execute("ls /etc/security/limits.d/ 2>/dev/null", timeout=5)
    if ls_result.success and ls_result.stdout:
        for fname in ls_result.stdout.strip().split("\n"):
            fname = fname.strip()
            if not fname.endswith(".conf") or fname == "99-nofile.conf":
                continue
            await executor.execute(f"rm -f /etc/security/limits.d/{fname}", timeout=5)
            cleaned.append(f"limits.d/{fname}")
    
    # ---- /etc/security/limits.conf: clean custom nofile/nproc/memlock lines ----
    await executor.execute(
        r"sed -i '/^\*.*nofile/d; /^root.*nofile/d; /^\*.*nproc/d; /^root.*nproc/d; "
        r"/^\*.*memlock/d; /^root.*memlock/d' /etc/security/limits.conf",
        timeout=5,
    )
    
    # ---- Stop/disable third-party tuning services ----
    for svc in THIRD_PARTY_SERVICES:
        check = await executor.execute(f"systemctl is-enabled {svc}.service 2>/dev/null", timeout=5)
        if check.exit_code == 0:
            await executor.execute(f"systemctl stop {svc}.service", timeout=10)
            await executor.execute(f"systemctl disable {svc}.service", timeout=10)
            cleaned.append(f"service:{svc}")
    
    # ---- Remove third-party tuning scripts ----
    for script in THIRD_PARTY_SCRIPTS:
        check = await executor.execute(f"test -f {script}", timeout=3)
        if check.exit_code == 0:
            await executor.execute(f"rm -f {script}", timeout=5)
            cleaned.append(f"script:{script}")
    
    # ---- Clean crontab entries that apply sysctl ----
    cron_check = await executor.execute(
        "crontab -l 2>/dev/null | grep -qE 'sysctl|network-tun|tcp-tun'", timeout=5
    )
    if cron_check.exit_code == 0:
        await executor.execute(
            "crontab -l 2>/dev/null | grep -vE 'sysctl|network-tun|tcp-tun' | crontab -",
            timeout=5,
        )
        cleaned.append("crontab (cleaned)")
    
    return cleaned


@router.post("/optimizations/apply")
async def apply_optimizations(request: ApplyOptimizationsRequest):
    """
    Apply system optimizations to the node.
    
    1. Cleans up conflicting configs from other software
    2. Writes ALL config files (sysctl, limits, systemd, network-tune.sh, network-tune.service)
    3. Configures PAM limits
    4. Applies sysctl settings
    5. Restarts network-tune service (for hashsize, RPS/RFS)
    6. Verifies all values are applied correctly
    """
    executor = get_host_executor()
    errors = []
    warnings = []
    applied_files = []
    
    # Clean up conflicting configs from other software (3X-UI, Marzban, etc.)
    cleaned = await cleanup_conflicting_configs(executor)
    if cleaned:
        applied_files.append(f"cleanup ({len(cleaned)} items)")
    
    # 1. Write sysctl config
    if await write_host_file(SYSCTL_CONFIG_PATH, request.sysctl_content):
        applied_files.append("sysctl.conf")
    else:
        errors.append("Failed to write sysctl config")
    
    # 2. Write limits config
    if await write_host_file(LIMITS_CONFIG_PATH, request.limits_content):
        applied_files.append("limits.conf")
    else:
        errors.append("Failed to write limits config")
    
    # 3. Write systemd limits
    await executor.execute("mkdir -p /etc/systemd/system.conf.d", timeout=5)
    if await write_host_file(SYSTEMD_LIMITS_PATH, request.systemd_content):
        applied_files.append("systemd-limits.conf")
    else:
        errors.append("Failed to write systemd limits")
    
    # 4. Write systemd user slice limits
    user_slice_content = request.systemd_content.replace("[Manager]", "[Slice]")
    await executor.execute("mkdir -p /etc/systemd/system/user-.slice.d", timeout=5)
    if await write_host_file(SYSTEMD_USER_LIMITS_PATH, user_slice_content):
        applied_files.append("user-slice-limits.conf")
    else:
        errors.append("Failed to write systemd user slice limits")
    
    # 5. Write network-tune.sh script
    if request.network_tune_content:
        await executor.execute("mkdir -p /opt/monitoring/scripts", timeout=5)
        if await write_host_file(NETWORK_TUNE_SCRIPT_PATH, request.network_tune_content):
            await executor.execute(f"chmod +x {NETWORK_TUNE_SCRIPT_PATH}", timeout=5)
            applied_files.append("network-tune.sh")
        else:
            errors.append("Failed to write network-tune.sh")
        # Clean up old path
        await executor.execute(f"rm -f {NETWORK_TUNE_SCRIPT_PATH_OLD}", timeout=5)
    
    # 6. Write network-tune.service
    if request.network_tune_service_content:
        if await write_host_file(NETWORK_TUNE_SERVICE_PATH, request.network_tune_service_content):
            applied_files.append("network-tune.service")
        else:
            errors.append("Failed to write network-tune.service")
    
    # 7. Configure PAM limits (for SSH sessions to respect limits.conf)
    pam_check = await executor.execute(f"grep -q 'pam_limits.so' {PAM_SESSION_PATH}", timeout=5)
    if pam_check.exit_code != 0:
        # pam_limits.so not configured, add it
        pam_result = await executor.execute(
            f"echo 'session required pam_limits.so' >> {PAM_SESSION_PATH}",
            timeout=5
        )
        if pam_result.success and pam_result.exit_code == 0:
            applied_files.append("pam-limits")
        else:
            warnings.append("Failed to configure PAM limits")
    
    # 8. Save optimizations version to file
    if request.version:
        await executor.execute("mkdir -p /opt/monitoring/configs", timeout=5)
        if await write_host_file(OPTIMIZATIONS_VERSION_PATH, request.version + "\n"):
            applied_files.append("VERSION")
        else:
            errors.append("Failed to write version file")
        # Clean up old path
        await executor.execute(f"rm -f {OPTIMIZATIONS_VERSION_PATH_OLD}", timeout=5)
    
    # 9. Load conntrack module BEFORE sysctl (required for nf_conntrack_* params)
    await executor.execute("modprobe nf_conntrack", timeout=5)
    
    # 10. Apply sysctl settings
    apply_result = await executor.execute(f"sysctl -p {SYSCTL_CONFIG_PATH}", timeout=30)
    if not apply_result.success or apply_result.exit_code != 0:
        if apply_result.stderr:
            warnings.append(f"Sysctl warnings: {apply_result.stderr.strip()}")
    
    # 11. Reload systemd (to pick up new service file and limits)
    await executor.execute("systemctl daemon-reload", timeout=10)
    
    # 12. Enable and restart network-tune service
    await executor.execute("systemctl enable network-tune.service", timeout=10)
    tune_result = await executor.execute("systemctl restart network-tune.service", timeout=30)
    if not tune_result.success or tune_result.exit_code != 0:
        # Try running script directly if service fails
        if request.network_tune_content:
            direct_result = await executor.execute(f"bash {NETWORK_TUNE_SCRIPT_PATH}", timeout=30)
            if not direct_result.success:
                warnings.append("network-tune service failed, direct execution also failed")
            else:
                warnings.append("network-tune service failed, but direct script execution succeeded")
        else:
            warnings.append("network-tune service restart failed")
    
    # Critical errors - fail the request
    if errors:
        raise HTTPException(
            status_code=500,
            detail={"message": "Partial failure", "errors": errors, "warnings": warnings, "applied": applied_files}
        )
    
    # 13. Verify all values are applied correctly
    verification = await verify_sysctl_values(executor)
    
    return {
        "success": verification["success"],
        "message": "Optimizations applied" + (" with issues" if not verification["success"] else " successfully"),
        "version": request.version,
        "applied_files": applied_files,
        "warnings": warnings if warnings else None,
        "verification": {
            "all_passed": verification["success"],
            "failed": verification["failed"] if verification["failed"] else None,
            "checked_count": len(verification["checked"])
        }
    }
