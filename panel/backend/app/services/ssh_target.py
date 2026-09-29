"""SSH-доступ панели к уже добавленному серверу (креды хранятся на записи Server).

Им пользуются доставка образа ноды и установка Remnawave, когда сервер за ТСПУ
качает всё через панель.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import asyncssh

CONNECT_TIMEOUT = 30


@dataclass
class SSHTarget:
    host: str
    port: int = 22
    user: str = "root"
    password: Optional[str] = None
    private_key: Optional[str] = None
    passphrase: Optional[str] = None


def ssh_connect_kwargs(t: SSHTarget) -> dict:
    kwargs: dict = {
        "host": t.host,
        "port": t.port,
        "username": t.user,
        "known_hosts": None,
        "connect_timeout": CONNECT_TIMEOUT,
        # Долгая заливка образа или установка по медленному/throttled-каналу:
        # keepalive держит SSH-сессию живой в паузах между шагами.
        "keepalive_interval": 15,
        "keepalive_count_max": 8,
    }
    if t.private_key:
        kwargs["client_keys"] = [asyncssh.import_private_key(t.private_key, t.passphrase or None)]
    elif t.password:
        kwargs["password"] = t.password
    return kwargs
