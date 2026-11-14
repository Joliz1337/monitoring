"""Конвейер автобэкапа в Telegram: бэкап лежит на диске, а не в памяти бэкенда.

Без БД, сети и Docker: pg_dump — подмена subprocess.run, которая пишет случайные
байты в полученный файл, Telegram — транспорт-заглушка httpx. Раньше в памяти
лежало до пяти копий архива разом (дамп, дамп с .env, шифртекст, шифртекст с
заголовком, тома) — на VPS с дампом в сотни мегабайт это OOM. Теперь в памяти
только том, который уходит сейчас. Формат набора при этом прежний: старые
наборы должны восстанавливаться, новые — собираться прежним кодом.
"""
import gc
import io
import os
import re
import subprocess
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


def uploaded_caption(request: httpx.Request) -> str:
    match = re.search(rb'name="caption"\r\n\r\n(.*?)\r\n--', request.content, re.DOTALL)
    return match.group(1).decode() if match else ""


class PipelineTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # На память и раскладку томов вывод ключа не влияет, а шаг цикла событий
        # затягивает дольше, чем asyncio под тестом считает нормой
        self.patch(bt, "_PBKDF2_ITERS", 1)
        self.patch(bk, "_pg_env", lambda: ("panel", "paneldb"))
        self.patch(bk, "read_panel_env", lambda: ENV_TEXT)
        self.requests: list[httpx.Request] = []
        self.transport: httpx.AsyncBaseTransport = httpx.MockTransport(self.telegram)
        real_client = httpx.AsyncClient
        self.patch(bt.httpx, "AsyncClient", lambda **options: real_client(transport=self.transport, **options))

    def patch(self, target, name: str, replacement):
        patcher = mock.patch.object(target, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)
        return replacement

    def telegram(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"ok": True})

    def pg_dump_writes(self, pieces) -> None:
        """docker exec pg_dump: пишет дамп кусками в полученный дескриптор, как дочерний процесс."""
        def run(command, **options) -> subprocess.CompletedProcess:
            for piece in pieces():
                os.write(options["stdout"].fileno(), piece)
            return subprocess.CompletedProcess(command, 0, stdout=None, stderr=b"")

        self.patch(bk.subprocess, "run", run)


class StreamingTelegram(httpx.AsyncBaseTransport):
    """Как настоящий транспорт: тело запроса читается потоком и в запросе не оседает."""

    def __init__(self) -> None:
        self.sent_bytes = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async for chunk in request.stream:
            self.sent_bytes += len(chunk)
        return httpx.Response(200, json={"ok": True})


class ArchiveInMemoryTests(PipelineTestCase):
    DUMP_BYTES = 32 * MB

    def setUp(self):
        super().setUp()
        self.dump_bytes = MB
        self.pg_dump_writes(lambda: (os.urandom(MB) for _ in range(self.dump_bytes // MB)))
        self.transport = StreamingTelegram()
        # Ответ httpx держит свой запрос циклической ссылкой: отправленный том
        # лежал бы в памяти до сборщика мусора. Без сборщика это видно всегда,
        # а не как повезёт с его расписанием.
        gc.disable()
        self.addCleanup(gc.enable)
        if not tracemalloc.is_tracing():
            tracemalloc.start()
            self.addCleanup(tracemalloc.stop)

    async def test_only_the_volume_being_sent_is_in_memory(self):
        # Прогон на мегабайте — разогрев: импорты и кэши первого запуска в замер не попадают
        await sched.run_backup_to_telegram(telegram_settings())
        self.dump_bytes = self.DUMP_BYTES
        self.transport.sent_bytes = 0
        baseline = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()

        outcome = await sched.run_backup_to_telegram(telegram_settings())

        self.assertEqual(outcome, (True, None))
        self.assertGreater(self.transport.sent_bytes, self.DUMP_BYTES)
        peak = tracemalloc.get_traced_memory()[1] - baseline
        # Том в 1 МБ, его куски перед склейкой и шаг чтения с диска — но не дамп
        # целиком и не все уже отправленные тома
        self.assertLess(peak, self.DUMP_BYTES / 4, f"пик {peak / MB:.1f} МБ при дампе {self.DUMP_BYTES // MB} МБ")


class DeliveredSetTests(PipelineTestCase):
    async def test_volumes_sent_to_telegram_restore_to_the_dump_and_env(self):
        dump = os.urandom(3 * MB + 12345)
        self.pg_dump_writes(lambda: [dump])

        outcome = await sched.run_backup_to_telegram(telegram_settings())

        self.assertEqual(outcome, (True, None))
        documents = [uploaded_document(request) for request in self.requests]
        filenames = [name for name, _ in documents]
        volumes = [io.BytesIO(content) for _, content in documents]
        self.assertEqual([name[-8:] for name in filenames], [".enc.001", ".enc.002", ".enc.003", ".enc.004"])
        self.assertEqual([len(content) for _, content in documents[:-1]], [MB] * 3)
        # Дальше — ровно то, что делает окно восстановления панели с загруженным набором
        self.assertEqual(bt.missing_volume_numbers(filenames), [])
        with bk._stage_backup(volumes, filenames, bk.RestorePassword(PASSWORD)) as backup:
            dump_offset, restored_env = bk._read_frame(backup)
            backup.seek(dump_offset)
            self.assertTrue(backup.read() == dump, "дамп из набора томов не совпал с исходным")
        self.assertEqual(restored_env, bk.parse_env(ENV_TEXT))

    async def test_first_volume_is_captioned_with_the_size_and_the_number_of_volumes(self):
        self.pg_dump_writes(lambda: [os.urandom(3 * MB + 12345)])

        await sched.run_backup_to_telegram(telegram_settings())

        captions = [uploaded_caption(request) for request in self.requests]
        self.assertIn("3 МБ · томов: 4", captions[0])
        self.assertEqual(captions[1:], ["", "", ""])

    async def test_volume_telegram_keeps_refusing_fails_the_backup(self):
        self.pg_dump_writes(lambda: [os.urandom(2 * MB)])
        self.patch(bt, "SEND_RETRY_DELAY", 0)

        def refuse_second_volume(request: httpx.Request) -> httpx.Response:
            if not request.url.path.endswith("/sendDocument"):
                return httpx.Response(200, json={"ok": True})
            self.requests.append(request)
            refused = uploaded_document(request)[0].endswith(".enc.002")
            return httpx.Response(500 if refused else 200, json={"ok": not refused})

        self.transport = httpx.MockTransport(refuse_second_volume)

        ok, error = await sched.run_backup_to_telegram(telegram_settings())

        self.assertFalse(ok)
        self.assertIn("том 2/3", error)
        # Каждая попытка шлёт том целиком, а не то, что осталось от прошлой
        attempts = [uploaded_document(request) for request in self.requests[1:]]
        self.assertEqual(len(attempts), bt.SEND_RETRIES)
        self.assertEqual({len(content) for _, content in attempts}, {MB})
        self.assertEqual(len({content for _, content in attempts}), 1)


if __name__ == "__main__":
    unittest.main()
