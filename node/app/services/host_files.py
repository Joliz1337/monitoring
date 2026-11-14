"""Чтение и запись файлов хоста через nsenter."""

import base64
import logging
from typing import Optional

from app.services.host_executor import get_host_executor

logger = logging.getLogger(__name__)


async def read_host_file(path: str) -> Optional[str]:
    """Read file from host filesystem via nsenter"""
    executor = get_host_executor()
    result = await executor.execute(f"cat {path}", timeout=5)
    if result.success and result.exit_code == 0:
        return result.stdout
    return None


async def read_host_file_exact(path: str) -> Optional[str]:
    """Побайтовое чтение файла хоста.

    HostExecutor стрипает stdout, поэтому обычный `cat` теряет завершающий
    перевод строки. Для конфигов это ломает и откат байт-в-байт, и сверку
    хэшей (хэш прочитанного не совпал бы с хэшем записанного), поэтому
    содержимое переносится через base64 — он не содержит пробельных краёв.
    """
    executor = get_host_executor()
    result = await executor.execute(f"base64 -w0 {path}", timeout=10)
    if not (result.success and result.exit_code == 0):
        return None
    if not result.stdout:
        return ""
    try:
        return base64.b64decode(result.stdout).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _umask_for(mode: str) -> str:
    """Umask, создающий файл сразу с правами `mode` — до всякого chmod."""
    return "".join(str(7 - int(digit)) for digit in mode[-3:])


def _permissions_are_safe(actual: str, wanted: str) -> bool:
    """Совпадения бит-в-бит требовать нельзя: pmxcfs (`/etc/pve` на Proxmox)
    держит собственные фиксированные права `0640 root:www-data` и отвечает на
    chmod отказом. Значимы две границы: владелец получил не меньше запрошенного
    (иначе скрипт остался бы без бита исполнения, а конфиг — нечитаемым), а
    «прочие» — не больше (иначе приватный ключ утёк бы всем). Группа между этими
    границами свободна: в таких ФС она принадлежит служебному демону, который
    эти файлы и читает.
    """
    if not actual.isdigit() or len(actual) < 3:
        return False
    actual_owner, actual_other = int(actual[-3]), int(actual[-1])
    wanted_owner, wanted_other = int(wanted[-3]), int(wanted[-1])
    return (
        actual_owner & wanted_owner == wanted_owner
        and actual_other & ~wanted_other == 0
    )


def _write_command(path: str, content: str, mode: Optional[str], in_place: bool = False) -> str:
    """Команда записи файла на хост.

    Uses base64 rather than a heredoc: the old `cat > path << 'EOFCONFIG'` form
    terminated early if any content line happened to equal EOFCONFIG, silently
    truncating the file. Now that the *inputs* (shell scripts, templates) are
    what get pushed, that is no longer a theoretical concern.

    Файл собирается во временном рядом с целью и встаёт на место одним rename:
    сбой посреди записи (кончилось место, убит nsenter) оставляет прежний файл
    целым, а читатель — в том числе bash, исполняющий этот скрипт прямо сейчас —
    не видит половину нового. Запись на месте шла сквозь симлинк и не трогала
    владельца и права существующего файла, поэтому симлинк разыменовывается,
    владелец переносится всегда, права — когда `mode` не задан.

    in_place — писать прямо в файл (`> path`), сохраняя inode: rename оставил
    бы контейнер с single-file bind-mount на старом файле, а на pmxcfs
    (`/etc/pve`) поведение rename не проверено.
    """
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    decode = f"printf '%s' '{encoded}' | base64 -d"
    prepare = f"mkdir -p $(dirname {path})"
    # Права даёт umask при создании файла, а chmod лишь доводит их на уже
    # существующем: отдельный chmod после записи оставлял бы приватный ключ
    # читаемым всем в промежутке между двумя шагами. Провал самого chmod не
    # фатален — есть ФС, где права фиксированы и chmod запрещён (pmxcfs), а
    # запись при этом проходит; итог оценивается по фактическим правам.
    if mode:
        prepare = f"umask {_umask_for(mode)} && {prepare}"

    if in_place:
        write = f"{prepare} && {decode} > {path}"
        if not mode:
            return write
        return f"{write} || exit 1; chmod {mode} {path} 2>/dev/null; stat -c '%a' {path}"

    inherit = 'chown --reference="$target" "$tmp"'
    if not mode:
        inherit += '; chmod --reference="$target" "$tmp"'
    steps = [
        f'{prepare} && target=$(readlink -f {path}) || exit 1',
        # Замаскированный юнит — симлинк на /dev/null: rename подменил бы само устройство
        '[ ! -e "$target" ] || [ -f "$target" ] || { echo "not a regular file: $target" >&2; exit 1; }',
        'tmp="$target.tmp.$$"',
        f'{decode} > "$tmp" || {{ rm -f "$tmp"; exit 1; }}',
        f'[ -e "$target" ] && {{ {inherit}; }} 2>/dev/null',
    ]
    if mode:
        steps.append(f'chmod {mode} "$tmp" 2>/dev/null')
    # -T: каталог на месте цели — ошибка, а не повод положить файл внутрь него
    steps.append('mv -fT "$tmp" "$target" || { rm -f "$tmp"; exit 1; }')
    if mode:
        steps.append("stat -c '%a' \"$target\"")
    return "\n".join(steps)


def _write_succeeded(result, path: str, mode: Optional[str]) -> bool:
    if not (result.success and result.exit_code == 0):
        # Вызывающие получают только bool, поэтому причина (нет места, read-only fs,
        # отказ nsenter) видна лишь отсюда — без этой строки она терялась совсем.
        logger.error(f"Host file write failed: {path}: {result.stderr.strip() or 'exit code ' + str(result.exit_code)}")
        return False

    if mode and not _permissions_are_safe(result.stdout.strip(), mode):
        logger.error(
            f"Host file write left permissions {result.stdout.strip() or '?'} "
            f"instead of {mode}: {path}"
        )
        return False

    return True


async def write_host_file(
    path: str, content: str, mode: Optional[str] = None, secret: bool = False, in_place: bool = False,
) -> bool:
    """Write a file to the host filesystem via nsenter: атомарно, через rename.

    secret — содержимое не попадает в лог: исполнитель логирует начало команды,
    а в нём начало base64 файла (.env ноды, учётки реестров, пароль прокси).
    in_place — см. _write_command: для файлов, чей inode должен пережить запись."""
    result = await get_host_executor().execute(
        _write_command(path, content, mode, in_place), timeout=20, shell="bash",
        log_label=f"write {path}" if secret else None,
    )
    return _write_succeeded(result, path, mode)


def write_host_file_sync(path: str, content: str, mode: Optional[str] = None) -> bool:
    """Синхронный вариант для менеджеров, целиком живущих в тред-пуле.

    Оборачивать асинхронную версию в asyncio.run() нельзя: вызывающий может
    оказаться и в потоке, и в самом event loop, а во втором случае это падение.
    """
    result = get_host_executor().execute_sync(
        _write_command(path, content, mode), timeout=20, shell="bash"
    )
    return _write_succeeded(result, path, mode)
