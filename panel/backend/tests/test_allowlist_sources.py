"""Тесты списков по URL для общего белого списка (app/services/allowlist_sources.py).

Адрес из такого списка проходит на нодах мимо блок-листа, файрвола и лимитов
аварийного режима, а составляет список чужой сайт. Проверяется, что в белый
список не попадает лишнее и что заминка сайта не снимает с него CDN.

Голый unittest, без PostgreSQL и без сети: сессия и HTTP-клиент подменены.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app.models import AllowlistSource  # noqa: E402
from app.routers import blocklist as blocklist_router  # noqa: E402
from app.services import allowlist_sources  # noqa: E402
from app.services.allowlist_sources import AllowlistSources, extract_networks  # noqa: E402

CLOUDFLARE = "https://www.cloudflare.com/ips-v4/"
YANDEX = "https://tech.cdn.yandex.net/prefixes/yc.json"
CLOUDFLARE_TEXT = "173.245.48.0/20\n104.16.0.0/13\n"


class ExtractNetworksTest(unittest.TestCase):
    def test_plain_text_list(self):
        self.assertEqual(extract_networks(CLOUDFLARE_TEXT), ["173.245.48.0/20", "104.16.0.0/13"])

    def test_json_list(self):
        body = '{"prefixes": [{"cidr": "51.250.0.0/17"}, {"cidr": "84.201.128.0/18"}]}'
        self.assertEqual(extract_networks(body), ["51.250.0.0/17", "84.201.128.0/18"])

    def test_single_address_and_repeats(self):
        self.assertEqual(extract_networks("5.9.0.10 5.9.0.10\n5.9.0.10/32"), ["5.9.0.10/32"])

    def test_host_bits_are_dropped(self):
        self.assertEqual(extract_networks("104.16.5.1/13"), ["104.16.0.0/13"])

    def test_private_and_service_ranges_are_left_out(self):
        self.assertEqual(extract_networks("10.0.0.0/8 127.0.0.1 192.168.1.0/24 172.17.0.1 5.9.0.10"), ["5.9.0.10/32"])

    def test_whole_internet_is_left_out(self):
        # Такая запись открыла бы все порты всех нод кому угодно
        self.assertEqual(extract_networks("0.0.0.0/0 128.0.0.0/1 8.0.0.0/7 5.9.0.10"), ["5.9.0.10/32"])

    def test_slash_eight_is_the_widest_accepted(self):
        self.assertEqual(extract_networks("11.0.0.0/8"), ["11.0.0.0/8"])

    def test_garbage_is_ignored(self):
        self.assertEqual(extract_networks("<html>999.1.1.1 1.2.3.4/99 no addresses</html>"), [])


class FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)

    def scalars(self) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: list(self._rows))


class FakeSession:
    """Таблица списков в памяти: отдаёт включённые строки и строки по id."""

    def __init__(self, sources: list[AllowlistSource]) -> None:
        self._sources = sources
        self.commits = 0

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def execute(self, statement) -> FakeResult:
        if len(statement.selected_columns) == 2:
            enabled = sorted((s for s in self._sources if s.enabled), key=lambda s: s.id)
            return FakeResult([(s.id, s.url) for s in enabled])
        wanted = next(value for value in statement.compile().params.values() if isinstance(value, (list, tuple, set)))
        return FakeResult([s for s in self._sources if s.id in wanted])

    async def commit(self) -> None:
        self.commits += 1


class FakeClient:
    def __init__(self) -> None:
        self.answers: dict[str, object] = {}
        self.asked: list[str] = []

    async def get(self, url: str, timeout: float) -> SimpleNamespace:
        self.asked.append(url)
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        status, text = answer
        return SimpleNamespace(status_code=status, text=text)


class CollectTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.cloudflare = AllowlistSource(id=1, name="Cloudflare", url=CLOUDFLARE, enabled=True, ip_count=0)
        self.yandex = AllowlistSource(id=2, name="Yandex Cloud", url=YANDEX, enabled=False, ip_count=0)
        self.session = FakeSession([self.cloudflare, self.yandex])
        self.client = FakeClient()
        self.client.answers[CLOUDFLARE] = (200, CLOUDFLARE_TEXT)
        self.client.answers[YANDEX] = (200, '{"cidr": "51.250.0.0/17"}')
        for name, replacement in (
            ("async_session", lambda: self.session),
            ("get_external_client", lambda: self.client),
        ):
            patcher = mock.patch.object(allowlist_sources, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sources = AllowlistSources()

    def due_again(self, url: str) -> None:
        self.sources._next_fetch[url] = 0.0

    async def test_addresses_of_enabled_lists_only(self):
        self.assertEqual(await self.sources.collect(), ["173.245.48.0/20", "104.16.0.0/13"])
        self.assertEqual(self.client.asked, [CLOUDFLARE])

    async def test_download_result_is_shown_on_the_list(self):
        await self.sources.collect()

        self.assertEqual(self.cloudflare.ip_count, 2)
        self.assertIsNone(self.cloudflare.error_message)
        self.assertIsNotNone(self.cloudflare.last_updated)

    async def test_list_is_not_downloaded_on_every_build(self):
        await self.sources.collect()
        again = await self.sources.collect()

        self.assertEqual(self.client.asked, [CLOUDFLARE])
        self.assertEqual(again, ["173.245.48.0/20", "104.16.0.0/13"])

    async def test_refresh_now_downloads_again(self):
        await self.sources.collect()
        self.client.answers[CLOUDFLARE] = (200, "173.245.48.0/20")
        self.sources.expire()

        self.assertEqual(await self.sources.collect(), ["173.245.48.0/20"])
        self.assertEqual(self.cloudflare.ip_count, 1)

    async def test_failed_download_keeps_the_last_good_copy(self):
        await self.sources.collect()
        self.client.answers[CLOUDFLARE] = (503, "")
        self.due_again(CLOUDFLARE)

        self.assertEqual(await self.sources.collect(), ["173.245.48.0/20", "104.16.0.0/13"])
        self.assertEqual(self.cloudflare.error_message, "HTTP 503")
        self.assertEqual(self.cloudflare.ip_count, 2)

    async def test_answer_without_addresses_is_a_failure(self):
        # Сайт отдал страницу-заглушку: CDN не должен выпасть из белого списка
        await self.sources.collect()
        self.client.answers[CLOUDFLARE] = (200, "<html>maintenance</html>")
        self.due_again(CLOUDFLARE)

        self.assertEqual(await self.sources.collect(), ["173.245.48.0/20", "104.16.0.0/13"])
        self.assertEqual(self.cloudflare.error_message, allowlist_sources.NO_ADDRESSES_ERROR)

    async def test_list_that_never_downloaded_gives_nothing(self):
        self.client.answers[CLOUDFLARE] = httpx.ConnectError("unreachable")

        self.assertEqual(await self.sources.collect(), [])
        self.assertEqual(self.cloudflare.error_message, "unreachable")

    async def test_counter_shows_what_the_list_gives_right_now(self):
        # Панель перезапускалась, запасной копии нет: прежнее число вводило бы в заблуждение
        self.cloudflare.ip_count = 15
        self.client.answers[CLOUDFLARE] = (503, "")

        await self.sources.collect()

        self.assertEqual(self.cloudflare.ip_count, 0)

    async def test_timeout_is_named(self):
        self.client.answers[CLOUDFLARE] = httpx.ReadTimeout("slow")

        await self.sources.collect()

        self.assertEqual(self.cloudflare.error_message, "timeout")

    async def test_failing_site_is_not_asked_on_every_build(self):
        self.client.answers[CLOUDFLARE] = (503, "")
        await self.sources.collect()
        await self.sources.collect()

        self.assertEqual(self.client.asked, [CLOUDFLARE])

    async def test_recovered_list_clears_the_error(self):
        self.client.answers[CLOUDFLARE] = (503, "")
        await self.sources.collect()
        self.client.answers[CLOUDFLARE] = (200, CLOUDFLARE_TEXT)
        self.due_again(CLOUDFLARE)

        self.assertEqual(await self.sources.collect(), ["173.245.48.0/20", "104.16.0.0/13"])
        self.assertIsNone(self.cloudflare.error_message)

    async def test_switched_off_list_leaves_the_allowlist_at_once(self):
        await self.sources.collect()
        self.cloudflare.enabled = False

        self.assertEqual(await self.sources.collect(), [])

    async def test_list_switched_back_on_is_downloaded_again(self):
        # Пока список был выключен, сайт мог его сменить
        await self.sources.collect()
        self.cloudflare.enabled = False
        await self.sources.collect()
        self.cloudflare.enabled = True

        await self.sources.collect()

        self.assertEqual(self.client.asked, [CLOUDFLARE, CLOUDFLARE])

    async def test_switched_on_list_is_downloaded_at_once(self):
        await self.sources.collect()
        self.yandex.enabled = True

        self.assertEqual(await self.sources.collect(), ["173.245.48.0/20", "104.16.0.0/13", "51.250.0.0/17"])
        self.assertEqual(self.client.asked, [CLOUDFLARE, YANDEX])

    async def test_nothing_is_written_when_nothing_was_downloaded(self):
        await self.sources.collect()
        commits = self.session.commits

        await self.sources.collect()

        self.assertEqual(self.session.commits, commits)


class EndpointsTest(unittest.IsolatedAsyncioTestCase):
    """Обработчики /blocklist/allow-sources: когда список скачивается и когда нодам уходит белый список."""

    async def asyncSetUp(self) -> None:
        self.sources = mock.Mock(collect=mock.AsyncMock(return_value=[]), expire=mock.Mock())
        self.blocklist = mock.Mock()
        for name, replacement in (
            ("get_allowlist_sources", lambda: self.sources),
            ("get_blocklist_manager", lambda: self.blocklist),
        ):
            patcher = mock.patch.object(blocklist_router, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stored = AllowlistSource(id=1, name="Cloudflare", url=CLOUDFLARE, enabled=False, ip_count=0)
        self.db = mock.Mock(add=mock.Mock(), commit=mock.AsyncMock(), refresh=mock.AsyncMock(), delete=mock.AsyncMock())

    def found(self, entity: object) -> None:
        self.db.execute = mock.AsyncMock(return_value=SimpleNamespace(
            scalar_one_or_none=lambda: entity,
            scalars=lambda: SimpleNamespace(all=lambda: [self.stored]),
        ))

    async def test_new_list_is_downloaded_before_the_answer(self):
        self.found(None)

        answer = await blocklist_router.add_allow_source(
            blocklist_router.AddAllowSourceRequest(name="Cloudflare", url=f"  {CLOUDFLARE} "), db=self.db, _={}
        )

        self.assertEqual(answer["source"]["url"], CLOUDFLARE)
        self.sources.collect.assert_awaited_once_with()
        self.blocklist.request_allowlist_push.assert_called_once_with()

    async def test_same_url_is_not_added_twice(self):
        self.found(1)

        with self.assertRaises(HTTPException) as refused:
            await blocklist_router.add_allow_source(
                blocklist_router.AddAllowSourceRequest(name="Again", url=CLOUDFLARE), db=self.db, _={}
            )

        self.assertEqual(refused.exception.status_code, 400)
        self.db.add.assert_not_called()
        self.sources.collect.assert_not_awaited()

    def test_only_http_addresses_are_accepted(self):
        for url in ("ftp://example.com/list", "file:///etc/passwd", "cloudflare.com/ips-v4"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                blocklist_router.AddAllowSourceRequest(name="x", url=url)

    async def test_switched_on_list_is_downloaded_before_the_answer(self):
        self.found(self.stored)

        await blocklist_router.update_allow_source(
            1, blocklist_router.UpdateAllowSourceRequest(enabled=True), db=self.db, _={}
        )

        self.assertTrue(self.stored.enabled)
        self.sources.collect.assert_awaited_once_with()
        self.blocklist.request_allowlist_push.assert_called_once_with()

    async def test_switched_off_list_only_changes_the_allowlist(self):
        self.stored.enabled = True
        self.found(self.stored)

        await blocklist_router.update_allow_source(
            1, blocklist_router.UpdateAllowSourceRequest(enabled=False), db=self.db, _={}
        )

        self.sources.collect.assert_not_awaited()
        self.blocklist.request_allowlist_push.assert_called_once_with()

    async def test_rename_does_not_touch_the_nodes(self):
        self.found(self.stored)

        await blocklist_router.update_allow_source(
            1, blocklist_router.UpdateAllowSourceRequest(name="CF"), db=self.db, _={}
        )

        self.assertEqual(self.stored.name, "CF")
        self.blocklist.request_allowlist_push.assert_not_called()

    async def test_removed_list_leaves_the_allowlist(self):
        self.found(self.stored)

        await blocklist_router.delete_allow_source(1, db=self.db, _={})

        self.db.delete.assert_awaited_once_with(self.stored)
        self.blocklist.request_allowlist_push.assert_called_once_with()

    async def test_unknown_list_is_reported(self):
        self.found(None)

        with self.assertRaises(HTTPException) as missing:
            await blocklist_router.delete_allow_source(7, db=self.db, _={})

        self.assertEqual(missing.exception.status_code, 404)

    async def test_refresh_now_downloads_every_list_again(self):
        self.found(self.stored)
        order = mock.Mock()
        order.attach_mock(self.sources.expire, "expire")
        order.attach_mock(self.sources.collect, "collect")

        answer = await blocklist_router.refresh_allow_sources(db=self.db, _={})

        self.assertEqual(order.mock_calls, [mock.call.expire(), mock.call.collect()])
        self.assertEqual([source["url"] for source in answer["sources"]], [CLOUDFLARE])
        self.blocklist.request_allowlist_push.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
