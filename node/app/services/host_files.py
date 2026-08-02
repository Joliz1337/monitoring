"""Чтение и запись файлов хоста через nsenter."""

import base64
from typing import Optional

from app.services.host_executor import get_host_executor


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
        cmd += f" && chmod {mode} {path}"

    result = await executor.execute(cmd, timeout=20, shell="bash")
    return result.success and result.exit_code == 0
