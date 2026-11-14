"""Тесты алертера на массовых событиях (app/services/server_alerter.py).

Голый unittest, без PostgreSQL и без сети: сессия, Telegram и пробы подменены.

Авария хостера роняет сотню нод разом. Сообщение на каждую упирается в лимит
Telegram (~20 сообщений в минуту в группу): алерты приходят с опозданием на минуты
или теряются. Один сервер с кривыми метриками срывал проверку всем, кто шёл после
него. А сами метрики алертер каждый обход поднимал из базы и разбирал заново, хотя
разобранный ответ ноды уже лежит в памяти процесса.

Массовая пропажа нод бывает и обрывом связи у самой панели: тогда мёртвым объявлялся
весь парк. А алерт, поднятый при недоступном Telegram, терялся — в истории он
оставался «не отправлен» навсегда.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aiogram.exceptions import TelegramNetworkError  # noqa: E402
from aiogram.methods import SendMessage  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402

from app.models import AlertHistory, AlertSettings  # noqa: E402
from app.services import server_alerter  # noqa: E402
from app.services.latest_metrics import LatestMetrics  # noqa: E402
from app.services.loss_alerts import HistoryRecord  # noqa: E402
from app.services.socks_probe import ProxyFault  # noqa: E402
from app.services.telegram_bot import TelegramBotService  # noqa: E402

TOKEN = "123456:ABCDEFghijklmnopqrstuvwxyz0123456789"
CHAT_ID = "-1001"
CRITICAL = "\U0001f534"
WARNING = "\U0001f7e1"
INFO = "\U0001f7e2"
# Заведомо больше порога сводки и больше, чем имён в неё помещается
OUTAGE = 120
NAMES_IN_DIGEST = 30
HIDDEN = OUTAGE - NAMES_IN_DIGEST


def settings(**overrides) -> AlertSettings:
    """Включён только offline; незаданные триггеры и списки исключений пусты."""
    values = dict(
        language="en", telegram_bot_token=TOKEN, telegram_chat_id=CHAT_ID,
        check_interval=60, alert_cooldown=1800,
        offline_enabled=True, offline_fail_threshold=3, offline_recovery_notify=True,
    )
    values.update(overrides)
    return AlertSettings(**values)


def load_alerts() -> AlertSettings:
    return settings(load_avg_enabled=True, load_avg_sustained_checks=1)


def node(server_id: int, *, silent_for_seconds: float, name: str | None = None) -> SimpleNamespace:
    last_seen = datetime.now(timezone.utc) - timedelta(seconds=silent_for_seconds)
    return SimpleNamespace(id=server_id, name=name or f"node-{server_id:03d}", last_seen=last_seen, proxy_url=None)


def silent(server_id: int, name: str | None = None) -> SimpleNamespace:
    return node(server_id, silent_for_seconds=3600, name=name)


def alive(server_id: int) -> SimpleNamespace:
    return node(server_id, silent_for_seconds=5)


def metrics(load_avg: float) -> dict:
    return {
        "cpu": {"cores_logical": 2, "load_avg_1": load_avg, "usage_percent": 35.0},
        "memory": {"ram": {"percent": 40.0}},
    }


def overloaded() -> dict:
    return metrics(load_avg=9.0)


def offline_text(name: str) -> str:
    return f"Server {name} is offline (API and ICMP unreachable)"


def load_text(name: str) -> str:
    return f"Load Average high on {name}: 9.00 (threshold 3.0, cores: 2)"


def names_of(servers: list) -> str:
    return ", ".join(srv.name for srv in servers)


def alert_text(emoji: str, message: str, header: str = "Server Alert") -> str:
    return f"{emoji} <b>{header}</b>\n\n{message}"


def isolated_text(silent_count: int, active: int) -> str:
    return alert_text(
        CRITICAL,
        f"The panel has lost outbound connectivity: {silent_count} of {active} servers are silent "
        "and none of the check addresses (1.1.1.1:443, 8.8.8.8:443, 9.9.9.9:443) responds. "
        "Servers are not reported offline until connectivity returns",
        header="Panel Alert",
    )


RECONNECTED_TEXT = alert_text(INFO, "Panel outbound connectivity is restored after 1 min", header="Panel Alert")


class FakeTelegram:
    """Подмена TelegramBotService: копит тексты вместо отправки.

    С `delivered = False` ведёт себя как сервис при недоступном Telegram: сообщение
    с retry_later встаёт в очередь повтора и доходит по `deliver_queued()`.
    """

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.delivered = True
        self.crashes_on: str | None = None
        self.sent_without_retry: list[str] = []
        self.queued: list[tuple[str, object]] = []
        self.queued_ages: list[float] = []

    async def send_message(
        self, bot_token: str, chat_id: str, text: str, *, retry_later: bool = False, on_delivered=None,
    ) -> bool:
        if self.crashes_on and self.crashes_on in text:
            raise RuntimeError("telegram client crashed")
        self.texts.append(text)
        if not retry_later:
            self.sent_without_retry.append(text)
        elif not self.delivered:
            self.queued.append((text, on_delivered))
        return self.delivered

    def queue_message(self, bot_token: str, chat_id: str, text: str, *, on_delivered=None, age: float = 0.0) -> None:
        self.queued.append((text, on_delivered))
        self.queued_ages.append(age)

    async def deliver_queued(self, count: int | None = None) -> list[str]:
        """Telegram снова доступен: очередь повтора доставляет первые `count` сообщений."""
        count = len(self.queued) if count is None else count
        batch, self.queued = self.queued[:count], self.queued[count:]
        for _text, on_delivered in batch:
            await on_delivered()
        return [text for text, _on_delivered in batch]


class FakeDatabase:
    """Подмена async_session: на запрос отдаёт серверы, строки истории копит.

    Понимает два запроса алертера к истории: «отметить доставленными» и
    «что осталось недоставленным».
    """

    def __init__(self, servers: list) -> None:
        self.servers = servers
        self.statements: list = []
        self.history: list[AlertHistory] = []

    def __call__(self) -> "FakeDatabase":
        return self

    async def __aenter__(self) -> "FakeDatabase":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def execute(self, statement) -> SimpleNamespace:
        self.statements.append(statement)
        if statement.is_update:
            params = statement.compile(dialect=postgresql.dialect()).params
            for row in self.history:
                if row.id in params["id_1"]:
                    row.notified = params["notified"]
        undelivered = [(row.id, row.details, row.created_at) for row in self.history if not row.notified]
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: list(self.servers)),
            all=lambda: undelivered,
        )

    def add(self, row: AlertHistory) -> None:
        row.id = len(self.history) + 1
        row.created_at = datetime.now(timezone.utc)
        self.history.append(row)

    async def commit(self) -> None:
        pass


class AlerterTickTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.telegram = FakeTelegram()
        self.latest = LatestMetrics()
        for patcher in (
            mock.patch("app.services.telegram_bot.get_telegram_bot_service", return_value=self.telegram),
            mock.patch.object(server_alerter, "get_latest_metrics", return_value=self.latest),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.alerter = self.new_alerter()

    def new_alerter(self) -> server_alerter.ServerAlerter:
        alerter = server_alerter.ServerAlerter()
        # Пропавшая нода не отвечает ни по API, ни по ICMP
        alerter._active_probe_sequence = mock.AsyncMock(return_value=(True, False))
        # У самой панели связь есть; настоящих подключений наружу тест не делает
        alerter._panel_reaches_outside = mock.AsyncMock(return_value=True)
        return alerter

    def node_answered(self, srv: SimpleNamespace, answer: dict) -> None:
        self.latest.put(srv.id, answer)

    async def tick(
        self, servers: list, alert_settings: AlertSettings | None = None, db: FakeDatabase | None = None,
    ) -> FakeDatabase:
        db = db or FakeDatabase(servers)
        db.servers = servers
        with mock.patch.object(server_alerter, "async_session", db):
            await self.alerter._check_all(alert_settings or settings())
        return db

    async def telegram_recovers(self, db: FakeDatabase, count: int | None = None) -> list[str]:
        with mock.patch.object(server_alerter, "async_session", db):
            return await self.telegram.deliver_queued(count)


class OutageDigestTest(AlerterTickTestCase):
    async def test_hoster_outage_is_one_message_instead_of_one_per_server(self):
        servers = [silent(i) for i in range(OUTAGE, 0, -1)]

        await self.tick(servers)

        shown = names_of(sorted(servers, key=lambda srv: srv.name)[:NAMES_IN_DIGEST])
        self.assertEqual(
            self.telegram.texts,
            [f"{CRITICAL} <b>Server Alert</b>\n\nOffline — servers: {OUTAGE}\n{shown}\n… and {HIDDEN} more"],
        )

    async def test_every_server_of_the_outage_keeps_its_own_history_row(self):
        servers = [silent(i) for i in range(1, OUTAGE + 1)]

        db = await self.tick(servers)

        self.assertEqual(
            [(row.server_id, row.server_name, row.alert_type, row.severity, row.message) for row in db.history],
            [(srv.id, srv.name, "offline", "critical", offline_text(srv.name)) for srv in servers],
        )
        self.assertTrue(all(row.notified for row in db.history))
        self.assertEqual({row.details for row in db.history}, {'{"icmp_reachable": false}'})

    async def test_outage_marks_every_server_offline_and_is_not_reported_twice(self):
        servers = [silent(i) for i in range(1, OUTAGE + 1)]

        await self.tick(servers)
        await self.tick(servers)

        states = [self.alerter._states[srv.id] for srv in servers]
        self.assertTrue(all(state.was_offline and "offline" in state.last_alert for state in states))
        self.assertEqual(len(self.telegram.texts), 1)

    async def test_few_offline_servers_get_a_message_each_as_before(self):
        servers = [silent(i) for i in range(1, 4)]

        db = await self.tick(servers)

        self.assertEqual(
            self.telegram.texts,
            [f"{CRITICAL} <b>Server Alert</b>\n\n{offline_text(srv.name)}" for srv in servers],
        )
        self.assertEqual([row.message for row in db.history], [offline_text(srv.name) for srv in servers])

    async def test_outage_at_the_threshold_still_sends_a_message_per_server(self):
        count = server_alerter.ALERT_DIGEST_THRESHOLD

        await self.tick([silent(i) for i in range(1, count + 1)])

        self.assertEqual(len(self.telegram.texts), count)

    async def test_one_server_above_the_threshold_turns_the_outage_into_a_digest(self):
        servers = [silent(i) for i in range(1, server_alerter.ALERT_DIGEST_THRESHOLD + 2)]

        await self.tick(servers)

        self.assertEqual(
            self.telegram.texts,
            [f"{CRITICAL} <b>Server Alert</b>\n\nOffline — servers: {len(servers)}\n{names_of(servers)}"],
        )

    async def test_recovery_after_the_outage_is_one_message_too(self):
        servers = [silent(i) for i in range(1, OUTAGE + 1)]
        await self.tick(servers)
        self.telegram.texts.clear()
        for srv in servers:
            srv.last_seen = datetime.now(timezone.utc)

        db = await self.tick(servers)

        shown = names_of(servers[:NAMES_IN_DIGEST])
        self.assertEqual(
            self.telegram.texts,
            [f"{INFO} <b>Server Alert</b>\n\nBack online — servers: {OUTAGE}\n{shown}\n… and {HIDDEN} more"],
        )
        self.assertEqual(
            [(row.alert_type, row.message) for row in db.history],
            [("recovery", f"Server {srv.name} is back online") for srv in servers],
        )
        self.assertFalse(any(self.alerter._states[srv.id].was_offline for srv in servers))

    async def test_digest_is_written_in_the_language_of_notifications(self):
        servers = [silent(i) for i in range(1, 46)]

        await self.tick(servers, settings(language="ru"))

        shown = names_of(servers[:NAMES_IN_DIGEST])
        self.assertEqual(
            self.telegram.texts,
            [f"{CRITICAL} <b>Уведомление сервера</b>\n\nНедоступны — серверов: 45\n{shown}\n… ещё 15"],
        )

    async def test_digest_that_telegram_did_not_accept_leaves_the_rows_unnotified(self):
        self.telegram.delivered = False

        db = await self.tick([silent(i) for i in range(1, OUTAGE + 1)])

        self.assertEqual(len(db.history), OUTAGE)
        self.assertFalse(any(row.notified for row in db.history))

    async def test_server_name_cannot_break_the_digest_markup(self):
        servers = [silent(i) for i in range(1, 20)] + [silent(20, name="<b>edge & co")]

        await self.tick(servers)

        self.assertEqual(len(self.telegram.texts), 1)
        self.assertIn("&lt;b&gt;edge &amp; co", self.telegram.texts[0])
        self.assertNotIn("<b>edge", self.telegram.texts[0])

    def test_every_digest_title_is_translated(self):
        titles = server_alerter.ALERT_DIGEST_TITLES
        self.assertEqual(set(titles["ru"]), set(titles["en"]))


class MixedAlertsTest(AlerterTickTestCase):
    async def test_storm_of_one_alert_leaves_other_alerts_as_separate_messages(self):
        first, last = alive(1), alive(22)
        outage = [silent(i) for i in range(2, 22)]
        for srv in (first, last):
            self.node_answered(srv, overloaded())

        await self.tick([first, *outage, last], load_alerts())

        self.assertEqual(self.telegram.texts, [
            f"{WARNING} <b>Server Alert</b>\n\n{load_text(first.name)}",
            f"{CRITICAL} <b>Server Alert</b>\n\nOffline — servers: 20\n{names_of(outage)}",
            f"{WARNING} <b>Server Alert</b>\n\n{load_text(last.name)}",
        ])

    async def test_any_alert_raised_by_many_servers_at_once_is_digested(self):
        servers = [alive(i) for i in range(1, 21)]
        for srv in servers:
            self.node_answered(srv, overloaded())

        db = await self.tick(servers, load_alerts())

        self.assertEqual(
            self.telegram.texts,
            [f"{WARNING} <b>Server Alert</b>\n\nLoad Average high — servers: 20\n{names_of(servers)}"],
        )
        self.assertEqual([row.message for row in db.history], [load_text(srv.name) for srv in servers])

    async def test_crash_while_sending_one_alert_does_not_lose_the_alerts_after_it(self):
        first, second, third = servers = [silent(i) for i in range(1, 4)]
        self.telegram.crashes_on = second.name

        with self.assertRaises(RuntimeError):
            await self.tick(servers)
        db = await self.tick(servers)

        self.assertEqual(
            self.telegram.texts,
            [f"{CRITICAL} <b>Server Alert</b>\n\n{offline_text(srv.name)}" for srv in (first, third)],
        )
        self.assertEqual([row.server_name for row in db.history], [third.name])


class BrokenServerTest(AlerterTickTestCase):
    async def test_server_with_malformed_metrics_does_not_stop_the_tick(self):
        healthy_before, broken, healthy_after = alive(1), alive(2), alive(3)
        self.node_answered(healthy_before, overloaded())
        self.node_answered(broken, {"cpu": None})
        self.node_answered(healthy_after, overloaded())

        with self.assertLogs(server_alerter.logger, level="ERROR") as logs:
            db = await self.tick([healthy_before, broken, healthy_after], load_alerts())

        self.assertEqual([row.server_name for row in db.history], [healthy_before.name, healthy_after.name])
        self.assertIn(broken.name, "\n".join(logs.output))

    async def test_fleet_wide_breakage_names_every_server_but_dumps_one_traceback(self):
        servers = [alive(i) for i in range(1, 4)]
        for srv in servers:
            self.node_answered(srv, {"cpu": None})

        with self.assertLogs(server_alerter.logger, level="ERROR") as logs:
            await self.tick(servers, load_alerts())

        self.assertEqual(len(logs.records), len(servers))
        for srv, record in zip(servers, logs.records, strict=True):
            self.assertIn(srv.name, record.getMessage())
        self.assertEqual([bool(record.exc_info) for record in logs.records], [True, False, False])


class MetricsSourceTest(AlerterTickTestCase):
    async def test_alerter_judges_by_the_answer_in_memory_not_by_the_database_copy(self):
        srv = alive(1)
        # Копия в базе отстаёт на минуты: по ней сервер ещё спокоен
        srv.last_metrics = json.dumps(metrics(load_avg=0.1))
        self.node_answered(srv, overloaded())

        db = await self.tick([srv], load_alerts())

        self.assertEqual([row.alert_type for row in db.history], ["load_avg_high"])

    async def test_fleet_query_leaves_the_metrics_column_in_the_database(self):
        db = await self.tick([alive(1)])

        queries = [str(statement.compile(dialect=postgresql.dialect())) for statement in db.statements]
        fleet_queries = [sql for sql in queries if "FROM servers" in sql]
        self.assertEqual(len(fleet_queries), 1)
        self.assertNotIn("last_metrics", fleet_queries[0])


class PanelIsolationTest(AlerterTickTestCase):
    """Панель без связи наружу не видит ни одной ноды. Пробы объявили бы мёртвым
    весь парк — вместо этого панель сообщает о себе и ждёт, пока связь вернётся."""

    def setUp(self) -> None:
        super().setUp()
        self.alerter._panel_reaches_outside.return_value = False
        self.fleet = [silent(i) for i in range(1, OUTAGE + 1)]

    def connectivity_returns(self) -> None:
        self.alerter._panel_reaches_outside.return_value = True
        self.telegram.texts.clear()

    async def test_isolated_panel_reports_itself_instead_of_the_whole_fleet(self):
        db = await self.tick(self.fleet)

        self.assertEqual(self.telegram.texts, [isolated_text(OUTAGE, OUTAGE)])
        self.assertEqual(
            [(row.server_id, row.server_name, row.alert_type, row.severity) for row in db.history],
            [(None, "Panel server", "panel_isolated", "critical")],
        )

    async def test_isolated_panel_probes_nobody_and_marks_nobody_offline(self):
        await self.tick(self.fleet)

        self.alerter._active_probe_sequence.assert_not_awaited()
        self.assertFalse(any(state.was_offline for state in self.alerter._states.values()))

    async def test_isolation_is_logged_and_reported_once_per_episode(self):
        with self.assertLogs(server_alerter.logger, level="WARNING") as logs:
            await self.tick(self.fleet)
        with self.assertNoLogs(server_alerter.logger, level="WARNING"):
            await self.tick(self.fleet)
            await self.tick(self.fleet)

        self.assertEqual(len(logs.records), 1)
        self.assertEqual(len(self.telegram.texts), 1)

    async def test_isolation_alert_is_written_in_the_language_of_notifications(self):
        db = await self.tick(self.fleet, settings(language="ru"))

        self.assertEqual(
            self.telegram.texts,
            [alert_text(
                CRITICAL,
                f"Панель потеряла связь с внешним миром: молчат {OUTAGE} из {OUTAGE} серверов, "
                "и ни один из адресов проверки (1.1.1.1:443, 8.8.8.8:443, 9.9.9.9:443) не отвечает. "
                "Пока связи нет, серверы недоступными не объявляются",
                header="Уведомление панели",
            )],
        )
        self.assertEqual(db.history[0].server_name, "Сервер панели")

    async def test_returned_connectivity_is_reported_and_dead_nodes_are_checked_again(self):
        await self.tick(self.fleet)
        self.connectivity_returns()

        db = await self.tick(self.fleet)

        self.assertEqual(self.telegram.texts[0], RECONNECTED_TEXT)
        self.assertIn(f"Offline — servers: {OUTAGE}", self.telegram.texts[1])
        self.assertEqual(len(self.telegram.texts), 2)
        self.assertEqual(db.history[0].alert_type, "panel_reconnected")
        self.assertEqual(self.alerter._active_probe_sequence.await_count, OUTAGE)

    async def test_returned_connectivity_is_reported_when_the_fleet_is_back_by_itself(self):
        await self.tick(self.fleet)
        self.connectivity_returns()

        await self.tick([alive(srv.id) for srv in self.fleet])

        self.assertEqual(self.telegram.texts, [RECONNECTED_TEXT])

    async def test_isolation_that_comes_back_within_the_cooldown_is_not_reported_twice(self):
        await self.tick(self.fleet)
        self.connectivity_returns()
        await self.tick([alive(srv.id) for srv in self.fleet])
        self.alerter._panel_reaches_outside.return_value = False
        self.telegram.texts.clear()

        await self.tick(self.fleet)
        self.connectivity_returns()
        await self.tick([alive(srv.id) for srv in self.fleet])

        self.assertEqual(self.telegram.texts, [])

    async def test_isolation_alert_waits_for_telegram_and_arrives_when_connectivity_returns(self):
        self.telegram.delivered = False

        db = await self.tick(self.fleet)

        self.assertEqual([row.notified for row in db.history], [False])
        self.assertEqual(await self.telegram_recovers(db), [isolated_text(OUTAGE, OUTAGE)])
        self.assertEqual([row.notified for row in db.history], [True])


class MassOfflineThresholdTest(AlerterTickTestCase):
    """Связь панели проверяется, только когда разом пропало много нод: не меньше 20
    и не меньше 30% парка."""

    async def connectivity_checks(self, silent_count: int, alive_count: int) -> int:
        servers = [silent(i) for i in range(1, silent_count + 1)]
        servers += [alive(silent_count + i) for i in range(1, alive_count + 1)]
        await self.tick(servers)
        return self.alerter._panel_reaches_outside.await_count

    async def test_outage_of_a_few_nodes_is_probed_without_checking_the_panel(self):
        self.assertEqual(await self.connectivity_checks(silent_count=19, alive_count=0), 0)

    async def test_outage_of_a_small_share_of_a_big_fleet_is_probed_without_checking_the_panel(self):
        self.assertEqual(await self.connectivity_checks(silent_count=29, alive_count=71), 0)

    async def test_outage_of_a_third_of_the_fleet_checks_the_panel_first(self):
        self.assertEqual(await self.connectivity_checks(silent_count=30, alive_count=70), 1)

    async def test_whole_small_fleet_going_silent_checks_the_panel_first(self):
        self.assertEqual(await self.connectivity_checks(silent_count=20, alive_count=0), 1)

    async def test_nodes_already_reported_offline_do_not_count_as_a_new_outage(self):
        fleet = [silent(i) for i in range(1, 31)]
        await self.tick(fleet)

        await self.tick(fleet)

        self.assertEqual(self.alerter._panel_reaches_outside.await_count, 1)


class SharedProxyTest(AlerterTickTestCase):
    """Проверка прокси длится до 5 секунд, а за одним прокси стоят десятки нод."""

    PROXY = "192.0.2.7:1080@user:secret"

    def behind_proxy(self, server_id: int, proxy: str = PROXY) -> SimpleNamespace:
        srv = silent(server_id)
        srv.proxy_url = proxy
        return srv

    def proxy_check(self, fault: ProxyFault | None = ProxyFault.UNREACHABLE):
        return mock.patch.object(server_alerter, "find_proxy_fault", mock.AsyncMock(return_value=fault))

    async def test_proxy_shared_by_dead_nodes_is_checked_once_per_tick(self):
        servers = [self.behind_proxy(i) for i in range(1, 6)]

        with self.proxy_check() as check:
            db = await self.tick(servers)

        check.assert_awaited_once()
        self.assertEqual({json.loads(row.details)["proxy_fault"] for row in db.history}, {"unreachable"})
        self.assertEqual(len(db.history), len(servers))

    async def test_healthy_proxy_is_remembered_for_the_tick_too(self):
        with self.proxy_check(fault=None) as check:
            await self.tick([self.behind_proxy(i) for i in range(1, 6)])

        check.assert_awaited_once()

    async def test_different_proxies_are_checked_separately(self):
        with self.proxy_check() as check:
            await self.tick([self.behind_proxy(1), self.behind_proxy(2, proxy="198.51.100.9:1080")])

        self.assertEqual(check.await_count, 2)

    async def test_next_tick_checks_the_proxy_again(self):
        with self.proxy_check() as check:
            await self.tick([self.behind_proxy(1)])
            await self.tick([self.behind_proxy(1), self.behind_proxy(2)])

        self.assertEqual(check.await_count, 2)


class LateDeliveryTest(AlerterTickTestCase):
    """Telegram недоступен в момент алерта. Сообщение уходит позже из очереди
    повтора сервиса — и только тогда строки истории становятся «отправлено»."""

    def setUp(self) -> None:
        super().setUp()
        self.telegram.delivered = False

    def waiting(self) -> list[str]:
        return [text for text, _on_delivered in self.telegram.queued]

    async def test_every_alert_and_digest_asks_telegram_to_retry_later(self):
        self.telegram.delivered = True
        loaded = alive(500)
        self.node_answered(loaded, overloaded())

        await self.tick([loaded, *[silent(i) for i in range(1, OUTAGE + 1)]], load_alerts())
        self.alerter._panel_reaches_outside.return_value = False
        await self.tick([silent(i) for i in range(501, 501 + OUTAGE)])

        self.assertEqual(len(self.telegram.texts), 3)
        self.assertEqual(self.telegram.sent_without_retry, [])

    async def test_alert_delivered_later_becomes_notified_in_history(self):
        first, second = servers = [silent(1), silent(2)]
        db = await self.tick(servers)
        self.assertEqual([row.notified for row in db.history], [False, False])

        delivered = await self.telegram_recovers(db, count=1)

        self.assertEqual(delivered, [alert_text(CRITICAL, offline_text(first.name))])
        self.assertEqual([(row.server_name, row.notified) for row in db.history], [(first.name, True), (second.name, False)])

    async def test_digest_delivered_later_marks_every_server_of_the_outage(self):
        db = await self.tick([silent(i) for i in range(1, OUTAGE + 1)])
        self.assertEqual(len(self.telegram.queued), 1)

        await self.telegram_recovers(db)

        self.assertEqual(len(db.history), OUTAGE)
        self.assertTrue(all(row.notified for row in db.history))

    async def test_digest_that_arrives_while_history_is_being_written_marks_the_rows_written_after_it(self):
        servers = [silent(i) for i in range(1, OUTAGE + 1)]
        db = FakeDatabase(servers)
        commit = db.commit

        async def commit_and_deliver_after_the_tenth_row() -> None:
            await commit()
            if len(db.history) == 10 and self.telegram.queued:
                await self.telegram.deliver_queued()

        db.commit = commit_and_deliver_after_the_tenth_row
        await self.tick(servers, db=db)

        self.assertTrue(all(row.notified for row in db.history))

    async def test_history_keeps_the_exact_text_that_waits_for_telegram(self):
        srv = silent(1)

        db = await self.tick([srv])

        self.assertEqual(
            json.loads(db.history[0].details),
            {"icmp_reachable": False, "telegram_texts": [alert_text(CRITICAL, offline_text(srv.name))]},
        )

    async def test_digest_text_is_kept_once_and_the_other_rows_point_to_it(self):
        db = await self.tick([silent(i) for i in range(1, OUTAGE + 1)])

        first, *rest = [json.loads(row.details) for row in db.history]
        self.assertEqual(first["telegram_texts"], self.waiting())
        self.assertEqual(rest, [{"icmp_reachable": False, "telegram_texts_row": db.history[0].id}] * (OUTAGE - 1))

    async def test_alert_without_telegram_configured_is_saved_as_before(self):
        db = await self.tick([silent(1)], settings(telegram_bot_token=None))

        self.assertEqual(self.telegram.texts, [])
        self.assertEqual([(row.notified, row.details) for row in db.history], [(False, '{"icmp_reachable": false}')])


class BotBehindLostNetwork:
    """Бот aiogram, до которого сеть то есть, то нет."""

    def __init__(self) -> None:
        self.reachable = False
        self.delivered: list[str] = []

    async def send_message(self, *, chat_id: str, text: str, **_options) -> SimpleNamespace:
        if not self.reachable:
            raise TelegramNetworkError(method=SendMessage(chat_id=chat_id, text=text), message="Request timeout error")
        self.delivered.append(text)
        return SimpleNamespace(message_id=len(self.delivered))


class AlerterWithRealTelegramServiceTest(AlerterTickTestCase):
    """Алертер и настоящий TelegramBotService вместе: подменён только бот aiogram."""

    def setUp(self) -> None:
        super().setUp()
        self.bot = BotBehindLostNetwork()
        self.service = TelegramBotService()
        self.service._get_or_create_bot = mock.AsyncMock(return_value=self.bot)
        patcher = mock.patch("app.services.telegram_bot.get_telegram_bot_service", return_value=self.service)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_alerts_raised_without_telegram_arrive_in_order_and_become_notified(self):
        srv = silent(1)
        db = await self.tick([srv])
        srv.last_seen = datetime.now(timezone.utc)
        await self.tick([srv], db=db)
        self.assertEqual(self.bot.delivered, [])
        self.assertEqual([(row.alert_type, row.notified) for row in db.history], [("offline", False), ("recovery", False)])

        self.bot.reachable = True
        with mock.patch.object(server_alerter, "async_session", db):
            await self.service._flush_outbox()

        self.assertEqual(self.bot.delivered, [
            alert_text(CRITICAL, offline_text(srv.name)),
            alert_text(INFO, f"Server {srv.name} is back online"),
        ])
        self.assertEqual([row.notified for row in db.history], [True, True])


class RestartWithUndeliveredAlertsTest(AlerterTickTestCase):
    """Очередь повтора живёт в памяти сервиса Telegram и с рестартом панели пропадает.
    Недоставленное за последние 6 часов алертер ставит в неё заново из истории."""

    def setUp(self) -> None:
        super().setUp()
        self.telegram.delivered = False

    async def restart(self, db: FakeDatabase, alert_settings: AlertSettings | None = None) -> None:
        self.telegram.queued.clear()
        self.alerter = self.new_alerter()
        self.alerter._load_settings = mock.AsyncMock(return_value=alert_settings or settings(enabled=True))
        with mock.patch.object(server_alerter, "async_session", db):
            await self.alerter._requeue_undelivered()

    def waiting(self) -> list[str]:
        return [text for text, _on_delivered in self.telegram.queued]

    async def test_undelivered_alerts_are_queued_again_with_the_text_they_had(self):
        servers = [silent(1), silent(2)]
        db = await self.tick(servers)

        await self.restart(db)

        self.assertEqual(self.waiting(), [alert_text(CRITICAL, offline_text(srv.name)) for srv in servers])

    async def test_undelivered_digest_is_queued_again_as_one_message(self):
        db = await self.tick([silent(i) for i in range(1, OUTAGE + 1)])
        digest = self.waiting()

        await self.restart(db)

        self.assertEqual(len(digest), 1)
        self.assertEqual(self.waiting(), digest)

    async def test_alert_delivered_after_the_restart_marks_its_history_rows(self):
        db = await self.tick([silent(i) for i in range(1, OUTAGE + 1)])
        await self.restart(db)

        await self.telegram_recovers(db)

        self.assertTrue(all(row.notified for row in db.history))

    async def test_delivered_alerts_are_not_sent_again(self):
        db = await self.tick([silent(1), silent(2)])
        await self.telegram_recovers(db)

        await self.restart(db)

        self.assertEqual(self.waiting(), [])

    async def test_rows_that_never_waited_for_telegram_are_left_alone(self):
        db = await self.tick([silent(1)], settings(telegram_bot_token=None))
        db.add(AlertHistory(
            server_id=2, server_name="node-002", alert_type="gre_silent", severity="warning",
            message="written by another service", details='{"interface": "gre1"}', notified=False,
        ))
        db.add(AlertHistory(
            server_id=3, server_name="node-003", alert_type="offline", severity="critical",
            message="legacy row", details=None, notified=False,
        ))

        await self.restart(db)

        self.assertEqual(self.waiting(), [])

    async def test_nothing_is_queued_while_alerts_are_switched_off(self):
        db = await self.tick([silent(1)])

        await self.restart(db, settings(enabled=False))

        self.assertEqual(self.waiting(), [])

    async def test_requeued_alert_keeps_its_age_and_only_the_last_six_hours_are_read(self):
        db = await self.tick([silent(1)])
        db.history[0].created_at = datetime.now(timezone.utc) - timedelta(hours=2)

        await self.restart(db)

        self.assertAlmostEqual(self.telegram.queued_ages[0], 2 * 3600, delta=5)
        query = db.statements[-1].compile(dialect=postgresql.dialect())
        self.assertIn("alert_history.notified IS false", str(query))
        oldest_read = next(value for value in query.params.values() if isinstance(value, datetime))
        self.assertAlmostEqual(
            (datetime.now(timezone.utc).replace(tzinfo=None) - oldest_read).total_seconds(), 6 * 3600, delta=5,
        )


class LossDigestDeliveryTest(AlerterTickTestCase):
    """Сводка о потерях бывает из нескольких сообщений: её строки истории
    «отправлены», только когда дошли все части."""

    async def test_rows_become_notified_when_the_last_part_of_the_digest_arrives(self):
        self.telegram.delivered = False
        records = [
            HistoryRecord(1, "relay-1", False, "New loss: 203.0.113.5:443", {"kind": "new"}),
            HistoryRecord(2, "relay-2", True, "Loss is gone: 203.0.113.9:443", {"kind": "recovered"}),
        ]
        db = FakeDatabase([])
        with (
            mock.patch.object(server_alerter, "async_session", db),
            mock.patch.object(server_alerter, "format_digest", return_value=["part 1/2", "part 2/2"]),
            mock.patch.object(server_alerter, "history_records", return_value=records),
        ):
            await self.alerter._send_loss_digest(settings(), [], {}, None)

        self.assertEqual(self.telegram.sent_without_retry, [])
        self.assertEqual(
            [(row.server_name, row.alert_type, row.severity, row.message) for row in db.history],
            [
                ("relay-1", "packet_loss", "warning", "New loss: 203.0.113.5:443"),
                ("relay-2", "packet_loss_recovery", "info", "Loss is gone: 203.0.113.9:443"),
            ],
        )
        await self.telegram_recovers(db, count=1)
        self.assertEqual([row.notified for row in db.history], [False, False])
        await self.telegram_recovers(db)
        self.assertEqual([row.notified for row in db.history], [True, True])


if __name__ == "__main__":
    unittest.main()
