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


async def write_host_file(path: str, content: str, mode: Optional[str] = None) -> bool:
    """Write a file to the host filesystem via nsenter.

    Uses base64 rather than a heredoc: the old `cat > path << 'EOFCONFIG'` form
    terminated early if any content line happened to equal EOFCONFIG, silently
    truncating the file. Now that the *inputs* (shell scripts, templates) are
    what get pushed, that is no longer a theoretical concern.

    Пишет строго in-place (`> path`): inode сохраняется, что критично для
    файлов, смонтированных в контейнеры как single-file bind-mount.
    """
    executor = get_host_executor()
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

    cmd = (
        f"mkdir -p $(dirname {path}) && "
        f"printf '%s' '{encoded}' | base64 -d > {path}"
    )
    if mode:
        # Права даёт umask при создании файла, а chmod лишь доводит их на уже
        # существующем: отдельный chmod после записи оставлял бы приватный ключ
        # читаемым всем в промежутке между двумя шагами. Провал самого chmod не
        # фатален — есть ФС, где права фиксированы и chmod запрещён (pmxcfs), а
        # запись при этом проходит; итог оценивается по фактическим правам.
        cmd = (
            f"umask {_umask_for(mode)} && {cmd} || exit 1; "
            f"chmod {mode} {path} 2>/dev/null; "
            f"stat -c '%a' {path}"
        )

    result = await executor.execute(cmd, timeout=20, shell="bash")
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
