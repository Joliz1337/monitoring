"""Версия HAProxy и релиз ОС хоста.

По релизу панель решает, есть ли под эту систему официальная сборка новее
установленной, и показывает кнопку обновления HAProxy.
"""
import asyncio

from app.services.haproxy_manager import get_haproxy_manager
from app.services.host_files import read_host_file

OS_RELEASE_PATH = "/etc/os-release"


def parse_os_release(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            fields[key.strip()] = value.strip().strip("\"'")
    return fields


async def read_haproxy_info() -> dict:
    version, os_release = await asyncio.gather(
        asyncio.to_thread(get_haproxy_manager().installed_version),
        read_host_file(OS_RELEASE_PATH),
    )
    os_fields = parse_os_release(os_release or "")
    return {
        "version": version,
        "os_id": os_fields.get("ID"),
        "os_codename": os_fields.get("VERSION_CODENAME"),
    }
