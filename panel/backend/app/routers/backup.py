"""Backup & Restore — pg_dump / pg_restore through docker exec."""

import asyncio
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.auth import verify_auth
from app.config import get_settings
from app.database import async_session
from app.services.http_client import close_http_clients, init_http_clients
from app.services.pki import load_or_create_keygen

router = APIRouter(prefix="/backup", tags=["backup"])
logger = logging.getLogger(__name__)

BACKUP_DIR = Path("/app/data/backups")
POSTGRES_CONTAINER = "panel-postgres"
MAX_BACKUPS = 20

_operation_status: dict = {
    "state": "idle",
    "filename": None,
    "error": None,
    "started_at": None,
    "completed_at": None,
}


def _get_version() -> str:
    version_file = Path("/app/VERSION")
    if version_file.exists():
        return version_file.read_text().strip()
    return "unknown"


def _pg_env() -> tuple[str, str]:
    s = get_settings()
    return s.postgres_user, s.postgres_db


def _run_pg_dump() -> tuple[bytes, str | None]:
    user, db = _pg_env()
    result = subprocess.run(
        ["docker", "exec", POSTGRES_CONTAINER, "pg_dump", "-U", user, "-d", db, "-Fc"],
        capture_output=True,
        timeout=600,
    )
    if result.returncode != 0:
        return b"", result.stderr.decode(errors="replace")
    return result.stdout, None


def _run_pg_restore(data: bytes) -> str | None:
    user, db = _pg_env()

    subprocess.run(
        [
            "docker", "exec", POSTGRES_CONTAINER,
            "psql", "-U", user, "-d", db,
            "-c",
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{db}' AND pid <> pg_backend_pid();",
        ],
        capture_output=True,
        timeout=10,
    )

    drop = subprocess.run(
        [
            "docker", "exec", POSTGRES_CONTAINER,
            "psql", "-U", user, "-d", db,
            "-c", "DROP SCHEMA public CASCADE; CREATE SCHEMA public;",
        ],
        capture_output=True,
        timeout=30,
    )
    if drop.returncode != 0:
        return f"Schema reset failed: {drop.stderr.decode(errors='replace')}"

    base_cmd = ["docker", "exec", "-i", POSTGRES_CONTAINER, "pg_restore", "-U", user, "-d", db, "--no-owner"]

    # Phase 1: schema + data
    r1 = subprocess.run(
        base_cmd + ["--section=pre-data", "--section=data"],
        input=data, capture_output=True, timeout=600,
    )

    # Phase 2: clean orphaned FK references before constraints are added
    subprocess.run(
        ["docker", "exec", POSTGRES_CONTAINER, "psql", "-U", user, "-d", db, "-c", """
            DELETE FROM server_cache WHERE server_id NOT IN (SELECT id FROM servers);
            DELETE FROM metrics_snapshots WHERE server_id NOT IN (SELECT id FROM servers);
            DELETE FROM aggregated_metrics WHERE server_id NOT IN (SELECT id FROM servers);
            DELETE FROM blocklist_rules WHERE server_id IS NOT NULL AND server_id NOT IN (SELECT id FROM servers);
            DELETE FROM alert_history WHERE server_id NOT IN (SELECT id FROM servers);
        """],
        capture_output=True, timeout=60,
    )

    # Phase 3: constraints + indexes
    r3 = subprocess.run(
        base_cmd + ["--section=post-data"],
        input=data, capture_output=True, timeout=600,
    )

    errors = []
    for r in (r1, r3):
        if r.returncode == 0:
            continue
        stderr = r.stderr.decode(errors="replace").strip()
        non_warning = [
            ln for ln in stderr.splitlines()
            if "WARNING" not in ln and "pg_restore" not in ln.split(":")[0]
        ]
        if non_warning:
            errors.append(stderr)
    return "\n".join(errors) if errors else None


def _ensure_dir():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def _save_metadata(filename: str, size: int):
    meta = {
        "filename": filename,
        "size": size,
        "version": _get_version(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_path = BACKUP_DIR / f"{filename}.meta.json"
    meta_path.write_text(json.dumps(meta))


def _read_metadata(filename: str) -> dict | None:
    meta_path = BACKUP_DIR / f"{filename}.meta.json"
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text())
        except Exception:
            pass
    return None


def _cleanup_old_backups():
    dumps = sorted(BACKUP_DIR.glob("*.dump"), key=lambda p: p.stat().st_mtime)
    while len(dumps) > MAX_BACKUPS:
        old = dumps.pop(0)
        old.unlink(missing_ok=True)
        meta = BACKUP_DIR / f"{old.name}.meta.json"
        meta.unlink(missing_ok=True)


def _set_status(state: str, filename: str | None = None, error: str | None = None):
    _operation_status["state"] = state
    _operation_status["filename"] = filename
    _operation_status["error"] = error
    if state in ("creating", "restoring"):
        _operation_status["started_at"] = datetime.now(timezone.utc).isoformat()
        _operation_status["completed_at"] = None
    elif state == "idle":
        _operation_status["completed_at"] = datetime.now(timezone.utc).isoformat()


async def _create_backup_task():
    _ensure_dir()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"backup_{ts}.dump"
    _set_status("creating", filename)

    try:
        data, err = await asyncio.get_event_loop().run_in_executor(None, _run_pg_dump)
        if err or not data:
            _set_status("idle", filename, err or "Empty dump")
            logger.error(f"Backup failed: {err}")
            return

        dump_path = BACKUP_DIR / filename
        dump_path.write_bytes(data)
        _save_metadata(filename, len(data))
        _cleanup_old_backups()
        _set_status("idle", filename)
        logger.info(f"Backup created: {filename} ({len(data)} bytes)")
    except Exception as e:
        _set_status("idle", filename, str(e))
        logger.error(f"Backup error: {e}")


async def _reload_pki(app) -> None:
    """Перечитать PKI из восстановленной БД и пересоздать HTTP-клиенты."""
    try:
        await close_http_clients()
        keygen = await load_or_create_keygen(async_session)
        await init_http_clients(keygen)
        app.state.pki = keygen
        logger.info("PKI and HTTP clients reloaded after restore")
    except Exception as e:
        logger.error(f"Failed to reload PKI after restore: {e}")


async def _restore_backup_task(data: bytes, filename: str, app=None):
    _set_status("restoring", filename)
    try:
        err = await asyncio.get_event_loop().run_in_executor(
            None, _run_pg_restore, data
        )
        if err:
            _set_status("idle", filename, err)
            logger.error(f"Restore failed: {err}")
        else:
            if app is not None:
                await _reload_pki(app)
            _set_status("idle", filename)
            logger.info(f"Restore completed: {filename}")
    except Exception as e:
        _set_status("idle", filename, str(e))
        logger.error(f"Restore error: {e}")


@router.post("/create")
async def create_backup(_: dict = Depends(verify_auth)):
    if _operation_status["state"] != "idle":
        raise HTTPException(409, "Another backup operation is in progress")

    asyncio.create_task(_create_backup_task())
    return {"success": True, "message": "Backup creation started"}


@router.get("/list")
async def list_backups(_: dict = Depends(verify_auth)):
    _ensure_dir()
    backups = []
    for dump in sorted(BACKUP_DIR.glob("*.dump"), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = _read_metadata(dump.name) or {}
        backups.append({
            "filename": dump.name,
            "size": dump.stat().st_size,
            "created_at": meta.get("created_at") or datetime.fromtimestamp(
                dump.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
            "version": meta.get("version"),
        })
    return {"backups": backups, "count": len(backups)}


@router.get("/{filename}/download")
async def download_backup(filename: str, _: dict = Depends(verify_auth)):
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")

    path = BACKUP_DIR / filename
    if not path.exists() or not path.suffix == ".dump":
        raise HTTPException(404, "Backup not found")

    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
    )


@router.delete("/{filename}")
async def delete_backup(filename: str, _: dict = Depends(verify_auth)):
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")

    path = BACKUP_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Backup not found")

    path.unlink(missing_ok=True)
    meta = BACKUP_DIR / f"{filename}.meta.json"
    meta.unlink(missing_ok=True)
    return {"success": True}


@router.post("/restore")
async def restore_backup(
    request: Request,
    file: UploadFile = File(...),
    _: dict = Depends(verify_auth),
):
    if _operation_status["state"] != "idle":
        raise HTTPException(409, "Another backup operation is in progress")

    if not file.filename or not file.filename.endswith(".dump"):
        raise HTTPException(400, "Only .dump files are accepted")

    data = await file.read()
    if len(data) < 16:
        raise HTTPException(400, "File is too small to be a valid backup")

    asyncio.create_task(_restore_backup_task(data, file.filename, app=request.app))
    return {"success": True, "message": "Restore started"}


@router.get("/status")
async def backup_status(_: dict = Depends(verify_auth)):
    return {
        "state": _operation_status["state"],
        "filename": _operation_status["filename"],
        "error": _operation_status["error"],
        "started_at": _operation_status["started_at"],
        "completed_at": _operation_status["completed_at"],
    }
