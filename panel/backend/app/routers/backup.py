"""Backup & Restore — pg_dump / pg_restore through docker exec."""

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app import crypto

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
# Именно starlette-версия: fastapi.UploadFile — её наследник, и парсер формы
# отдаёт базовый класс, который isinstance по наследнику не признаёт
from starlette.datastructures import UploadFile

from app.auth import verify_auth
from app.config import get_settings
from app.database import async_session, async_session_maker, get_db
from app.services.http_client import close_http_clients, init_http_clients
from app.services.pki import load_or_create_keygen

router = APIRouter(prefix="/backup", tags=["backup"])
logger = logging.getLogger(__name__)

BACKUP_DIR = Path("/app/data/backups")
POSTGRES_CONTAINER = "panel-postgres"
MAX_BACKUPS = 20

# Ключ шифрования секретов едет внутри .dump заголовком: без него восстановленный
# на новом хосте дамп не расшифровал бы mTLS-ключи (потеря доступа ко всем нодам).
# Дамп всё равно содержит эти ключи — самодостаточность бэкапа важнее.
PANEL_ENV_PATH = Path("/opt/monitoring-panel/.env")
_BACKUP_MAGIC = b"MONBKP1\n"  # сырой pg_dump начинается с "PGDMP" — легаси-дампы отличимы

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

    # Единственный надёжный признак провала — код возврата: свою диагностику
    # pg_restore печатает с префиксом «pg_restore:», и отсев строк по имени
    # программы вычищал бы ровно то, ради чего сообщение и читают. Схема к этому
    # моменту уже уничтожена DROP SCHEMA, поэтому «успех» вместо ошибки означает
    # молча опустевшую базу.
    errors = []
    for phase, r in (("pre-data+data", r1), ("post-data", r3)):
        if r.returncode == 0:
            continue
        stderr = r.stderr.decode(errors="replace").strip()
        meaningful = "\n".join(ln for ln in stderr.splitlines() if "WARNING" not in ln)
        errors.append(
            f"pg_restore {phase}: exit code {r.returncode}\n{meaningful or stderr or 'no output'}"
        )
    return "\n\n".join(errors) if errors else None


def _frame_backup(dump: bytes, key: str | None) -> bytes:
    """Вшить ключ шифрования в начало дампа. Без ключа — сырой pg_dump (легаси)."""
    if not key:
        return dump
    return _BACKUP_MAGIC + key.encode() + b"\n" + dump


def _unframe_backup(data: bytes) -> tuple[bytes, str | None]:
    """Достать вшитый ключ и вернуть чистый дамп. Легаси-дамп — как есть, ключа нет."""
    if not data.startswith(_BACKUP_MAGIC):
        return data, None
    rest = data[len(_BACKUP_MAGIC):]
    key_line, _, dump = rest.partition(b"\n")
    return dump, key_line.decode() or None


def _persist_enc_key(key: str) -> None:
    """Записать PANEL_ENC_KEY в .env панели и перечитать его в процессе.

    После полного restore все секреты в БД зашифрованы ключом из набора — панель
    обязана использовать именно его, иначе не расшифрует mTLS-ключи."""
    try:
        lines = PANEL_ENV_PATH.read_text().splitlines() if PANEL_ENV_PATH.exists() else []
        for i, ln in enumerate(lines):
            if ln.startswith("PANEL_ENC_KEY="):
                lines[i] = f"PANEL_ENC_KEY={key}"
                break
        else:
            lines.append(f"PANEL_ENC_KEY={key}")
        PANEL_ENV_PATH.write_text("\n".join(lines) + "\n")
    except OSError as e:
        logger.error(f"Could not persist PANEL_ENC_KEY to .env: {e}")
    os.environ["PANEL_ENC_KEY"] = key
    crypto.reload_key()


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


def _claim_operation(state: str, filename: str | None):
    """Занять слот операции.

    Между проверкой и захватом не должно быть ни одного await: пока статус
    выставлялся внутри фоновой задачи, два запроса подряд успевали пройти
    проверку до старта первой и запускали два параллельных DROP SCHEMA.
    """
    if _operation_status["state"] != "idle":
        raise HTTPException(409, "Another backup operation is in progress")
    _set_status(state, filename)


def _set_status(state: str, filename: str | None = None, error: str | None = None):
    _operation_status["state"] = state
    _operation_status["filename"] = filename
    _operation_status["error"] = error
    if state in ("creating", "restoring"):
        _operation_status["started_at"] = datetime.now(timezone.utc).isoformat()
        _operation_status["completed_at"] = None
    elif state == "idle":
        _operation_status["completed_at"] = datetime.now(timezone.utc).isoformat()


async def _create_backup_task(filename: str):
    _ensure_dir()
    try:
        data, err = await asyncio.get_event_loop().run_in_executor(None, _run_pg_dump)
        if err or not data:
            _set_status("idle", filename, err or "Empty dump")
            logger.error(f"Backup failed: {err}")
            return

        key = os.environ.get("PANEL_ENC_KEY") if crypto.encryption_enabled() else None
        framed = _frame_backup(data, key)
        dump_path = BACKUP_DIR / filename
        dump_path.write_bytes(framed)
        _save_metadata(filename, len(framed))
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
    try:
        dump, embedded_key = _unframe_backup(data)
        err = await asyncio.get_event_loop().run_in_executor(
            None, _run_pg_restore, dump
        )
        if err:
            _set_status("idle", filename, err)
            logger.error(f"Restore failed: {err}")
        else:
            # Ключ из набора нужен ДО чтения PKI: восстановленные mTLS-ключи
            # зашифрованы им. На том же хосте ключ совпадает — перезапись пропускается.
            if embedded_key and embedded_key != os.environ.get("PANEL_ENC_KEY"):
                _persist_enc_key(embedded_key)
            if app is not None:
                await _reload_pki(app)
            _set_status("idle", filename)
            logger.info(f"Restore completed: {filename}")
    except Exception as e:
        _set_status("idle", filename, str(e))
        logger.error(f"Restore error: {e}")


@router.post("/create")
async def create_backup(_: dict = Depends(verify_auth)):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"backup_{ts}.dump"

    _claim_operation("creating", filename)
    asyncio.create_task(_create_backup_task(filename))
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
    _: dict = Depends(verify_auth),
):
    """Форму разбираем вручную, а не через `UploadFile = File(...)`.

    FastAPI вычитывает тело запроса до того, как отработают зависимости: с
    объявленным body-параметром аноним успевал бы залить дамп целиком и только
    потом получить 401 — эндпоинт открыт снаружи и лимит размера на нём поднят.
    """
    if _operation_status["state"] != "idle":
        raise HTTPException(409, "Another backup operation is in progress")

    async with request.form() as form:
        upload = form.get("file")
        if not isinstance(upload, UploadFile) or not (upload.filename or "").endswith(".dump"):
            raise HTTPException(400, "Only .dump files are accepted")

        filename = upload.filename
        data = await upload.read()

    if len(data) < 16:
        raise HTTPException(400, "File is too small to be a valid backup")

    _claim_operation("restoring", filename)
    asyncio.create_task(_restore_backup_task(data, filename, app=request.app))
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


# ==================== Автобэкапы в Telegram ====================


class BackupSettingsIn(BaseModel):
    enabled: Optional[bool] = None
    schedule_kind: Optional[str] = None
    at_time: Optional[str] = None
    every_hours: Optional[int] = None
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None
    archive_password: Optional[str] = None
    volume_size_mb: Optional[int] = None


@router.get("/settings")
async def get_backup_settings(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    from app.services.backup_scheduler import get_or_create_settings
    s = await get_or_create_settings(db)
    return {
        "enabled": s.enabled,
        "schedule_kind": s.schedule_kind,
        "at_time": s.at_time,
        "every_hours": s.every_hours,
        "chat_id": s.chat_id,
        "volume_size_mb": s.volume_size_mb,
        "has_bot_token": bool(s.bot_token),
        "has_archive_password": bool(s.archive_password),
        "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
        "last_status": s.last_status,
        "last_error": s.last_error,
    }


@router.put("/settings")
async def update_backup_settings(
    req: BackupSettingsIn,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    from app.services.backup_scheduler import get_or_create_settings
    s = await get_or_create_settings(db)
    if req.schedule_kind is not None:
        if req.schedule_kind not in ("daily", "every_hours"):
            raise HTTPException(400, "schedule_kind: daily | every_hours")
        s.schedule_kind = req.schedule_kind
    if req.at_time is not None:
        s.at_time = req.at_time.strip() or "04:00"
    if req.every_hours is not None:
        s.every_hours = max(1, req.every_hours)
    if req.chat_id is not None:
        s.chat_id = req.chat_id.strip() or None
    if req.volume_size_mb is not None:
        s.volume_size_mb = min(49, max(1, req.volume_size_mb))
    if req.bot_token is not None:
        s.bot_token = req.bot_token.strip() or None
    if req.archive_password is not None:
        s.archive_password = req.archive_password or None
    if req.enabled is not None:
        s.enabled = req.enabled
    if s.enabled and (not s.bot_token or not s.chat_id or not s.archive_password):
        raise HTTPException(400, "Для автобэкапов нужны бот-токен, чат и пароль архива")
    await db.commit()
    return {"success": True}


@router.post("/telegram-now")
async def backup_to_telegram_now(_: dict = Depends(verify_auth)):
    """Внеплановый бэкап в Telegram в фоне (может идти минуты на медленном канале)."""
    async def task():
        from app.services.backup_scheduler import get_or_create_settings, run_backup_to_telegram
        now = datetime.now(timezone.utc)
        async with async_session_maker() as db:
            s = await get_or_create_settings(db)
            ok, err = await run_backup_to_telegram(s)
            s.last_run_at = now
            s.last_status = "ok" if ok else "error"
            s.last_error = err
            await db.commit()

    asyncio.create_task(task())
    return {"success": True, "message": "Backup to Telegram started"}


@router.post("/test-telegram")
async def test_telegram(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    from app.services import backup_telegram
    from app.services.backup_scheduler import get_or_create_settings
    s = await get_or_create_settings(db)
    if not s.bot_token or not s.chat_id:
        raise HTTPException(400, "Не заданы бот-токен и чат")
    ok = await backup_telegram.send_message(
        s.bot_token, s.chat_id, "✅ Тест: панель мониторинга подключена к каналу бэкапов"
    )
    if not ok:
        raise HTTPException(400, "Не удалось отправить в Telegram — проверьте токен и chat_id")
    return {"success": True}
