"""Backup & Restore — pg_dump / pg_restore through docker exec."""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Optional

from app import crypto

from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
# Именно starlette-версия: fastapi.UploadFile — её наследник, и парсер формы
# отдаёт базовый класс, который isinstance по наследнику не признаёт
from starlette.datastructures import UploadFile

from app.auth import revoke_sessions_after_restore, verify_auth
from app.config import get_settings
from app.database import async_session, async_session_maker, database_maintenance, get_db
from app.services import backup_telegram
from app.services.fleet_history import forget_fleet_history
from app.services.http_client import close_http_clients, init_http_clients
from app.services.pki import load_or_create_keygen

router = APIRouter(prefix="/backup", tags=["backup"])
logger = logging.getLogger(__name__)

BACKUP_DIR = Path("/app/data/backups")
POSTGRES_CONTAINER = "panel-postgres"
MAX_BACKUPS = 20
PG_DUMP_TIMEOUT_SEC = 600
PG_RESTORE_PHASE_TIMEOUT_SEC = 600
# Сырые замеры раз в 10 секунд живут всего сутки, но при 2000 нодах это ~17 млн
# строк и ~5 ГБ: дамп с ними не укладывался ни в таймаут, ни в память. Из дампа
# выпадают только строки — определение таблицы остаётся, и restore создаёт её
# пустой; часовые и суточные агрегаты едут в бэкапе целиком.
RAW_METRICS_TABLE = "metrics_snapshots"
# В бэкапе лежит весь .env панели, поэтому при заданном пароле архива файл
# шифруется им целиком и получает добавочное расширение: backup_<дата>.dump.enc.
# Без пароля бэкап остаётся открытым .dump.
PLAIN_SUFFIX = ".dump"
ENCRYPTED_SUFFIX = ".dump.enc"
# Бэкап пишется под временным именем: в список, под скачивание и под чистку
# старых попадают только готовые файлы обоих видов
PARTIAL_SUFFIX = ".part"
MIN_BACKUP_BYTES = 16
COPY_CHUNK_BYTES = 1024 * 1024

# Весь .env панели едет внутри .dump заголовком — чтобы бэкапом можно было
# восстановить панель полностью (логин, секреты, ключ шифрования). Без ключа
# шифрования дамп не расшифровал бы mTLS-ключи (потеря доступа к нодам). Инфра-
# специфичные ключи при restore НЕ применяются — иначе чужой POSTGRES_PASSWORD
# оторвал бы панель от её же postgres.
PANEL_ENV_PATH = Path("/opt/monitoring-panel/.env")
_BACKUP_MAGIC = b"MONBKP1\n"     # v1: только PANEL_ENC_KEY (легаси dev-бэкапы)
_BACKUP_MAGIC_V2 = b"MONBKP2\n"  # v2: весь .env; сырой pg_dump начинается с "PGDMP"
# Строка заголовка — длина .env (v2) или ключ (v1). Потолок — чтобы битый файл
# без перевода строки не читался в память до конца.
_FRAME_LINE_MAX_BYTES = 256
# Не восстанавливаем из бэкапа — специфичны для конкретной установки/postgres
_ENV_PRESERVE = {
    "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
    "DOMAIN", "PANEL_PORT", "PANEL_HTTP_PORT", "MON_IMAGE_TAG",
}

_operation_status: dict = {
    "state": "idle",
    "operation": None,  # create | restore — какая операция шла/идёт; переживает возврат в idle
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


def _run_pg_dump(target: BinaryIO) -> tuple[int, str | None]:
    """Снять дамп в конец target; вернуть размер дампа и причину провала.

    pg_dump пишет в сам дескриптор файла: дамп в сотни мегабайт не проходит
    через память бэкенда.
    """
    user, db = _pg_env()
    started = time.monotonic()
    # Дочерний процесс пишет с позиции дескриптора — буфер Python к этому
    # моменту должен быть на диске
    target.flush()
    dump_start = target.tell()
    try:
        result = subprocess.run(
            [
                "docker", "exec", POSTGRES_CONTAINER, "pg_dump", "-U", user, "-d", db, "-Fc",
                f"--exclude-table-data={RAW_METRICS_TABLE}",
            ],
            stdout=target,
            stderr=subprocess.PIPE,
            timeout=PG_DUMP_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        # Обычный провал, а не исключение: вылетев наружу, таймаут обходил запись
        # запуска автобэкапа, и расписание стартовало новый дамп каждую минуту
        return 0, f"pg_dump timed out after {PG_DUMP_TIMEOUT_SEC} s"
    if result.returncode != 0:
        return 0, result.stderr.decode(errors="replace")
    dump_bytes = target.seek(0, os.SEEK_END) - dump_start
    logger.info("pg_dump finished: %d bytes in %.1f s", dump_bytes, time.monotonic() - started)
    return dump_bytes, None


def _pg_restore_phase(backup: BinaryIO, dump_offset: int, *sections: str) -> subprocess.CompletedProcess:
    """Одна фаза pg_restore: дамп идёт на stdin прямо из файла, мимо памяти бэкенда."""
    user, db = _pg_env()
    # docker получает сам дескриптор и читает с его позиции — её и ставим,
    # а не позицию буфера Python
    os.lseek(backup.fileno(), dump_offset, os.SEEK_SET)
    return subprocess.run(
        [
            "docker", "exec", "-i", POSTGRES_CONTAINER, "pg_restore", "-U", user, "-d", db, "--no-owner",
            *(f"--section={section}" for section in sections),
        ],
        stdin=backup, capture_output=True, timeout=PG_RESTORE_PHASE_TIMEOUT_SEC,
    )


def _run_pg_restore(backup: BinaryIO, dump_offset: int) -> str | None:
    """Восстановить базу из дампа, который начинается в файле backup с dump_offset."""
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

    # Phase 1: schema + data
    r1 = _pg_restore_phase(backup, dump_offset, "pre-data", "data")

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
    r3 = _pg_restore_phase(backup, dump_offset, "post-data")

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


def _frame_header(env_text: str | None) -> bytes:
    """Заголовок с целым .env, который встаёт перед дампом. Без .env — пусто: сырой pg_dump (легаси)."""
    if not env_text:
        return b""
    env_b = env_text.encode("utf-8")
    return _BACKUP_MAGIC_V2 + str(len(env_b)).encode() + b"\n" + env_b


def write_backup(target: BinaryIO) -> tuple[int, str | None]:
    """Бэкап в файл: заголовок с .env, следом pg_dump сам дописывает дамп.

    Возвращает размер дампа без заголовка и причину провала.
    """
    target.write(_frame_header(read_panel_env()))
    return _run_pg_dump(target)


def _read_frame(backup: BinaryIO) -> tuple[int, dict]:
    """Смещение чистого дампа в файле бэкапа и словарь восстанавливаемого .env.

    v2 — весь .env; v1 — только PANEL_ENC_KEY; сырой pg_dump — .env пуст."""
    backup.seek(0)
    magic = backup.read(len(_BACKUP_MAGIC_V2))
    if magic == _BACKUP_MAGIC_V2:
        env_len = int(backup.readline(_FRAME_LINE_MAX_BYTES))
        env_b = backup.read(env_len)
        return backup.tell(), parse_env(env_b.decode("utf-8"))
    if magic == _BACKUP_MAGIC:
        key = backup.readline(_FRAME_LINE_MAX_BYTES).removesuffix(b"\n").decode()
        return backup.tell(), ({"PANEL_ENC_KEY": key} if key else {})
    return 0, {}


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


def _is_backup_name(name: str) -> bool:
    return name.endswith((PLAIN_SUFFIX, ENCRYPTED_SUFFIX))


def _ready_backups() -> list[Path]:
    """Готовые бэкапы, открытые и зашифрованные, от старых к новым."""
    ready = [path for path in BACKUP_DIR.iterdir() if _is_backup_name(path.name)]
    return sorted(ready, key=lambda path: path.stat().st_mtime)


def _cleanup_old_backups():
    dumps = _ready_backups()
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
    _operation_status["operation"] = "create" if state == "creating" else "restore"
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


def _write_backup_file(path: Path, password: str | None) -> tuple[int, str | None]:
    """Бэкап в файл на томе; с паролем архива — зашифрованный им."""
    if not password:
        with path.open("wb") as target:
            return write_backup(target)
    # Открытый бэкап существует только в безымянном временном файле вне тома
    # бэкапов: на том ложится уже шифртекст
    with tempfile.TemporaryFile() as plain:
        dump_bytes, err = write_backup(plain)
        if err or not dump_bytes:
            return dump_bytes, err
        with path.open("wb") as target:
            backup_telegram.encrypt_file(plain, password, target)
    return dump_bytes, None


async def _archive_password() -> str | None:
    """Пароль архива из настроек автобэкапа — им же шифруются локальные бэкапы."""
    from app.services.backup_scheduler import get_or_create_settings
    async with async_session_maker() as db:
        return (await get_or_create_settings(db)).archive_password


async def _create_backup_task(filename: str, password: str | None = None):
    _ensure_dir()
    partial = BACKUP_DIR / f"{filename}{PARTIAL_SUFFIX}"
    try:
        # Слот операции один, значит любой *.part здесь — след прерванного
        # бэкапа (рестарт посреди дампа), сам он с тома не исчезнет
        for leftover in BACKUP_DIR.glob(f"*{PARTIAL_SUFFIX}"):
            leftover.unlink(missing_ok=True)

        dump_bytes, err = await asyncio.get_event_loop().run_in_executor(
            None, _write_backup_file, partial, password
        )
        if err or not dump_bytes:
            _set_status("idle", filename, err or "Empty dump")
            logger.error(f"Backup failed: {err}")
            return

        size = partial.stat().st_size
        partial.replace(BACKUP_DIR / filename)
        _save_metadata(filename, size)
        _cleanup_old_backups()
        _set_status("idle", filename)
        logger.info(f"Backup created: {filename} ({dump_bytes} bytes)")
    except Exception as e:
        _set_status("idle", filename, str(e))
        logger.error(f"Backup error: {e}")
    finally:
        partial.unlink(missing_ok=True)


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


async def _restore_backup_task(backup: BinaryIO, filename: str, app=None):
    """Восстановить базу из файла бэкапа; файл закрывается здесь при любом исходе."""
    try:
        dump_offset, env_dict = _read_frame(backup)
        # Пока идёт pg_restore, панель не должна трогать базу — иначе фоновые
        # записи попадают между фазами и ломают первичные ключи
        async with database_maintenance("database restore in progress"):
            err = await asyncio.get_event_loop().run_in_executor(
                None, _run_pg_restore, backup, dump_offset
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
            await revoke_sessions_after_restore()
            _set_status("idle", filename)
            logger.info(f"Restore completed: {filename}")
    except Exception as e:
        _set_status("idle", filename, str(e))
        logger.error(f"Restore error: {e}")
    finally:
        backup.close()
        # Схема сбрасывается и при провале restore: бакеты истории парка,
        # посчитанные по прежней базе, больше ничему не соответствуют
        forget_fleet_history()


@router.post("/create")
async def create_backup(_: dict = Depends(verify_auth)):
    password = await _archive_password()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"backup_{ts}{ENCRYPTED_SUFFIX if password else PLAIN_SUFFIX}"

    _claim_operation("creating", filename)
    asyncio.create_task(_create_backup_task(filename, password))
    return {"success": True, "message": "Backup creation started"}


@router.get("/list")
async def list_backups(_: dict = Depends(verify_auth)):
    _ensure_dir()
    backups = []
    for dump in reversed(_ready_backups()):
        meta = _read_metadata(dump.name) or {}
        backups.append({
            "filename": dump.name,
            "encrypted": dump.name.endswith(ENCRYPTED_SUFFIX),
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
    if not path.exists() or not _is_backup_name(filename):
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


@dataclass(frozen=True)
class RestorePassword:
    """Пароль к зашифрованному бэкапу: введённый в форме или взятый из настроек."""
    value: str
    from_settings: bool = False


def _decrypt_archive(volumes: list[BinaryIO], filenames: list[str], password: RestorePassword, target: BinaryIO) -> None:
    """Зашифрованный бэкап → чистый бэкап в target.

    Это набор томов из Telegram или локальный .dump.enc одним файлом — формат
    один, распознан по сигнатуре первого файла. Подлинность проверяется до
    возврата: с неверным паролем до базы дело не доходит.
    """
    missing = backup_telegram.missing_volume_numbers(filenames)
    if missing:
        numbers = ", ".join(f".{n:03d}" for n in missing)
        raise HTTPException(400, f"Неполный набор: не хватает тома {numbers} — выберите все тома набора")
    if not password.value:
        raise HTTPException(400, "Бэкап зашифрован — укажите пароль архива")
    try:
        backup_telegram.decrypt_volumes(volumes, password.value, target)
    except (InvalidTag, ValueError):
        if password.from_settings:
            raise HTTPException(
                400, "Сохранённый пароль архива не подошёл — введите пароль, которым зашифрован этот бэкап"
            )
        raise HTTPException(400, "Не удалось расшифровать: неверный пароль или неполный/перепутанный набор томов")


def _stage_backup(uploads: list[BinaryIO], filenames: list[str], password: RestorePassword) -> BinaryIO:
    """Загруженные файлы → один файл бэкапа на диске, с которого читает pg_restore.

    Загруженное закрывается вместе с запросом, а восстановление идёт после
    ответа, поэтому бэкап перекладывается в свой файл: зашифрованный сразу
    расшифровывается в него, обычный дамп копируется. Файл безымянный —
    исчезает с закрытием, даже если процесс убит посреди восстановления.
    """
    staged = tempfile.TemporaryFile()
    try:
        if backup_telegram.is_encrypted_set(uploads[0]):
            _decrypt_archive(uploads, filenames, password, staged)
        elif len(uploads) > 1:
            raise HTTPException(
                400, "Несколько файлов принимаются только как тома бэкапа из Telegram — обычный .dump загружайте одним файлом"
            )
        else:
            shutil.copyfileobj(uploads[0], staged, COPY_CHUNK_BYTES)
        if staged.tell() < MIN_BACKUP_BYTES:
            raise HTTPException(400, "Файл слишком мал для бэкапа")
        staged.flush()
    except BaseException:
        staged.close()
        raise
    return staged


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
        password = RestorePassword(str(raw_password).strip() if raw_password else "")
        # Тома набора идут по имени: …enc.001 < …enc.002 < …
        uploads.sort(key=lambda u: u.filename or "")
        filenames = [u.filename or "" for u in uploads]
        filename = filenames[0] or "backup"
        # Поле пароля оставили пустым: бэкап этой же панели открывается её паролем архива
        if not password.value and backup_telegram.is_encrypted_set(uploads[0].file):
            password = RestorePassword(await _archive_password() or "", from_settings=True)
        # В потоке: расшифровка и запись сотен мегабайт остановили бы цикл событий
        staged = await asyncio.to_thread(_stage_backup, [u.file for u in uploads], filenames, password)

    try:
        _claim_operation("restoring", filename)
    except HTTPException:
        staged.close()
        raise
    asyncio.create_task(_restore_backup_task(staged, filename, app=request.app))
    return {"success": True, "message": "Restore started"}


@router.get("/status")
async def backup_status(_: dict = Depends(verify_auth)):
    return {
        "state": _operation_status["state"],
        "operation": _operation_status["operation"],
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
        from app.services.backup_scheduler import get_or_create_settings, run_and_record
        async with async_session_maker() as db:
            s = await get_or_create_settings(db)
        await run_and_record(s, datetime.now(timezone.utc))

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
