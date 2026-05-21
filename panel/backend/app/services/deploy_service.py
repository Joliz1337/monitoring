"""Авторазвёртывание ноды на удалённом сервере по SSH.

Панель подключается к серверу, скачивает install.sh и запускает его в режиме
`--unattended`: ставится нода мониторинга и, по желанию, WARP / нода Remnawave /
HTTP-прокси установщика. Пароль SSH живёт только в памяти на время установки.
"""
from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from typing import AsyncIterator

import asyncssh

INSTALLER_URL = "https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh"
REMOTE_SCRIPT = "/tmp/mon-install.sh"

# Установка с нуля (apt, docker, pull образов) укладывается в ~25 минут
DEPLOY_TIMEOUT = 1500
CONNECT_TIMEOUT = 30


@dataclass
class DeployParams:
    host: str
    ssh_port: int
    ssh_user: str
    node_secret: str
    panel_ip: str | None = None
    ssh_password: str | None = None
    ssh_private_key: str | None = None
    ssh_key_passphrase: str | None = None
    install_warp: bool = False
    install_remnawave: bool = False
    remnawave_cert: str | None = None
    proxy_url: str | None = None
    install_optimizations: bool = False
    opt_profile: str = "vpn"


def _build_inner_command(params: DeployParams) -> str:
    """Команда установки: скачать install.sh и запустить --unattended.

    Все значения env экранируются shlex.quote — защита от инъекций.
    """
    env: dict[str, str] = {
        "MON_INSTALL_NODE": "1",
        "NODE_SECRET": params.node_secret,
    }
    if params.panel_ip:
        env["PANEL_IP"] = params.panel_ip
    if params.proxy_url:
        env["MON_PROXY_URL"] = params.proxy_url
    if params.install_optimizations:
        env["MON_INSTALL_OPTIMIZATIONS"] = "1"
        env["MON_OPT_PROFILE"] = params.opt_profile
    if params.install_warp:
        env["MON_INSTALL_WARP"] = "1"
    if params.install_remnawave:
        env["MON_INSTALL_REMNAWAVE"] = "1"
        if params.remnawave_cert:
            # install.sh ждёт сертификат с экранированными переводами строк
            cert = params.remnawave_cert.replace("\r\n", "\n").replace("\n", "\\n")
            env["REMNAWAVE_CERT"] = cert

    assignments = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
    return (
        f"curl -fsSL {shlex.quote(INSTALLER_URL)} -o {shlex.quote(REMOTE_SCRIPT)} && "
        f"{assignments} bash {shlex.quote(REMOTE_SCRIPT)} --unattended"
    )


def _connect_kwargs(params: DeployParams) -> dict:
    """Параметры asyncssh.connect. known_hosts=None — целевые серверы заранее
    неизвестны панели, ключ хоста не проверяем (оператор вводит доверенные креды)."""
    kwargs: dict = {
        "host": params.host,
        "port": params.ssh_port,
        "username": params.ssh_user,
        "known_hosts": None,
        "connect_timeout": CONNECT_TIMEOUT,
    }
    if params.ssh_private_key:
        key = asyncssh.import_private_key(
            params.ssh_private_key, params.ssh_key_passphrase or None
        )
        kwargs["client_keys"] = [key]
    elif params.ssh_password:
        kwargs["password"] = params.ssh_password
    return kwargs


async def deploy_node(params: DeployParams) -> AsyncIterator[dict]:
    """Подключается по SSH, ставит ноду и стримит лог установки построчно.

    Yields события: {"type": "log"|"done"|"error", ...}.
    """
    if not params.ssh_password and not params.ssh_private_key:
        yield {"type": "error", "message": "Не указан пароль или приватный ключ SSH"}
        return

    try:
        connect_kwargs = _connect_kwargs(params)
    except (asyncssh.KeyImportError, ValueError) as exc:
        yield {"type": "error", "message": f"Некорректный SSH-ключ: {exc}"}
        return

    inner = _build_inner_command(params)
    needs_sudo = params.ssh_user.strip() != "root"
    if needs_sudo:
        # с паролем — sudo -S читает его из stdin; без пароля — sudo -n (нужен NOPASSWD)
        sudo_flag = "-S -p ''" if params.ssh_password else "-n"
        command = f"sudo {sudo_flag} bash -c " + shlex.quote(inner)
    else:
        command = inner

    try:
        async with asyncssh.connect(**connect_kwargs) as conn:
            yield {
                "type": "log",
                "line": f"[panel] SSH-подключение к {params.host}:{params.ssh_port} установлено",
            }
            process = await conn.create_process(command, stderr=asyncssh.STDOUT)

            if needs_sudo and params.ssh_password:
                process.stdin.write(params.ssh_password + "\n")

            loop = asyncio.get_event_loop()
            deadline = loop.time() + DEPLOY_TIMEOUT

            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    process.terminate()
                    yield {"type": "error", "message": "Превышен таймаут установки"}
                    return
                try:
                    line = await asyncio.wait_for(
                        process.stdout.readline(), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    process.terminate()
                    yield {"type": "error", "message": "Превышен таймаут установки"}
                    return
                if not line:
                    break
                yield {"type": "log", "line": line.rstrip("\n")}

            await process.wait()
            # returncode: код выхода (0..255) или отрицательный номер сигнала
            exit_code = process.returncode
            yield {"type": "done", "exit_code": exit_code if exit_code is not None else 1}
    except asyncssh.PermissionDenied:
        yield {"type": "error", "message": "SSH: неверный логин, пароль или ключ"}
    except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
        yield {"type": "error", "message": f"Ошибка SSH-подключения: {exc}"}
