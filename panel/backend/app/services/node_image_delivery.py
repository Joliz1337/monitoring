"""Доставка образа ноды с панели на заблокированную ноду по SSH.

SFTP-заливка gzip-tar образа → docker load → `docker compose up -d` на загруженном
образе. update.sh не запускается и код на диске ноды не меняется: нода поднимается
со своим docker-compose.yml, какой бы версии он ни был. Не зависит от mTLS —
работает даже когда агент ноды лежит. К серверу с SOCKS5-прокси панель и по SSH
ходит через него.

Канал до такой ноды медленный и рвётся, поэтому доставка идёт сеансами: обрыв —
это переподключение и продолжение с места обрыва, а не заливка с нуля. Недокачанный
архив остаётся на ноде под именем с отпечатком образа, и следующая задача его
докачивает. До заливки панель убирает с ноды остатки прошлых обновлений и сверяет
свободное место, после запуска — прежний образ: сама нода чистит их только в
конце удачного обновления через агента, а до него на таких нодах дело не доходит.

Доставка идёт фоновой задачей, не привязанной к HTTP: заливка ~200 МБ по 6 Мбит
длится минуты, обрыв вкладки её не прерывает; лог стримится подписчикам и
переигрывается при переподключении (как у авторазвёртывания).
"""
from __future__ import annotations

import asyncio
import logging
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Optional

import asyncssh

from app.services.http_client import sanitize_proxy
from app.services.node_image_cache import NodeImageArchive, ensure_image, image_ref
from app.services.ssh_target import CONNECT_TIMEOUT, SSHTarget, open_ssh_connection, ssh_connect_kwargs

logger = logging.getLogger(__name__)

LOAD_TIMEOUT = 3600
UPDATE_TIMEOUT = 7200
HOUSEKEEPING_TIMEOUT = 600
# Не /tmp: на Debian 13 и Ubuntu 26.04 это tmpfs, и архив занял бы память ноды
REMOTE_DIR = "/var/tmp"
REMOTE_PREFIX = "mon-node-img-"
# Архив от прежних версий панели: имя без отпечатка, докачать его нельзя
LEGACY_REMOTE_TAR = "/tmp/mon-node-img.tar.gz"
# Образы ноды на её диске (ERE для grep): из реестра и локальная сборка старых
# версий, чей compose ещё не называл образ
NODE_IMAGE_REPOS = r"ghcr\.io/joliz1337/monitoring-node-api|monitoring-node-api"
# Причину сбоя docker печатает перед итоговой строкой команды
STEP_ERROR_TAIL_LINES = 2
MIB = 1024 * 1024
UPLOAD_CHUNK = MIB
PROGRESS_INTERVAL = 8
FREE_SPACE_MARGIN_MB = 200
# Обрывы подряд, за которые заливка не продвинулась, — после них задача сдаётся
STALLED_SESSIONS_LIMIT = 5
# Потолок сеансов на задачу: нода, у которой связь рвётся каждую минуту, не должна
# часами занимать место в общей очереди доставок
SESSIONS_LIMIT = 40
FINISHED_TTL_SECONDS = 600
DELIVERY_CONCURRENCY = 5
LOG_BUFFER_LIMIT = 5000
# Подключение к серверу за ТСПУ срывается через раз — повторный запуск обычно проходит
SSH_CONNECT_ATTEMPTS = 3
SSH_CONNECT_RETRY_DELAY = 10
# docker-compose.yml на диске ноды любой версии: у текущей агент слушает локальный
# сокет, у старой — 127.0.0.1:7500. Потолок на пробу: агент, принявший соединение
# и замолчавший, иначе держал бы цикл ожидания вечно
AGENT_PROBE_TIMEOUT_SEC = 3
AGENT_HEALTH_PROBE = (
    f"{{ curl -sf --max-time {AGENT_PROBE_TIMEOUT_SEC} --unix-socket /run/monitoring-node/api.sock http://localhost/health"
    f" || curl -sf --max-time {AGENT_PROBE_TIMEOUT_SEC} http://127.0.0.1:7500/health; }} >/dev/null 2>&1"
)


def _error_text(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def _connect_error_text(target: SSHTarget, exc: BaseException) -> str:
    # asyncssh обрывает подключение по connect_timeout через asyncio.wait_for —
    # исключение приходит без текста
    if isinstance(exc, asyncio.TimeoutError):
        return f"{target.host}:{target.port} не ответил за {CONNECT_TIMEOUT} с"
    return _error_text(exc)


async def _run_streamed(conn, command: str, timeout: int) -> AsyncIterator[dict]:
    """Выполнить команду по SSH, стримя вывод построчно. Последнее событие —
    {"type": "exit", "code": N}."""
    process = await conn.create_process(command, stderr=asyncssh.STDOUT)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            process.terminate()
            yield {"type": "exit", "code": 124}
            return
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
        except asyncio.TimeoutError:
            process.terminate()
            yield {"type": "exit", "code": 124}
            return
        if not line:
            break
        yield {"type": "log", "line": line.rstrip("\n")}
    await process.wait()
    if process.returncode is None:
        # Статус выхода не пришёл — канал закрылся вместе с подключением
        raise asyncssh.ConnectionLost("SSH-сессия закрылась до завершения команды")
    yield {"type": "exit", "code": process.returncode}


async def _run_step(conn, command: str, timeout: int, failure: str) -> AsyncIterator[dict]:
    """Команда со стримом вывода в лог. Ненулевой код — событие error с хвостом
    вывода: по нему в уведомлении видна причина, а не только номер кода."""
    tail: list[str] = []
    async for event in _run_streamed(conn, command, timeout):
        if event["type"] == "exit":
            if event["code"] != 0:
                detail = f": {' / '.join(tail)}" if tail else ""
                yield {"type": "error", "message": f"{failure} (код {event['code']}){detail}"}
            return
        line = event["line"].strip()
        if line:
            tail = [*tail, line][-STEP_ERROR_TAIL_LINES:]
        yield event


async def _run_quiet(conn, command: str, timeout: int) -> str:
    """Служебная команда без стрима в лог — её вывод разбирает панель."""
    result = await conn.run(command, check=False, timeout=timeout)
    if result.exit_status is None:
        raise asyncssh.ConnectionLost("SSH-сессия закрылась до завершения команды")
    return str(result.stdout or "")


def _sh(script: str, *args: str) -> str:
    """Скрипт для sh ноды с позиционными аргументами: оболочка входа у root может
    быть любой, а значения в текст скрипта не подставляются."""
    return "sh -c " + " ".join(shlex.quote(part) for part in (script, "mon-delivery", *args))


# Уборка остатков обновлений и отчёт о диске. $1 — архив этой доставки, $2 — keep
# (оставить его для докачки) или drop, $3 — архив прежних версий панели, $4 — маска
# имён архивов: чужие, от других образов, уже никто не докачает; $5 — доставляемый
# образ, $6 — репозитории образов ноды. Образ ноды под другим тегом (сменили канал,
# локальная сборка старой версии) image prune не видит — он снимается по имени;
# занятый контейнером образ docker rmi не удаляет
_CLEANUP_SCRIPT = """archive=$1
upload_dir=$(dirname "$archive")
docker_root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)
[ -d "$docker_root" ] || docker_root=/var/lib/docker
free_mb() { df -Pm "$1" 2>/dev/null | awk 'NR==2 {print $4}'; }
before=$(free_mb "$docker_root")
[ "$2" = drop ] && rm -f "$archive"
rm -f "$3"
find "$upload_dir" -maxdepth 1 -type f -name "$4" ! -name "$(basename "$archive")" -delete 2>/dev/null
docker image prune -f >/dev/null 2>&1
docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -E "^($6):" | grep -vxF "$5" |
    while read -r stale; do docker rmi "$stale" >/dev/null 2>&1; done
docker builder prune -f --keep-storage=500MB >/dev/null 2>&1
same=0
[ "$(stat -c %d "$docker_root" 2>/dev/null)" = "$(stat -c %d "$upload_dir" 2>/dev/null)" ] && same=1
have=$(stat -c %s "$archive" 2>/dev/null || echo 0)
echo "MON_DISK before=$before docker=$(free_mb "$docker_root") upload=$(free_mb "$upload_dir") same=$same have=$have"
"""
_DISK_REPORT = re.compile(r"MON_DISK before=(\d+) docker=(\d+) upload=(\d+) same=([01]) have=(\d+)")


@dataclass(frozen=True)
class NodeDisk:
    """Диск ноды после уборки."""
    freed_mb: int
    docker_free_mb: int
    upload_free_mb: int
    same_disk: bool  # архив и хранилище Docker лежат на одном разделе
    archive_bytes: int  # сколько байт архива этой доставки уже на ноде


def parse_disk_report(output: str) -> Optional[NodeDisk]:
    match = _DISK_REPORT.search(output)
    if match is None:
        return None
    before, docker_free, upload_free, same, have = match.groups()
    return NodeDisk(
        freed_mb=max(int(docker_free) - int(before), 0),
        docker_free_mb=int(docker_free),
        upload_free_mb=int(upload_free),
        same_disk=same == "1",
        archive_bytes=int(have),
    )


def _ceil_mb(size: int) -> int:
    return -(-max(size, 0) // MIB)


def _shortage(place: str, free_mb: int, need_mb: int) -> Optional[str]:
    if free_mb >= need_mb:
        return None
    return (
        f"{place} мало места: свободно {free_mb} МБ, образу нужно около {need_mb} МБ. "
        "Остатки прошлых обновлений панель уже убрала — освободите место на сервере"
    )


def space_shortage(disk: NodeDisk, upload_bytes: int, image_bytes: int) -> Optional[str]:
    """Чего не хватает на диске ноды, чтобы долить архив и загрузить образ.

    Образ считается один раз: docker load распаковывает архив целиком, а сколько
    слоёв окажутся новыми, заранее не узнать. Заливать до отказа нельзя — диск,
    забитый до нуля, роняет и остальные сервисы ноды.
    """
    upload_mb, image_mb = _ceil_mb(upload_bytes), _ceil_mb(image_bytes)
    if disk.same_disk:
        return _shortage("На диске ноды", disk.docker_free_mb, upload_mb + image_mb + FREE_SPACE_MARGIN_MB)
    return (
        _shortage(f"В {REMOTE_DIR} на ноде", disk.upload_free_mb, upload_mb + FREE_SPACE_MARGIN_MB)
        or _shortage("На диске Docker ноды", disk.docker_free_mb, image_mb + FREE_SPACE_MARGIN_MB)
    )


def remote_archive_path(sha256: str) -> str:
    """Имя архива на ноде несёт отпечаток: недокачанный файл другого образа за свой не сойдёт."""
    return f"{REMOTE_DIR}/{REMOTE_PREFIX}{sha256[:16]}.tar.gz"


def resume_offset(remote_size: int, archive_size: int) -> int:
    """С какого байта продолжать заливку. Кусок, в котором кончается файл на ноде,
    шлётся заново — обрыв мог оставить в нём дыру. Файл длиннее архива чужой: с нуля."""
    if not 0 < remote_size <= archive_size:
        return 0
    return (remote_size - 1) // UPLOAD_CHUNK * UPLOAD_CHUNK


def _cleanup_command(remote_tar: str, tag: str, keep_archive: bool) -> str:
    return _sh(
        _CLEANUP_SCRIPT, remote_tar, "keep" if keep_archive else "drop",
        LEGACY_REMOTE_TAR, f"{REMOTE_PREFIX}*.tar.gz", image_ref(tag), NODE_IMAGE_REPOS,
    )


def _checksum_command(remote_tar: str) -> str:
    return _sh('sha256sum "$1" 2>/dev/null | cut -d" " -f1', remote_tar)


def _load_command(remote_tar: str) -> str:
    # docker load сам распаковывает gzip. Не загрузился — архив убираем: чаще всего
    # это кончившееся место, и 200 МБ архива его только отнимают
    return _sh('docker load -i "$1" || { rc=$?; rm -f "$1"; exit $rc; }', remote_tar)


def _start_command(tag: str) -> str:
    """Поднять ноду на доставленном образе. Тег в .env приводится к доставленному,
    иначе compose полез бы в реестр.

    Итог — по факту, а не по коду compose: `up -d` ждёт healthy у api и на медленной
    ноде выходит с ошибкой, хотя агент поднялся (тогда контейнеры стартуют напрямую,
    как в apply-update.sh), а на забитом диске он может не пересоздать контейнер, и
    прежний агент продолжит отвечать. Успех — контейнер агента работает на
    доставленном образе.
    """
    return _sh(
        "cd /opt/monitoring-node || exit 1\n"
        'if grep -q "^MON_IMAGE_TAG=" .env; then\n'
        f'    sed -i "s|^MON_IMAGE_TAG=.*|MON_IMAGE_TAG={tag}|" .env || exit 1\n'
        "else\n"
        f'    echo "MON_IMAGE_TAG={tag}" >> .env || exit 1\n'
        "fi\n"
        "docker compose up -d || docker compose start || docker start monitoring-api monitoring-nginx\n"
        f"for i in $(seq 1 30); do {AGENT_HEALTH_PROBE} && break; sleep 2; done\n"
        f'delivered=$(docker image inspect -f "{{{{.Id}}}}" {image_ref(tag)} 2>/dev/null)\n'
        'running=$(docker inspect -f "{{.State.Running}} {{.Image}}" monitoring-api 2>/dev/null)\n'
        'if [ -z "$delivered" ] || [ "$running" != "true $delivered" ]; then\n'
        '    echo "[node] контейнер агента не работает на доставленном образе"\n'
        "    exit 1\n"
        "fi\n"
        'echo "[node] нода поднята на доставленном образе"'
    )


@dataclass
class _UploadState:
    """Заливка одной задачи: переживает переподключения."""
    remote_tar: str
    position: int = 0  # где заливка сейчас — для процента в логе
    reached: int = 0  # до какого байта архив доходил до ноды — продвинулся ли сеанс


def _is_connection_drop(exc: BaseException) -> bool:
    """Обрыв связи, после которого стоит переподключиться. Отказ самой ноды —
    не приняла файл, нет прав — повтором не лечится."""
    if isinstance(exc, (asyncssh.SFTPConnectionLost, asyncssh.SFTPNoConnection)):
        return True
    if isinstance(exc, (asyncssh.SFTPError, asyncssh.PermissionDenied)):
        return False
    return isinstance(exc, (OSError, asyncio.TimeoutError, asyncssh.DisconnectError, asyncssh.ChannelOpenError))


def _session_error_text(exc: BaseException) -> str:
    if isinstance(exc, asyncssh.SFTPFailure):
        # sftp-server отвечает одним словом «Failure» на любой отказ записи
        return f"нода не приняла файл образа ({_error_text(exc)}) — так бывает, когда на её диске кончилось место"
    return _error_text(exc)


async def _connect(target: SSHTarget, connect_kwargs: dict) -> AsyncIterator:
    """Подключиться с повторами. Отдаёт события лога и последним — само подключение;
    не подключился — событие error."""
    for attempt in range(1, SSH_CONNECT_ATTEMPTS + 1):
        try:
            conn = await open_ssh_connection(connect_kwargs, target.socks5_proxy)
        except asyncssh.PermissionDenied:
            yield {"type": "error", "message": "SSH: неверный логин, пароль или ключ"}
            return
        except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
            reason = _connect_error_text(target, exc)
            if attempt == SSH_CONNECT_ATTEMPTS:
                yield {"type": "error", "message": f"Ошибка SSH: {reason} (попыток: {SSH_CONNECT_ATTEMPTS})"}
                return
            yield {
                "type": "log",
                "line": f"[panel] SSH не подключился: {reason} — попытка {attempt + 1}/{SSH_CONNECT_ATTEMPTS} "
                        f"через {SSH_CONNECT_RETRY_DELAY} с",
            }
            await asyncio.sleep(SSH_CONNECT_RETRY_DELAY)
            continue
        via_proxy = f" через SOCKS5 {sanitize_proxy(target.socks5_proxy)}" if target.socks5_proxy else ""
        yield {"type": "log", "line": f"[panel] SSH к {target.host}:{target.port} установлен{via_proxy}"}
        yield conn
        return


async def _archive_matches(conn, remote_tar: str, sha256: str) -> bool:
    output = await _run_quiet(conn, _checksum_command(remote_tar), HOUSEKEEPING_TIMEOUT)
    return output.strip() == sha256


async def _upload_archive(sftp, archive_path: Path, upload: _UploadState, offset: int) -> None:
    """Залить архив с байта offset. Куски идут по порядку — следующий уходит, когда
    нода подтвердила предыдущий, — поэтому после обрыва дыра возможна только в последнем."""
    flags = asyncssh.FXF_WRITE | asyncssh.FXF_CREAT | (0 if offset else asyncssh.FXF_TRUNC)
    upload.position = offset
    async with sftp.open(upload.remote_tar, flags, encoding=None) as remote_file:
        with archive_path.open("rb") as source:
            source.seek(offset)
            while chunk := await asyncio.to_thread(source.read, UPLOAD_CHUNK):
                await remote_file.write(chunk, offset)
                offset += len(chunk)
                upload.position = offset
                upload.reached = max(upload.reached, offset)


def _percent(done: int, total: int) -> int:
    return int(done * 100 / total) if total else 100


async def _upload_with_progress(
    sftp, archive: NodeImageArchive, upload: _UploadState, offset: int,
) -> AsyncIterator[dict]:
    total = archive.size
    task = asyncio.create_task(_upload_archive(sftp, archive.path, upload, offset))
    try:
        # Раз в PROGRESS_INTERVAL с — прогресс: стрим лога не молчит (иначе прокси
        # и браузер рвут его по idle-таймауту) и виден процент
        while not task.done():
            await asyncio.wait({task}, timeout=PROGRESS_INTERVAL)
            percent = _percent(upload.position, total)
            yield {"type": "step", "step": "upload", "percent": percent}
            yield {"type": "log", "line": f"[panel] Заливка: {percent}% ({upload.position // MIB}/{total // MIB} МБ)"}
        await task  # пробросить исключение, если заливка упала
    finally:
        if not task.done():
            task.cancel()


async def _upload_verified(
    conn, archive: NodeImageArchive, upload: _UploadState, offset: int,
) -> AsyncIterator[dict]:
    """Долить архив и сверить его с исходным. Докачанный файл, не сошедшийся по
    sha256, заливается заново с начала — один раз."""
    size_mb = archive.size // MIB
    yield {"type": "step", "step": "upload", "percent": _percent(offset, archive.size)}
    if offset:
        yield {"type": "log", "line": f"[panel] На ноде уже {offset // MIB} из {size_mb} МБ этого образа — докачиваю остальное"}
    else:
        yield {"type": "log", "line": f"[panel] Заливаю образ ({size_mb} МБ) — на медленном канале это несколько минут…"}

    async with conn.start_sftp_client() as sftp:
        async for event in _upload_with_progress(sftp, archive, upload, offset):
            yield event
        if await _archive_matches(conn, upload.remote_tar, archive.sha256):
            return
        if offset:
            yield {"type": "log", "line": "[panel] Докачанный архив не совпал с исходным — заливаю заново с начала"}
            async for event in _upload_with_progress(sftp, archive, upload, 0):
                yield event
            if await _archive_matches(conn, upload.remote_tar, archive.sha256):
                return

    await _run_quiet(conn, _sh('rm -f "$1"', upload.remote_tar), HOUSEKEEPING_TIMEOUT)
    yield {"type": "error", "message": "Архив образа на ноде не совпал с исходным — запустите обновление ещё раз"}


def _disk_line(disk: NodeDisk) -> str:
    if disk.freed_mb:
        return f"[panel] Уборка освободила {disk.freed_mb} МБ, на диске ноды свободно {disk.docker_free_mb} МБ"
    return f"[panel] На диске ноды свободно {disk.docker_free_mb} МБ"


async def _deliver_in_session(
    conn, tag: str, archive: NodeImageArchive, upload: _UploadState,
) -> AsyncIterator[dict]:
    """Один SSH-сеанс доставки. Каждый шаг можно повторить в следующем сеансе:
    заливка продолжается с места обрыва, загрузка и запуск повторяются без вреда."""
    yield {"type": "log", "line": "[panel] Убираю с ноды остатки прошлых обновлений…"}
    disk = parse_disk_report(
        await _run_quiet(conn, _cleanup_command(upload.remote_tar, tag, keep_archive=True), HOUSEKEEPING_TIMEOUT)
    )
    total = archive.size
    on_node = min(disk.archive_bytes, total) if disk else 0
    uploaded = on_node == total and await _archive_matches(conn, upload.remote_tar, archive.sha256)

    if disk is None:
        yield {"type": "log", "line": "[panel] Нода не сообщила свободное место — продолжаю без проверки"}
    else:
        yield {"type": "log", "line": _disk_line(disk)}
        shortage = space_shortage(disk, 0 if uploaded else total - on_node, archive.image_size)
        if shortage:
            yield {"type": "error", "message": shortage}
            return

    if uploaded:
        upload.reached = total
        yield {"type": "log", "line": "[panel] Архив образа уже лежит на ноде целиком — заливка не нужна"}
    else:
        offset = resume_offset(disk.archive_bytes, total) if disk else 0
        async for event in _upload_verified(conn, archive, upload, offset):
            yield event
            if event["type"] == "error":
                return

    yield {"type": "step", "step": "load"}
    yield {"type": "log", "line": "[panel] Загружаю образ в docker…"}
    async for event in _run_step(conn, _load_command(upload.remote_tar), LOAD_TIMEOUT, "docker load не удался"):
        yield event
        if event["type"] == "error":
            return

    # Никаких скачиваний с ноды: образ уже загружен, просто поднимаем её на нём
    yield {"type": "step", "step": "start"}
    yield {"type": "log", "line": "[panel] Поднимаю ноду на доставленном образе (без скачивания)…"}
    async for event in _run_step(conn, _start_command(tag), UPDATE_TIMEOUT, "Не удалось поднять ноду на образе"):
        yield event
        if event["type"] == "error":
            return

    # Нода уже работает на новом образе: сбой уборки не повод заливать архив заново
    try:
        after = parse_disk_report(
            await _run_quiet(conn, _cleanup_command(upload.remote_tar, tag, keep_archive=False), HOUSEKEEPING_TIMEOUT)
        )
    except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
        yield {
            "type": "log",
            "line": f"[panel] Уборка после запуска не завершилась ({_error_text(exc)}) — её повторит следующее обновление",
        }
    else:
        if after is not None:
            yield {
                "type": "log",
                "line": f"[panel] Прежний образ и архив убраны, на диске ноды свободно {after.docker_free_mb} МБ",
            }
    yield {"type": "done", "message": "Образ доставлен, нода поднята"}


async def deliver_image(target: SSHTarget, tag: str) -> AsyncIterator[dict]:
    """Доставить образ ноды и поднять её на нём. Стримит события {type: log|step|error|done}."""
    yield {"type": "step", "step": "prepare"}
    yield {"type": "log", "line": f"[panel] Готовлю образ ноды ({tag})…"}
    try:
        archive = await ensure_image(tag)
    except Exception as exc:  # noqa: BLE001 — граница: реестр/докер недоступны панели
        yield {"type": "error", "message": f"Панель не смогла получить образ с реестра: {exc}"}
        return

    connect_kwargs = ssh_connect_kwargs(target)
    upload = _UploadState(remote_tar=remote_archive_path(archive.sha256))
    stalled = 0
    for session in range(1, SESSIONS_LIMIT + 1):
        conn = None
        async for item in _connect(target, connect_kwargs):
            if not isinstance(item, dict):
                conn = item
                break
            yield item
        if conn is None:
            return

        reached_before = upload.reached
        try:
            async with conn:
                async for event in _deliver_in_session(conn, tag, archive, upload):
                    yield event
                    if event["type"] in ("error", "done"):
                        return
        except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
            stalled = 0 if upload.reached > reached_before else stalled + 1
            gave_up = stalled >= STALLED_SESSIONS_LIMIT or session == SESSIONS_LIMIT
            if gave_up or not _is_connection_drop(exc):
                yield {"type": "error", "message": f"Ошибка SSH: {_session_error_text(exc)}"}
                return
            yield {
                "type": "log",
                "line": f"[panel] Связь с нодой оборвалась ({_error_text(exc)}) — переподключаюсь, "
                        "доставка продолжится с места обрыва",
            }
            await asyncio.sleep(SSH_CONNECT_RETRY_DELAY)


@dataclass
class DeliveryJob:
    id: str
    server_id: int
    name: str
    host: str
    status: str = "queued"  # queued | running | success | error
    # Этап по порядку: prepare → upload → load → start — статус на карточке ноды
    # «заливка образа 45% (2/4)»; пока задача идёт
    step: Optional[str] = None
    percent: Optional[int] = None  # прогресс заливки образа
    log: list[str] = field(default_factory=list)
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    subscribers: set = field(default_factory=set)
    task: Optional[asyncio.Task] = None


class ImageDeliveryJobManager:
    """In-memory реестр фоновых задач доставки образа с pub/sub лога."""

    def __init__(self, concurrency: int = DELIVERY_CONCURRENCY) -> None:
        self._jobs: dict[str, DeliveryJob] = {}
        self._concurrency = concurrency
        # Все заливки идут с одного аплинка панели: без потолка массовый запуск
        # поделил бы канал на всех и не довёз бы образ ни до одной ноды за разумное время
        self._slots = asyncio.Semaphore(concurrency)

    def _cleanup_finished(self) -> None:
        now = time.time()
        for jid in [
            j for j, job in self._jobs.items()
            if job.finished_at is not None and now - job.finished_at > FINISHED_TTL_SECONDS
        ]:
            self._jobs.pop(jid, None)

    def get(self, job_id: str) -> Optional[DeliveryJob]:
        return self._jobs.get(job_id)

    def _active_job_for(self, server_id: int) -> Optional[DeliveryJob]:
        return next(
            (j for j in self._jobs.values() if j.server_id == server_id and j.finished_at is None),
            None,
        )

    def list_jobs(self) -> list[dict]:
        self._cleanup_finished()
        return [
            {
                "job_id": j.id,
                "server_id": j.server_id,
                "name": j.name,
                "host": j.host,
                "status": j.status,
                "error": j.error,
                "started_at": j.started_at,
                "finished_at": j.finished_at,
                "step": j.step,
                "percent": j.percent,
            }
            for j in sorted(self._jobs.values(), key=lambda x: x.started_at)
        ]

    def start(self, server_id: int, name: str, target: SSHTarget, tag: str) -> str:
        """Запустить доставку. Если по серверу уже идёт задача — вернуть её, вторую не плодить."""
        self._cleanup_finished()
        active = self._active_job_for(server_id)
        if active is not None:
            return active.id
        job_id = uuid.uuid4().hex
        job = DeliveryJob(id=job_id, server_id=server_id, name=name, host=target.host)
        self._jobs[job_id] = job
        job.task = asyncio.create_task(self._run(job, target, tag))
        return job_id

    def _emit(self, job: DeliveryJob, event: dict) -> None:
        if event.get("type") == "log":
            job.log.append(event.get("line", ""))
            if len(job.log) > LOG_BUFFER_LIMIT:
                del job.log[: len(job.log) - LOG_BUFFER_LIMIT]
            event = {**event, "_idx": len(job.log) - 1}
        for queue in list(job.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def _finish(self, job: DeliveryJob, status: str, error: Optional[str] = None) -> None:
        if error:
            self._emit(job, {"type": "error", "message": error})
        job.status = status
        job.error = error
        job.step = None
        job.percent = None
        job.finished_at = time.time()
        self._emit(job, {"type": "done", "status": status})

    async def _run(self, job: DeliveryJob, target: SSHTarget, tag: str) -> None:
        try:
            self._emit(job, {"type": "start", "host": job.host})
            if self._slots.locked():
                self._emit(job, {
                    "type": "log",
                    "line": f"[panel] В очереди: одновременно обновляется не больше {self._concurrency} нод",
                })
            async with self._slots:
                job.status = "running"
                async for event in deliver_image(target, tag):
                    etype = event.get("type")
                    if etype == "error":
                        self._finish(job, "error", event.get("message"))
                        return
                    if etype == "done":
                        self._emit(job, {"type": "log", "line": f"[panel] {event.get('message')}"})
                        self._finish(job, "success")
                        return
                    if etype == "step":
                        # Этап — для статуса на карточке, в лог окна он не идёт
                        job.step = event.get("step")
                        job.percent = event.get("percent")
                        continue
                    self._emit(job, event)
            self._finish(job, "error", "Доставка прервалась без результата")
        except asyncio.CancelledError:
            self._finish(job, "error", "Доставка отменена")
            raise
        except Exception as exc:  # noqa: BLE001 — верхняя граница фоновой задачи
            logger.error("Delivery job %s failed: %s", job.id, exc)
            self._finish(job, "error", str(exc))

    async def subscribe(self, job_id: str) -> AsyncIterator[dict]:
        job = self._jobs.get(job_id)
        if job is None:
            return
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        live = job.finished_at is None
        if live:
            job.subscribers.add(queue)
        backlog = list(job.log)
        last_idx = len(backlog) - 1
        try:
            yield {"type": "start", "host": job.host}
            for line in backlog:
                yield {"type": "log", "line": line}
            if not live:
                if job.error:
                    yield {"type": "error", "message": job.error}
                yield {"type": "done", "status": job.status}
                return
            while True:
                event = await queue.get()
                etype = event.get("type")
                if etype == "start":
                    continue
                if etype == "log":
                    if event.get("_idx", -1) <= last_idx:
                        continue
                    yield {"type": "log", "line": event.get("line", "")}
                    continue
                yield event
                if etype == "done":
                    return
        finally:
            job.subscribers.discard(queue)


_manager = ImageDeliveryJobManager()


def get_image_delivery_manager() -> ImageDeliveryJobManager:
    return _manager
