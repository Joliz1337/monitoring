"""Общие заглушки тестов SSH-доступа: сессия базы, sshd в процессе, SOCKS5-прокси."""

import asyncio
import ipaddress
import unittest
from types import SimpleNamespace
from typing import Optional
from unittest import mock

import asyncssh

from app.models import Server, SshAccessProfile
from app.services import ssh_target
from app.services.ssh_target import SSHTarget, open_ssh_connection, ssh_connect_kwargs


class _Rows:
    def __init__(self, rows: list):
        self._rows = rows
        self.rowcount = len(rows)

    def scalars(self) -> "_Rows":
        return self

    def all(self) -> list:
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


# Вычисляемые колонки запросов: у строки модели такого атрибута нет
_COMPUTED_COLUMNS = {
    "has_password": lambda row: row.ssh_password is not None,
    "has_private_key": lambda row: row.ssh_private_key is not None,
}


def _column_value(row, name: str):
    computed = _COMPUTED_COLUMNS.get(name)
    return computed(row) if computed else getattr(row, name)


class FakeDb:
    """Сессия для сервисов SSH-доступа. execute отдаёт строки по первой сущности
    запроса (Server или SshAccessProfile), фильтры WHERE не применяет — в тест
    кладутся только нужные строки. Запрос колонок получает строки с теми же
    именами, что в запросе."""

    def __init__(self, servers=(), profiles=()):
        self.servers: list[Server] = list(servers)
        self.profiles: list[SshAccessProfile] = list(profiles)
        self.queries: list = []
        self.commit = mock.AsyncMock()

    async def execute(self, query) -> _Rows:
        self.queries.append(query)
        first = query.column_descriptions[0]
        rows = self.profiles if first["entity"] is SshAccessProfile else self.servers
        if first["expr"] is first["entity"]:
            return _Rows(rows)
        names = [description["name"] for description in query.column_descriptions]
        return _Rows([SimpleNamespace(**{name: _column_value(row, name) for name in names}) for row in rows])

    def add(self, row: SshAccessProfile) -> None:
        self.profiles.append(row)

    async def delete(self, row: SshAccessProfile) -> None:
        self.profiles.remove(row)


def public_text(key: asyncssh.SSHKey) -> str:
    return key.export_public_key("openssh").decode().strip()


def sshd_recording(seen: list[str]):
    """sshd, который записывает, до какого шага дошёл клиент."""

    class Sshd(asyncssh.SSHServer):
        def connection_made(self, conn):
            seen.append("connect")

        def begin_auth(self, username: str) -> bool:
            seen.append("auth")
            return True

        def password_auth_supported(self) -> bool:
            return True

        def validate_password(self, username: str, password: str) -> bool:
            seen.append("password")
            return password == "pw"

        def server_requested(self, listen_host: str, listen_port: int) -> bool:
            return True

    return Sshd


class LiveSshCase(unittest.IsolatedAsyncioTestCase):
    """Настоящий sshd в процессе: пароль root — «pw», любая команда завершается успехом."""

    async def asyncSetUp(self):
        self.seen: list[str] = []
        self.commands: list[Optional[str]] = []
        self.host_key = asyncssh.generate_private_key("ssh-ed25519")
        self.port = await self.start_sshd(self.host_key)
        self.remember = mock.AsyncMock()
        patcher = mock.patch.object(ssh_target, "_remember_host_key", self.remember)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _run_command(self, process: asyncssh.SSHServerProcess) -> None:
        self.commands.append(process.command)
        process.stdout.write("installed\n")
        process.exit(0)

    async def start_sshd(self, host_key: asyncssh.SSHKey) -> int:
        acceptor = await asyncssh.create_server(
            sshd_recording(self.seen), "127.0.0.1", 0,
            server_host_keys=[host_key], process_factory=self._run_command,
        )
        self.addAsyncCleanup(acceptor.wait_closed)
        self.addCleanup(acceptor.close)
        return acceptor.sockets[0].getsockname()[1]

    def target(self, **fields) -> SSHTarget:
        return SSHTarget(**{"host": "127.0.0.1", "port": self.port, "password": "pw", **fields})

    async def open(self, target: SSHTarget, connect_kwargs: dict | None = None) -> None:
        kwargs = ssh_connect_kwargs(target) if connect_kwargs is None else connect_kwargs
        conn = await open_ssh_connection(kwargs, None, target.server_id)
        conn.close()
        await conn.wait_closed()


class Socks5Proxy:
    """SOCKS5 без аутентификации: запоминает, куда через него подключались.

    С redirect_to соединяет с этим портом на 127.0.0.1, куда бы ни просили: так
    тест получает сервер, до которого напрямую не достать, а через прокси можно.
    """

    def __init__(self, redirect_to: Optional[int] = None):
        self.destinations: list[tuple[str, int]] = []
        self._redirect_to = redirect_to
        self._tasks: set[asyncio.Task] = set()
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._server.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            _version, methods = await reader.readexactly(2)
            await reader.readexactly(methods)
            writer.write(b"\x05\x00")
            _version, _command, _reserved, address_type = await reader.readexactly(4)
            if address_type == 1:
                host = str(ipaddress.ip_address(await reader.readexactly(4)))
            else:
                host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
            port = int.from_bytes(await reader.readexactly(2), "big")
            self.destinations.append((host, port))
            upstream = ("127.0.0.1", self._redirect_to) if self._redirect_to else (host, port)
            upstream_reader, upstream_writer = await asyncio.open_connection(*upstream)
            writer.write(b"\x05\x00\x00\x01" + bytes(6))
            await asyncio.gather(
                self._pipe(reader, upstream_writer), self._pipe(upstream_reader, writer),
            )
        except (asyncio.IncompleteReadError, OSError):
            pass
        finally:
            writer.close()
            self._tasks.discard(task)

    @staticmethod
    async def _pipe(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
