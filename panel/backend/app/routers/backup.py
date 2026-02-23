"""Backup & Restore — pg_dump / pg_restore through docker exec."""

import asyncio
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.auth import verify_auth
from app.config import get_settings

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
    result = subprocess.run(
        [
            "docker", "exec", "-i", POSTGRES_CONTAINER,
            "pg_restore", "-U", user, "-d", db,
            "--clean", "--if-exists", "--no-owner", "--single-transaction",
        ],
        input=data,
        capture_output=True,
        timeout=600,
    )
    stderr = result.stderr.decode(errors="replace").strip()
    if result.returncode != 0 and stderr:
        non_warning = [
            ln for ln in stderr.splitlines()
            if "WARNING" not in ln and "pg_restore" not in ln.split(":")[0]
        ]
        if non_warning:
            return stderr
    return None


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


async def _restore_backup_task(data: bytes, filename: str):
    _set_status("restoring", filename)
    try:
        err = await asyncio.get_event_loop().run_in_executor(
            None, _run_pg_restore, data
        )
        if err:
            _set_status("idle", filename, err)
            logger.error(f"Restore failed: {err}")
        else:
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

    asyncio.create_task(_restore_backup_task(data, file.filename))
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
