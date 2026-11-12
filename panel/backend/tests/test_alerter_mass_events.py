"""Тесты алертера на массовых событиях (app/services/server_alerter.py).

Голый unittest, без PostgreSQL и без сети: сессия, Telegram и пробы подменены.

Авария хостера роняет сотню нод разом. Сообщение на каждую упирается в лимит
Telegram (~20 сообщений в минуту в группу): алерты приходят с опозданием на минуты
или теряются. Один сервер с кривыми метриками срывал проверку всем, кто шёл после
него. А сами метрики алертер каждый обход поднимал из базы и разбирал заново, хотя
разобранный ответ ноды уже лежит в памяти процесса.

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

from sqlalchemy.dialects import postgresql  # noqa: E402

from app.models import AlertHistory, AlertSettings  # noqa: E402
from app.services import server_alerter  # noqa: E402
from app.services.latest_metrics import LatestMetrics  # noqa: E402

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


class FakeTelegram:
    """Подмена TelegramBotService: копит тексты вместо отправки."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.delivered = True
        self.crashes_on: str | None = None

    async def send_message(self, bot_token: str, chat_id: str, text: str) -> bool:
        if self.crashes_on and self.crashes_on in text:
            raise RuntimeError("telegram client crashed")
        self.texts.append(text)
        return self.delivered


class FakeDatabase:
    """Подмена async_session: на запрос отдаёт серверы, строки истории копит."""

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
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self.servers)))

    def add(self, row: AlertHistory) -> None:
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
        self.alerter = server_alerter.ServerAlerter()
        # Пропавшая нода не отвечает ни по API, ни по ICMP
        self.alerter._active_probe_sequence = mock.AsyncMock(return_value=(True, False))

    def node_answered(self, srv: SimpleNamespace, answer: dict) -> None:
        self.latest.put(srv.id, answer)

    async def tick(self, servers: list, alert_settings: AlertSettings | None = None) -> FakeDatabase:
        db = FakeDatabase(servers)
        with mock.patch.object(server_alerter, "async_session", db):
            await self.alerter._check_all(alert_settings or settings())
        return db


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


if __name__ == "__main__":
    unittest.main()
