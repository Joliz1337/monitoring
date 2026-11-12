"""Конвейер автобэкапа в Telegram: сколько копий архива он держит в памяти.

Без БД, сети и Docker: дамп — случайные байты, Telegram — транспорт-заглушка
httpx. Раньше в памяти лежало пять копий архива разом (дамп, дамп с .env,
шифртекст, шифртекст с заголовком, тома) и три — всё время заливки. Формат
набора при этом прежний: старые наборы должны восстанавливаться, новые —
собираться прежним кодом.
"""
import asyncio
import os
import re
import sys
import tracemalloc
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402

from app.models import BackupSettings  # noqa: E402
from app.routers import backup as bk  # noqa: E402
from app.services import backup_scheduler as sched  # noqa: E402
from app.services import backup_telegram as bt  # noqa: E402

MB = 1024 * 1024
PASSWORD = "archive-pw"
ENV_TEXT = "PANEL_ENC_KEY=a2V5\nJWT_SECRET=jjj\n"


def telegram_settings() -> BackupSettings:
    s = BackupSettings()
    s.bot_token = "123456:token"
    s.chat_id = "-100500"
    s.archive_password = PASSWORD
    s.volume_size_mb = 1
    return s


def uploaded_document(request: httpx.Request) -> tuple[str, bytes]:
    """Имя и содержимое файла из multipart-тела запроса sendDocument."""
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    body = request.content
    header_at = body.index(b'name="document"')
    content_at = body.index(b"\r\n\r\n", header_at) + 4
    content_end = body.rindex(b"\r\n--" + boundary + b"--")
    filename = re.search(rb'filename="([^"]+)"', body[header_at:content_at]).group(1).decode()
    return filename, body[content_at:content_end]


class PipelineTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # На память и раскладку томов вывод ключа не влияет, а шаг цикла событий
        # затягивает дольше, чем asyncio под тестом считает нормой
        self.patch(bt, "_PBKDF2_ITERS", 1)

    def patch(self, target, name: str, replacement):
        patcher = mock.patch.object(target, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)
        return replacement


class ArchiveCopiesInMemoryTests(PipelineTestCase):
    DUMP_BYTES = 16 * MB

    def setUp(self):
        super().setUp()
        # Дамп создаётся уже под замером — иначе не видно, отпустил ли его конвейер
        self.patch(bk, "_run_pg_dump", lambda: (os.urandom(self.DUMP_BYTES), None))
        self.patch(bk, "read_panel_env", lambda: ENV_TEXT)
        self.patch(bt, "send_volumes", self.upload)
        self.held_while_uploading = 0
        if not tracemalloc.is_tracing():
            tracemalloc.start()
            self.addCleanup(tracemalloc.stop)

    def traced_copies(self, sample: int) -> float:
        """Сколько размеров дампа занято сверх того, что было до запуска."""
        return (sample - self.baseline) / self.DUMP_BYTES

    async def upload(self, _token, _chat_id, volumes, _filename_base, _caption) -> None:
        for _volume in volumes:
            # Как настоящая заливка: сетевой await отдаёт управление циклу событий
            await asyncio.sleep(0)
            self.held_while_uploading = max(self.held_while_uploading, tracemalloc.get_traced_memory()[0])

    async def deliver_backup(self) -> None:
        self.baseline = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()
        outcome = await sched.run_backup_to_telegram(telegram_settings())
        self.assertEqual(outcome, (True, None))

    async def test_no_more_than_two_copies_of_the_archive_at_once(self):
        await self.deliver_backup()

        peak = tracemalloc.get_traced_memory()[1]
        self.assertLess(self.traced_copies(peak), 2.5)

    async def test_one_copy_of_the_archive_while_uploading(self):
        await self.deliver_backup()

        self.assertLess(self.traced_copies(self.held_while_uploading), 1.5)


class DeliveredSetTests(PipelineTestCase):
    async def test_volumes_sent_to_telegram_restore_to_the_dump_and_env(self):
        dump = os.urandom(3 * MB + 12345)
        requests: list[httpx.Request] = []

        def telegram(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"ok": True})

        real_client = httpx.AsyncClient
        self.patch(bt.httpx, "AsyncClient", lambda **options: real_client(transport=httpx.MockTransport(telegram), **options))
        self.patch(bk, "_run_pg_dump", lambda: (dump, None))
        self.patch(bk, "read_panel_env", lambda: ENV_TEXT)

        outcome = await sched.run_backup_to_telegram(telegram_settings())

        self.assertEqual(outcome, (True, None))
        documents = [uploaded_document(request) for request in requests]
        filenames = [name for name, _ in documents]
        volumes = [content for _, content in documents]
        self.assertEqual([name[-8:] for name in filenames], [".enc.001", ".enc.002", ".enc.003", ".enc.004"])
        self.assertEqual([len(volume) for volume in volumes[:-1]], [MB] * 3)
        # Дальше — ровно то, что делает окно восстановления панели с загруженным набором
        self.assertTrue(bt.is_encrypted_set(volumes[0]))
        self.assertEqual(bt.missing_volume_numbers(filenames), [])
        restored_dump, restored_env = bk._unframe_backup(bt.join_and_decrypt(volumes, PASSWORD))
        self.assertTrue(restored_dump == dump, "дамп из набора томов не совпал с исходным")
        self.assertEqual(restored_env, bk.parse_env(ENV_TEXT))


if __name__ == "__main__":
    unittest.main()
