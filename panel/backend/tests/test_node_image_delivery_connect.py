"""SSH-подключение доставки образа: повторы и текст ошибки.

Сервер за ТСПУ отвечает по SSH через раз — до ошибки панель пробует подключиться
ещё дважды. Таймаут подключения asyncssh приходит исключением без текста, и в
уведомлении оставалось пустое «Ошибка SSH: » — теперь там адрес и таймаут.
Отказ ноды принять файл приходил одним словом «Failure» — теперь ошибка говорит,
что это значит.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import asyncssh

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import node_image_delivery as module  # noqa: E402
from app.services.node_image_cache import NodeImageArchive  # noqa: E402
from app.services.ssh_target import CONNECT_TIMEOUT, SSHTarget  # noqa: E402

TARGET = SSHTarget(host="203.0.113.7", port=2222, password="secret")


class DeadSession:
    """Подключение есть, а сеанс рвётся на первой же команде — дальше подключения тест не идёт."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def run(self, *_args, **_kwargs):
        raise OSError()


class ScriptedConnect:
    """Подмена asyncssh.connect: каждый вызов берёт следующий исход из списка."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def __call__(self, **_kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class DeliverImageConnectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tar = Path(tmp.name) / "node.tar.gz"
        tar.write_bytes(b"image")
        archive = NodeImageArchive(tar, sha256="0" * 64, image_size=0)

        for patcher in (
            mock.patch.object(module, "ensure_image", mock.AsyncMock(return_value=archive)),
            mock.patch.object(module, "SSH_CONNECT_RETRY_DELAY", 0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def deliver(self, connect: ScriptedConnect) -> list[dict]:
        with mock.patch.object(module.asyncssh, "connect", connect):
            return [event async for event in module.deliver_image(TARGET, "latest")]

    async def test_timeouts_end_with_address_after_all_attempts(self):
        connect = ScriptedConnect(*[asyncio.TimeoutError()] * module.SSH_CONNECT_ATTEMPTS)
        events = await self.deliver(connect)

        self.assertEqual(connect.calls, module.SSH_CONNECT_ATTEMPTS)
        retries = [e for e in events if e["type"] == "log" and "SSH не подключился" in e["line"]]
        self.assertEqual(len(retries), module.SSH_CONNECT_ATTEMPTS - 1)
        self.assertEqual(events[-1], {
            "type": "error",
            "message": f"Ошибка SSH: 203.0.113.7:2222 не ответил за {CONNECT_TIMEOUT} с "
                       f"(попыток: {module.SSH_CONNECT_ATTEMPTS})",
        })

    async def test_retry_reaches_session_after_failed_attempt(self):
        sessions = [DeadSession() for _ in range(module.STALLED_SESSIONS_LIMIT)]
        connect = ScriptedConnect(ConnectionRefusedError(111, "Connect call failed"), *sessions)
        events = await self.deliver(connect)

        # Оборванный сеанс — это переподключение, пока обрывы не пойдут подряд без продвижения
        self.assertEqual(connect.calls, 1 + module.STALLED_SESSIONS_LIMIT)
        self.assertIn({"type": "log", "line": "[panel] SSH к 203.0.113.7:2222 установлен"}, events)
        # Ошибка без текста называет хотя бы свой тип, а не пустую строку
        self.assertEqual(events[-1], {"type": "error", "message": "Ошибка SSH: OSError"})

    async def test_wrong_credentials_are_not_retried(self):
        connect = ScriptedConnect(asyncssh.PermissionDenied("denied"))
        events = await self.deliver(connect)

        self.assertEqual(connect.calls, 1)
        self.assertEqual(events[-1], {"type": "error", "message": "SSH: неверный логин, пароль или ключ"})


class SessionErrorTextTests(unittest.TestCase):
    def test_sftp_failure_is_explained_and_not_retried(self):
        # Одним словом «Failure» sftp-server отвечает на любой отказ записи — так выглядел полный диск
        refusal = asyncssh.SFTPFailure("Failure")
        self.assertFalse(module._is_connection_drop(refusal))
        self.assertIn("на её диске кончилось место", module._session_error_text(refusal))
        self.assertIn("Failure", module._session_error_text(refusal))

    def test_lost_connection_is_worth_reconnecting(self):
        for drop in (
            asyncssh.ConnectionLost("Connection lost"),
            asyncssh.SFTPConnectionLost("Connection closed"),
            ConnectionResetError(104, "Connection reset by peer"),
            asyncio.TimeoutError(),
        ):
            self.assertTrue(module._is_connection_drop(drop), drop)

    def test_refused_credentials_are_not_a_dropped_link(self):
        self.assertFalse(module._is_connection_drop(asyncssh.PermissionDenied("denied")))


if __name__ == "__main__":
    unittest.main()
