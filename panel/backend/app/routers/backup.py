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

from cryptography.exceptions import InvalidTag
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
from app.services import backup_telegram
from app.services.http_client import close_http_clients, init_http_clients
from app.services.pki import load_or_create_keygen

router = APIRouter(prefix="/backup", tags=["backup"])
logger = logging.getLogger(__name__)

BACKUP_DIR = Path("/app/data/backups")
POSTGRES_CONTAINER = "panel-postgres"
MAX_BACKUPS = 20

# Весь .env панели едет внутри .dump заголовком — чтобы бэкапом можно было
# восстановить панель полностью (логин, секреты, ключ шифрования). Без ключа
# шифрования дамп не расшифровал бы mTLS-ключи (потеря доступа к нодам). Инфра-
# специфичные ключи при restore НЕ применяются — иначе чужой POSTGRES_PASSWORD
# оторвал бы панель от её же postgres.
PANEL_ENV_PATH = Path("/opt/monitoring-panel/.env")
_BACKUP_MAGIC = b"MONBKP1\n"     # v1: только PANEL_ENC_KEY (легаси dev-бэкапы)
_BACKUP_MAGIC_V2 = b"MONBKP2\n"  # v2: весь .env; сырой pg_dump начинается с "PGDMP"
# Не восстанавливаем из бэкапа — специфичны для конкретной установки/postgres
_ENV_PRESERVE = {
    "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
    "DOMAIN", "PANEL_PORT", "PANEL_HTTP_PORT", "MON_IMAGE_TAG",
}

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


def parse_env(text: str) -> dict:
    """Разобрать .env в словарь KEY→VALUE (пропуская комментарии/пустые строки)."""
    result: dict = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        result[key.strip()] = value
    return result


def read_panel_env() -> str | None:
    try:
        return PANEL_ENV_PATH.read_text() if PANEL_ENV_PATH.exists() else None
    except OSError as e:
        logger.warning(f"Could not read panel .env for backup: {e}")
        return None


def _frame_backup(dump: bytes, env_text: str | None) -> bytes:
    """Вшить весь .env в начало дампа. Без .env — сырой pg_dump (легаси)."""
    if not env_text:
        return dump
    env_b = env_text.encode("utf-8")
    return _BACKUP_MAGIC_V2 + str(len(env_b)).encode() + b"\n" + env_b + dump


def _unframe_backup(data: bytes) -> tuple[bytes, dict]:
    """Вернуть чистый дамп и словарь восстанавливаемого .env.

    v2 — весь .env; v1 — только PANEL_ENC_KEY; сырой pg_dump — .env пуст."""
    if data.startswith(_BACKUP_MAGIC_V2):
        rest = data[len(_BACKUP_MAGIC_V2):]
        nl = rest.index(b"\n")
        env_len = int(rest[:nl])
        env_b = rest[nl + 1:nl + 1 + env_len]
        dump = rest[nl + 1 + env_len:]
        return dump, parse_env(env_b.decode("utf-8"))
    if data.startswith(_BACKUP_MAGIC):
        rest = data[len(_BACKUP_MAGIC):]
        key_line, _, dump = rest.partition(b"\n")
        key = key_line.decode()
        return dump, ({"PANEL_ENC_KEY": key} if key else {})
    return data, {}


def apply_restored_env(env_dict: dict) -> None:
    """Слить восстановленный .env в текущий, пропуская инфра-специфичные ключи.

    POSTGRES_*/DOMAIN/порты берутся из живой установки (иначе панель оторвётся от
    своей БД). PANEL_ENC_KEY применяется сразу — им зашифрованы восстановленные
    секреты; остальное (логин, JWT и т.п.) вступит в силу после рестарта панели."""
    applied = {k: v for k, v in env_dict.items() if k not in _ENV_PRESERVE}
    if not applied:
        return
    try:
        lines = PANEL_ENV_PATH.read_text().splitlines() if PANEL_ENV_PATH.exists() else []
        seen = set()
        out = []
        for line in lines:
            stripped = line.strip()
            if "=" in stripped and not stripped.startswith("#"):
                key = stripped.split("=", 1)[0].strip()
                if key in applied:
                    out.append(f"{key}={applied[key]}")
                    seen.add(key)
                    continue
            out.append(line)
        for key, value in applied.items():
            if key not in seen:
                out.append(f"{key}={value}")
        PANEL_ENV_PATH.write_text("\n".join(out) + "\n")
    except OSError as e:
        logger.error(f"Could not write restored .env: {e}")

    if "PANEL_ENC_KEY" in applied:
        os.environ["PANEL_ENC_KEY"] = applied["PANEL_ENC_KEY"]
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

        framed = _frame_backup(data, read_panel_env())
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
        dump, env_dict = _unframe_backup(data)
        err = await asyncio.get_event_loop().run_in_executor(
            None, _run_pg_restore, dump
        )
        if err:
            _set_status("idle", filename, err)
            logger.error(f"Restore failed: {err}")
        else:
            # .env из набора применяем ДО чтения PKI: восстановленные mTLS-ключи
            # зашифрованы ключом из набора. Инфра-специфичные ключи пропускаются;
            # логин/JWT вступят в силу после рестарта панели.
            apply_restored_env(env_dict)
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


def _decrypt_volume_set(parts: list[bytes], filenames: list[str], password: str) -> bytes:
    """Набор томов из Telegram → чистый бэкап. Набор распознан по сигнатуре первого тома."""
    missing = backup_telegram.missing_volume_numbers(filenames)
    if missing:
        numbers = ", ".join(f".{n:03d}" for n in missing)
        raise HTTPException(400, f"Неполный набор: не хватает тома {numbers} — выберите все тома набора")
    if not password:
        raise HTTPException(400, "Это набор томов из Telegram — укажите пароль архива")
    try:
        return backup_telegram.join_and_decrypt(parts, password)
    except (InvalidTag, ValueError):
        raise HTTPException(400, "Не удалось расшифровать: неверный пароль или неполный/перепутанный набор томов")


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
        uploads = [v for v in form.getlist("file") if isinstance(v, UploadFile)]
        if not uploads:
            raise HTTPException(400, "Файл не выбран")
        raw_password = form.get("password")
        password = str(raw_password).strip() if raw_password else ""
        # Тома набора идут по имени: …enc.001 < …enc.002 < …
        uploads.sort(key=lambda u: u.filename or "")
        filenames = [u.filename or "" for u in uploads]
        filename = filenames[0] or "backup"
        parts = [await u.read() for u in uploads]

    if backup_telegram.is_encrypted_set(parts[0]):
        data = _decrypt_volume_set(parts, filenames, password)
    elif len(parts) > 1:
        raise HTTPException(
            400, "Несколько файлов принимаются только как тома бэкапа из Telegram — обычный .dump загружайте одним файлом"
        )
    else:
        data = parts[0]

    if len(data) < 16:
        raise HTTPException(400, "Файл слишком мал для бэкапа")

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
