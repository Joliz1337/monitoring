"""Тесты запроса метрик у ноды (app/services/metrics_request.py и разбор его
исходов в app/services/metrics_collector.py).

Голый unittest, без PostgreSQL. Вместо ноды — живой HTTPS-сервер на 127.0.0.1
с сертификатами из той же PKI, что выпускает панель.

Опрос метрик идёт общей сессией aiohttp: клиент httpx на ноду стоил втрое больше
процессора на запрос. Ноды за SOCKS5-прокси опрашиваются прежним клиентом httpx.
Что бы ни случилось с запросом, коллектор обязан записать ноде ту же ошибку и
тот же код, что раньше: по ним работают алерты и карточка сервера.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import functools
import gzip
import json
import os
import socket
import ssl
import sys
import tempfile
import time
import tracemalloc
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import aiohttp
import httpx
from aiohttp import web

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import http_client, metrics_collector, metrics_request  # noqa: E402
from app.services.metrics_collector import MetricsCollector  # noqa: E402
from app.services.metrics_request import MAX_BODY_BYTES, CappedBody, MetricsBodyRejected  # noqa: E402
from app.services.pki import PKIKeygenData, generate_ca, generate_client_cert, generate_node_cert  # noqa: E402

ANSWER = {"cpu": {"usage_percent": 12.5, "load_avg_1": 0.5}}
MEGABYTE = 1024 * 1024
BOMB_BYTES = 128 * MEGABYTE


def node(url: str, *, mtls: bool = True, proxy: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=1, name="node-1", url=url, pki_enabled=mtls, proxy_url=proxy, api_key="legacy-key",
    )


def make_keygen() -> PKIKeygenData:
    ca_cert, ca_key = generate_ca()
    client_cert, client_key = generate_client_cert(ca_cert, ca_key)
    return PKIKeygenData(
        ca_cert=ca_cert, ca_key=ca_key,
        client_cert=client_cert, client_key=client_key,
        shared_node_cert="", shared_node_key="",
    )


@functools.lru_cache
def gzip_of_zeros(size: int) -> bytes:
    """gzip-бомба: нули сжимаются в тысячу раз."""
    packer = zlib.compressobj(wbits=zlib.MAX_WBITS | 16)
    block = bytes(MEGABYTE)
    return b"".join(packer.compress(block) for _ in range(size // MEGABYTE)) + packer.flush()


def gzipped(payload: dict) -> web.Response:
    return web.Response(
        body=gzip.compress(json.dumps(payload).encode()),
        headers={"Content-Encoding": "gzip"}, content_type="application/json",
    )


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class CappedBodyTest(unittest.TestCase):
    def test_gzip_answer_is_unpacked(self):
        body = CappedBody("gzip")
        packed = gzip.compress(json.dumps(ANSWER).encode())
        for offset in range(0, len(packed), 7):
            body.feed(packed[offset:offset + 7])
        self.assertEqual(body.json(), ANSWER)

    def test_plain_answer_is_taken_as_is(self):
        for encoding in ("", "identity"):
            body = CappedBody(encoding)
            body.feed(json.dumps(ANSWER).encode())
            self.assertEqual(body.json(), ANSWER)

    def test_body_of_exactly_the_cap_is_accepted(self):
        CappedBody("").feed(bytes(MAX_BODY_BYTES))
        CappedBody("gzip").feed(gzip_of_zeros(MAX_BODY_BYTES))

    def test_plain_body_over_the_cap_is_rejected(self):
        body = CappedBody("")
        body.feed(bytes(MAX_BODY_BYTES))
        with self.assertRaises(MetricsBodyRejected):
            body.feed(b"x")

    def test_gzip_body_one_byte_over_the_cap_is_rejected(self):
        with self.assertRaises(MetricsBodyRejected):
            CappedBody("gzip").feed(gzip.compress(bytes(MAX_BODY_BYTES + 1)))

    def test_gzip_bomb_is_stopped_before_it_takes_the_memory(self):
        bomb = gzip_of_zeros(BOMB_BYTES)
        self.assertLess(len(bomb), MEGABYTE)

        tracemalloc.start()
        try:
            with self.assertRaises(MetricsBodyRejected):
                CappedBody("gzip").feed(bomb)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertLess(peak, BOMB_BYTES // 4)

    def test_compression_the_panel_cannot_unpack_is_rejected(self):
        for encoding in ("br", "zstd", "deflate"):
            with self.assertRaises(MetricsBodyRejected):
                CappedBody(encoding)


class LiveNodeCase(unittest.IsolatedAsyncioTestCase):
    """Нода — живой сервер; что она ответит, решает тест через `self.reply`."""

    keygen: PKIKeygenData
    certs: Path
    # None — таймауты как в бою
    TIMEOUT: aiohttp.ClientTimeout | None = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.keygen = make_keygen()
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.certs = Path(tmp.name)
        foreign_ca, foreign_ca_key = generate_ca()
        for name, (ca_cert, ca_key) in {
            "own": (cls.keygen.ca_cert, cls.keygen.ca_key),
            "foreign": (foreign_ca, foreign_ca_key),
        }.items():
            cert, key = generate_node_cert(ca_cert, ca_key, "node-1")
            (cls.certs / f"{name}.crt").write_text(cert)
            (cls.certs / f"{name}.key").write_text(key)

    async def asyncSetUp(self) -> None:
        if self.TIMEOUT is not None:
            patcher = mock.patch.object(http_client, "_METRICS_TIMEOUT", self.TIMEOUT)
            patcher.start()
            self.addCleanup(patcher.stop)
        await http_client.init_http_clients(self.keygen)
        self.addAsyncCleanup(http_client.close_http_clients)
        self.requests: list[web.Request] = []
        self.reply = self.healthy
        self.collector = MetricsCollector()

    async def start_node(self, *, issued_by: str = "own", client_cert_required: bool = True) -> str:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.certs / f"{issued_by}.crt", self.certs / f"{issued_by}.key")
        if client_cert_required:
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_verify_locations(cadata=self.keygen.ca_cert)

        app = web.Application()
        app.router.add_get("/api/metrics", self.handle)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
        await site.start()
        return f"https://127.0.0.1:{runner.addresses[0][1]}"

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(request)
        return await self.reply(request)

    async def healthy(self, request: web.Request) -> web.Response:
        return gzipped(ANSWER)

    async def poll(self, server: SimpleNamespace) -> tuple[dict | None, dict | None]:
        return await self.collector._fetch_metrics(server)


class DirectRequestTest(LiveNodeCase):
    async def test_answer_of_an_mtls_node_is_returned_with_the_moment_it_came(self):
        url = await self.start_node()
        before = time.time()

        metrics, error = await self.poll(node(url))

        self.assertIsNone(error)
        self.assertEqual(metrics["cpu"], ANSWER["cpu"])
        self.assertGreaterEqual(metrics["collected_at"], before)
        # От удачного опроса отсчитывается окно следующего
        self.assertIn(1, self.collector._last_success_mono)

    async def test_node_is_asked_for_the_window_since_its_last_poll(self):
        url = await self.start_node()
        window = self.collector._window_seconds(1)

        await self.poll(node(url))

        self.assertEqual(self.requests[0].query["window"], str(window))

    async def test_mtls_node_is_not_sent_the_api_key(self):
        url = await self.start_node()
        await self.poll(node(url))
        self.assertNotIn("X-API-Key", self.requests[0].headers)

    async def test_legacy_node_is_reached_without_a_client_certificate_and_gets_the_api_key(self):
        url = await self.start_node(issued_by="foreign", client_cert_required=False)

        metrics, error = await self.poll(node(url, mtls=False))

        self.assertIsNone(error)
        self.assertEqual(metrics["cpu"], ANSWER["cpu"])
        self.assertEqual(self.requests[0].headers["X-API-Key"], "legacy-key")

    async def test_plain_answer_of_a_node_without_gzip_is_read_too(self):
        url = await self.start_node()
        self.reply = mock.AsyncMock(return_value=web.json_response(ANSWER))

        metrics, error = await self.poll(node(url))

        self.assertIsNone(error)
        self.assertEqual(metrics["cpu"], ANSWER["cpu"])

    async def test_next_poll_goes_over_the_same_connection(self):
        url = await self.start_node()

        for _ in range(3):
            await self.poll(node(url))

        self.assertEqual(len({id(request.transport) for request in self.requests}), 1)


class DirectRequestFailureTest(LiveNodeCase):
    TIMEOUT = aiohttp.ClientTimeout(total=None, connect=10.0, sock_read=0.3)

    async def test_node_with_a_certificate_of_another_ca_is_an_ssl_error(self):
        url = await self.start_node(issued_by="foreign")

        metrics, error = await self.poll(node(url))

        self.assertIsNone(metrics)
        self.assertEqual(error, {"message": "SSL certificate error", "code": 495})
        self.assertEqual(self.requests, [])

    async def test_closed_port_is_connection_refused(self):
        # connect в этом классе длиннее боевого: Windows отвечает отказом только через две секунды
        metrics, error = await self.poll(node(f"https://127.0.0.1:{closed_port()}"))

        self.assertIsNone(metrics)
        self.assertEqual(error, {"message": "Connection refused", "code": 502})

    async def test_node_that_stays_silent_is_a_timeout(self):
        url = await self.start_node()
        release = asyncio.Event()
        self.addCleanup(release.set)

        async def silent(request: web.Request) -> web.Response:
            await release.wait()
            return gzipped(ANSWER)

        self.reply = silent

        metrics, error = await self.poll(node(url))

        self.assertIsNone(metrics)
        self.assertEqual(error, {"message": "Connection timeout", "code": 504})

    async def test_rejected_credentials_are_an_auth_error(self):
        url = await self.start_node()
        for status in (401, 403):
            self.reply = mock.AsyncMock(return_value=web.Response(status=status))
            metrics, error = await self.poll(node(url))
            self.assertIsNone(metrics)
            self.assertEqual(error, {"message": "Authentication failed", "code": status})

    async def test_other_status_is_a_server_error_with_the_status(self):
        url = await self.start_node()
        self.reply = mock.AsyncMock(return_value=web.Response(status=500))

        metrics, error = await self.poll(node(url))

        self.assertIsNone(metrics)
        self.assertEqual(error, {"message": "Server error: HTTP 500", "code": 500})
        # Окно следующего опроса по-прежнему считается от последнего удачного
        self.assertNotIn(1, self.collector._last_success_mono)

    async def test_redirect_is_not_followed(self):
        url = await self.start_node()
        self.reply = mock.AsyncMock(return_value=web.Response(status=302, headers={"Location": "/api/metrics"}))

        _, error = await self.poll(node(url))

        self.assertEqual(error, {"message": "Server error: HTTP 302", "code": 302})
        self.assertEqual(len(self.requests), 1)

    async def test_answer_that_is_not_json_is_an_unknown_error(self):
        url = await self.start_node()
        self.reply = mock.AsyncMock(return_value=web.Response(text="<html>bad gateway</html>"))

        metrics, error = await self.poll(node(url))

        self.assertIsNone(metrics)
        self.assertEqual(error["code"], 500)
        self.assertTrue(error["message"].startswith("Unknown error: "))


class OversizedAnswerTest(LiveNodeCase):
    async def rejected_poll(self, server: SimpleNamespace) -> tuple[dict | None, dict | None]:
        with self.assertLogs(metrics_collector.logger, level="WARNING") as captured:
            result = await self.poll(server)
        self.assertIn("node-1", captured.records[0].getMessage())
        return result

    async def test_gzip_bomb_is_an_invalid_response(self):
        url = await self.start_node()
        bomb = gzip_of_zeros(BOMB_BYTES)
        self.reply = mock.AsyncMock(
            return_value=web.Response(body=bomb, headers={"Content-Encoding": "gzip"}),
        )

        metrics, error = await self.rejected_poll(node(url))

        self.assertIsNone(metrics)
        self.assertEqual(error, {"message": "Invalid metrics response", "code": 502})

    async def test_plain_body_over_the_cap_is_an_invalid_response(self):
        url = await self.start_node()
        self.reply = mock.AsyncMock(return_value=web.Response(body=bytes(MAX_BODY_BYTES + 1)))

        metrics, error = await self.rejected_poll(node(url))

        self.assertIsNone(metrics)
        self.assertEqual(error, {"message": "Invalid metrics response", "code": 502})

    async def test_node_is_polled_again_after_an_oversized_answer(self):
        url = await self.start_node()
        self.reply = mock.AsyncMock(return_value=web.Response(body=bytes(MAX_BODY_BYTES + 1)))
        await self.rejected_poll(node(url))
        self.reply = self.healthy

        metrics, error = await self.poll(node(url))

        self.assertIsNone(error)
        self.assertEqual(metrics["cpu"], ANSWER["cpu"])

    async def test_oversized_answer_counts_as_a_failed_poll(self):
        url = await self.start_node()
        self.reply = mock.AsyncMock(return_value=web.Response(body=bytes(MAX_BODY_BYTES + 1)))

        with self.assertLogs(metrics_collector.logger, level="WARNING"):
            await self.collector._fetch_server_metrics(node(url))

        self.assertEqual(self.collector._node_failures[1], 1)


class Chunks(httpx.AsyncByteStream):
    """Тело ответа по кускам — и счёт того, сколько кусков клиент забрал."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.taken = 0

    async def __aiter__(self):
        for chunk in self._chunks:
            self.taken += 1
            yield chunk


class ProxiedRequestTest(unittest.IsolatedAsyncioTestCase):
    """Нода за SOCKS5-прокси: запрос идёт клиентом httpx, как и до aiohttp."""

    PROXIED = node("https://10.0.0.1:9100", proxy="192.0.2.10:1080")

    async def asyncSetUp(self) -> None:
        self.requests: list[httpx.Request] = []
        self.reply = lambda request: httpx.Response(
            200, headers={"Content-Encoding": "gzip"},
            stream=Chunks([gzip.compress(json.dumps(ANSWER).encode())]),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        self.addAsyncCleanup(client.aclose)
        for name, replacement in (
            ("get_node_client", mock.Mock(return_value=client)),
            ("get_metrics_session", mock.Mock(side_effect=AssertionError("a proxied node was polled directly"))),
        ):
            patcher = mock.patch.object(metrics_request, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.collector = MetricsCollector()

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.reply(request)

    def failing(self, error: Exception) -> None:
        def reply(request: httpx.Request) -> httpx.Response:
            raise error
        self.reply = reply

    async def poll(self) -> tuple[dict | None, dict | None]:
        return await self.collector._fetch_metrics(self.PROXIED)

    async def test_answer_comes_through_the_node_client(self):
        metrics, error = await self.poll()

        self.assertIsNone(error)
        self.assertEqual(metrics["cpu"], ANSWER["cpu"])
        self.assertIn("collected_at", metrics)
        [request] = self.requests
        self.assertEqual(request.url.path, "/api/metrics")
        self.assertEqual(request.url.params["window"], str(self.collector._window_seconds(1)))
        self.assertEqual(request.headers["Accept-Encoding"], "gzip")

    async def test_gzip_bomb_is_rejected_without_reading_the_rest_of_the_body(self):
        body = Chunks([gzip_of_zeros(BOMB_BYTES), b"tail", b"tail"])
        self.reply = lambda request: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=body)

        with self.assertLogs(metrics_collector.logger, level="WARNING"):
            metrics, error = await self.poll()

        self.assertIsNone(metrics)
        self.assertEqual(error, {"message": "Invalid metrics response", "code": 502})
        self.assertEqual(body.taken, 1)

    async def test_plain_body_over_the_cap_is_rejected(self):
        body = Chunks([bytes(MEGABYTE)] * (MAX_BODY_BYTES // MEGABYTE + 2))
        self.reply = lambda request: httpx.Response(200, stream=body)

        with self.assertLogs(metrics_collector.logger, level="WARNING"):
            _, error = await self.poll()

        self.assertEqual(error, {"message": "Invalid metrics response", "code": 502})
        self.assertEqual(body.taken, MAX_BODY_BYTES // MEGABYTE + 1)

    async def test_dead_proxy_is_told_apart_from_a_dead_node(self):
        self.failing(httpx.ProxyError("socks handshake failed"))
        self.assertEqual(await self.poll(), (None, {"message": "Proxy connection error", "code": 502}))

    async def test_timeout_through_the_proxy_is_a_timeout(self):
        self.failing(httpx.ReadTimeout("no answer"))
        self.assertEqual(await self.poll(), (None, {"message": "Connection timeout", "code": 504}))

    async def test_refused_connection_through_the_proxy(self):
        self.failing(httpx.ConnectError("[Errno 111] Connection refused"))
        self.assertEqual(await self.poll(), (None, {"message": "Connection refused", "code": 502}))

    async def test_certificate_failure_through_the_proxy(self):
        self.failing(httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
        self.assertEqual(await self.poll(), (None, {"message": "SSL certificate error", "code": 495}))

    async def test_status_other_than_ok_is_reported_with_its_code(self):
        self.reply = lambda request: httpx.Response(403)
        self.assertEqual(await self.poll(), (None, {"message": "Authentication failed", "code": 403}))


if __name__ == "__main__":
    unittest.main()
