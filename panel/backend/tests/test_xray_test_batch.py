"""Пачка проверок в одном процессе ядра.

Раньше на каждую ячейку поднимался свой процесс — на большом прогоне это сотни
запусков, а на боевой ноде ещё и заметная нагрузка. Ядро держит сколько угодно
inbound'ов, поэтому пачке хватает одного процесса: у каждой проверки свой
socks-порт, а маршрут внутри конфига связывает порт с её конфигурацией.

Здесь проверяется то, что при таком объединении можно сломать молча: связь
порт↔конфигурация и разбор общего лога ядра по проверкам.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.xray_test.config_builder import BatchEntry, build_batch, build_config  # noqa: E402
from app.services.xray_test.models import Core  # noqa: E402
from app.services.xray_test.parsers import parse_link  # noqa: E402
from app.services.xray_test.runner import _lines_for_slot  # noqa: E402

UUID = "11111111-2222-3333-4444-555555555555"


def _endpoint(host: str = "h.io", **extra):
    query = "&".join(f"{key}={value}" for key, value in extra.items())
    tail = f"&{query}" if query else ""
    return parse_link(f"vless://{UUID}@{host}:443?security=tls{tail}#{host}")


def _entries(count: int) -> list[BatchEntry]:
    return [
        BatchEntry(str(slot), _endpoint(f"h{slot}.io"), 7501 + slot)
        for slot in range(count)
    ]


class XrayBatchTest(unittest.TestCase):
    def test_port_bound_to_its_own_config(self):
        """Порт и конфигурация связаны правилом — иначе проверка уйдёт не туда."""
        config = build_batch(_entries(3), Core.XRAY)

        self.assertEqual([i["port"] for i in config["inbounds"]], [7501, 7502, 7503])
        by_tag = {o["tag"]: o for o in config["outbounds"]}
        for rule in config["routing"]["rules"]:
            inbound = next(
                i for i in config["inbounds"] if i["tag"] == rule["inboundTag"][0]
            )
            outbound = by_tag[rule["outboundTag"]]
            slot = inbound["tag"].rsplit("-", 1)[1]
            self.assertEqual(outbound["settings"]["vnext"][0]["address"], f"h{slot}.io")

    def test_every_inbound_has_a_rule(self):
        config = build_batch(_entries(5), Core.XRAY)
        routed = {rule["inboundTag"][0] for rule in config["routing"]["rules"]}
        self.assertEqual(routed, {i["tag"] for i in config["inbounds"]})

    def test_inbounds_stay_on_loopback(self):
        config = build_batch(_entries(4), Core.XRAY)
        self.assertTrue(all(i["listen"] == "127.0.0.1" for i in config["inbounds"]))

    def test_single_config_keeps_plain_tags(self):
        """Одиночный прогон — путь отката, его конфиг остаётся прежним."""
        config = build_config(_endpoint(), Core.XRAY, 7501)
        self.assertEqual(config["inbounds"][0]["tag"], "mon-test-in")
        self.assertEqual(config["outbounds"][0]["tag"], "mon-test-out")


class SingboxBatchTest(unittest.TestCase):
    def _batch(self, count: int = 3):
        entries = [
            BatchEntry(str(slot), parse_link(f"hysteria2://pw@h{slot}.io:443#h{slot}"),
                       7501 + slot)
            for slot in range(count)
        ]
        return build_batch(entries, Core.SINGBOX)

    def test_ports_and_rules_match(self):
        config = self._batch()
        self.assertEqual([i["listen_port"] for i in config["inbounds"]], [7501, 7502, 7503])
        for rule in config["route"]["rules"]:
            self.assertEqual(
                rule["inbound"][0].replace("-in-", "-out-"), rule["outbound"]
            )

    def test_no_default_route_in_batch(self):
        """`final` увёл бы непопавший трафик через чужую конфигурацию."""
        self.assertNotIn("final", self._batch()["route"])

    def test_single_keeps_default_route(self):
        config = build_config(parse_link("hysteria2://pw@h.io:443#x"), Core.SINGBOX, 7501)
        self.assertEqual(config["route"]["final"], config["outbounds"][0]["tag"])


class LogAttributionTest(unittest.TestCase):
    """Общий лог ядра надо делить между проверками, иначе причины перепутаются."""

    LOG = [
        "tcp: dialing TCP to a.io:443 [mon-test-in-1 -> mon-test-out-1]",
        "failed to process outbound traffic: i/o timeout [mon-test-in-2 -> mon-test-out-2]",
        "tcp: dialing TCP to c.io:443 [mon-test-in-11 -> mon-test-out-11]",
        "failed to read config: unknown field",
    ]

    def test_only_own_lines_returned(self):
        self.assertEqual(_lines_for_slot(self.LOG, "2"), [self.LOG[1]])

    def test_slot_prefix_not_confused(self):
        """Слот 1 не должен забирать строки слота 11."""
        self.assertEqual(_lines_for_slot(self.LOG, "1"), [self.LOG[0]])
        self.assertEqual(_lines_for_slot(self.LOG, "11"), [self.LOG[2]])

    def test_untagged_lines_shared(self):
        """Ошибка старта относится ко всей пачке — её видит любая проверка."""
        self.assertEqual(_lines_for_slot(self.LOG, "7"), [self.LOG[3]])

    def test_no_slot_returns_everything(self):
        self.assertEqual(_lines_for_slot(self.LOG, None), self.LOG)

    def test_nothing_to_attribute(self):
        self.assertEqual(_lines_for_slot(["a [mon-test-in-3 -> mon-test-out-3]"], "9"), [])


if __name__ == "__main__":
    unittest.main()
