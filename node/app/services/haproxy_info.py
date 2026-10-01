"""Версия HAProxy, релиз ОС и архитектура хоста.

По ним панель находит в репозитории сборщика пакетов новейшую сборку HAProxy
под эту систему и показывает кнопку обновления, если она новее установленной.
"""
import asyncio
from typing import Optional

from app.services.haproxy_manager import get_haproxy_manager
from app.services.host_executor import get_host_executor
from app.services.host_files import read_host_file

OS_RELEASE_PATH = "/etc/os-release"


def parse_os_release(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            fields[key.strip()] = value.strip().strip("\"'")
    return fields


async def _read_package_arch() -> Optional[str]:
    result = await get_host_executor().execute("dpkg --print-architecture", timeout=5)
    if not result.success:
        return None
    return result.stdout.strip() or None


async def read_haproxy_info() -> dict:
    version, os_release, arch = await asyncio.gather(
        asyncio.to_thread(get_haproxy_manager().installed_version),
        read_host_file(OS_RELEASE_PATH),
        _read_package_arch(),
    )
    os_fields = parse_os_release(os_release or "")
    return {
        "version": version,
        "os_id": os_fields.get("ID"),
        "os_codename": os_fields.get("VERSION_CODENAME"),
        "arch": arch,
    }
