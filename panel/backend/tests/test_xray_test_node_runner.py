"""Тесты обмена с исполнителем проверок на ноде.

Голый unittest, без сети и БД.

Формат задания и ответа — контракт между панелью и `configs/xray-test-runner.sh`.
Разъехаться он может молча: панель просто перестанет понимать результаты и
покажет «ошибка ноды» на рабочих ключах.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.xray_test.bundle import BundleTicket  # noqa: E402
from app.services.xray_test.matrix import build_matrix  # noqa: E402
from app.services.xray_test.models import Core, FailReason, Verdict  # noqa: E402
from app.services.xray_test.node_runner import (  # noqa: E402
    _build_payload,
    _cores_for,
    _parse_result,
    _parse_results,
)
from app.services.xray_test.parsers import parse_link  # noqa: E402
from app.services.xray_test.probes import ProbeOptions  # noqa: E402

UUID = "11111111-2222-3333-4444-555555555555"
TAB = "\t"


def _cell(link: str = ""):
    return build_matrix([parse_link(link or f"vless://{UUID}@h.io:443?security=tls#node")])[0]


def _cells(*links):
    return build_matrix([parse_link(link) for link in links])


def _ticket(core: Core = Core.XRAY) -> BundleTicket:
    return BundleTicket(
        token="tok", url="https://panel.example/api/xray-test/bundle/tok",
        sha256="a" * 64, version="26.3.27", core=core,
    )


class PayloadTest(unittest.TestCase):
    def _rows(self, cells=None, ports=None, core=Core.XRAY, **kwargs) -> list[list[str]]:
        options = ProbeOptions(**kwargs) if kwargs else ProbeOptions()
        payload = _build_payload(
            cells or [_cell()], ports or [7501], core, _ticket(core), options
        )
        decoded = base64.b64decode(payload).decode()
        return [line.split(TAB) for line in decoded.splitlines()]

    @staticmethod
    def _config(rows: list[list[str]]) -> dict:
        return json.loads(base64.b64decode(
            next(row for row in rows if row[0] == "CONF")[2]
        ).decode())

    def test_core_row_carries_url_and_digest(self):
        core_row = next(row for row in self._rows() if row[0] == "CORE")
        self.assertEqual(core_row[1], "xray")
        self.assertEqual(core_row[2], "26.3.27")
        self.assertTrue(core_row[3].startswith("https://"))
        self.assertEqual(len(core_row[4]), 64)

    def test_options_row_reflects_flags(self):
        row = next(r for r in self._rows(tcp=False, http=True, exit_identity=False, speed=True)
                   if r[0] == "OPTS")
        self.assertEqual(row[1:5], ["0", "1", "0", "1"])

    def test_single_config_for_whole_batch(self):
        """Один конфиг на пачку — ядро на ноде поднимается ровно один раз."""
        cells = _cells(
            f"vless://{UUID}@a.io:443?security=tls#one",
            f"vless://{UUID}@b.io:8443?security=tls#two",
        )
        rows = self._rows(cells=cells, ports=[7501, 7502])

        self.assertEqual(len([row for row in rows if row[0] == "CONF"]), 1)
        config = self._config(rows)
        self.assertEqual([i["port"] for i in config["inbounds"]], [7501, 7502])
        self.assertEqual(len(config["outbounds"]), 2)
        self.assertTrue(all(i["listen"] == "127.0.0.1" for i in config["inbounds"]))

    def test_tags_carry_cell_index(self):
        """Слот в теге — номер ячейки: по нему исполнитель делит общий лог ядра."""
        cells = _cells(
            f"vless://{UUID}@a.io:443?security=tls#one",
            f"vless://{UUID}@b.io:443?security=tls#two",
        )
        config = self._config(self._rows(cells=cells, ports=[7501, 7502]))

        self.assertEqual(
            [i["tag"] for i in config["inbounds"]],
            [f"mon-test-in-{cells[0].index}", f"mon-test-in-{cells[1].index}"],
        )
        for rule in config["routing"]["rules"]:
            self.assertEqual(
                rule["inboundTag"][0].replace("-in-", "-out-"), rule["outboundTag"]
            )

    def test_cell_row_carries_socks_port(self):
        row = next(row for row in self._rows() if row[0] == "CELL")
        self.assertEqual(row[3], "h.io")
        self.assertEqual(row[4], "443")
        self.assertEqual(row[5], "0")  # не UDP-протокол
        self.assertEqual(row[6], "7501")

    def test_udp_protocol_flagged(self):
        rows = self._rows(cells=[_cell("hysteria2://pw@h.io:443#hy")],
                          ports=[7502], core=Core.SINGBOX)
        row = next(r for r in rows if r[0] == "CELL")
        self.assertEqual(row[5], "1")

    def test_row_per_cell(self):
        cells = _cells(
            f"vless://{UUID}@a.io:443?security=tls#one",
            f"vless://{UUID}@b.io:443?security=tls#two",
            f"vless://{UUID}@c.io:443?security=tls#three",
        )
        rows = self._rows(cells=cells, ports=[7501, 7502, 7503])
        self.assertEqual(
            [row[0] for row in rows],
            ["CORE", "OPTS", "CONF", "CELL", "CELL", "CELL"],
        )

    def test_no_tabs_or_newlines_break_rows(self):
        """Каждая строка задания обязана остаться одной строкой."""
        rows = self._rows()
        self.assertEqual([row[0] for row in rows], ["CORE", "OPTS", "CONF", "CELL"])


class ParseResultTest(unittest.TestCase):
    def test_successful_cell(self):
        result = _parse_result(_cell(), {
            "type": "cell", "index": 0, "verdict": "ok", "reason": None, "detail": "",
            "tcp_min_ms": 12, "handshake_ms": 240, "rtt_ms": 95, "http_status": 204,
            "exit_ip": "203.0.113.7", "exit_country": "NL", "speed_mbps": 85.5,
        })

        self.assertIs(result.verdict, Verdict.OK)
        self.assertIsNone(result.reason)
        self.assertEqual(result.exit_ip, "203.0.113.7")
        self.assertEqual(result.exit_country, "NL")
        self.assertEqual(result.timings.rtt_ms, 95)
        self.assertEqual(result.timings.speed_mbps, 85.5)

    def test_failed_cell_keeps_reason(self):
        result = _parse_result(_cell(), {
            "type": "cell", "index": 0, "verdict": "fail",
            "reason": "CORE_START_FAILED", "detail": "invalid config",
        })

        self.assertIs(result.verdict, Verdict.FAIL)
        self.assertIs(result.reason, FailReason.CORE_START_FAILED)
        self.assertIn("invalid config", result.detail)

    def test_secrets_from_node_output_masked(self):
        result = _parse_result(_cell(), {
            "type": "cell", "index": 0, "verdict": "fail", "reason": "CORE_CRASHED",
            "detail": f"failed to parse id {UUID}",
        })
        self.assertNotIn(UUID, result.detail)

    def test_node_fields_fill_details(self):
        """Резолв и усреднённый TCP приходят с ноды — иначе в строке прочерки."""
        result = _parse_result(_cell(), {
            "type": "cell", "index": 0, "verdict": "ok", "reason": None,
            "resolved_ip": "203.0.113.5", "dns_ms": 12,
            "tcp_min_ms": 30, "tcp_avg_ms": 34, "tcp_jitter_ms": 5,
        })

        self.assertEqual(result.resolved_ip, "203.0.113.5")
        self.assertEqual(result.timings.dns_ms, 12)
        self.assertEqual(result.timings.tcp_avg_ms, 34)
        self.assertEqual(result.timings.tcp_jitter_ms, 5)

    def test_core_log_reduced_to_reason(self):
        """С ноды прилетает хвост лога — показывать нужно суть, а не простыню."""
        result = _parse_result(_cell(), {
            "type": "cell", "index": 0, "verdict": "fail",
            "reason": "PROXY_HANDSHAKE_FAILED",
            "detail": (
                "app/proxyman/outbound: failed to process outbound traffic > "
                "common/retry: [dial tcp 1.2.3.4:443: i/o timeout] > "
                "common/retry: all retry attempts failed"
            ),
        })

        self.assertIn("i/o timeout", result.detail)
        self.assertNotIn("app/proxyman", result.detail)
        self.assertEqual(result.hint, "IO_TIMEOUT")


class ParseBatchTest(unittest.TestCase):
    """Ответы пачки сопоставляются по номеру ячейки, а не по порядку.

    Исполнитель гонит проверки параллельно, поэтому строки приходят вперемешку.
    """

    def setUp(self):
        self.cells = _cells(
            f"vless://{UUID}@a.io:443?security=tls#one",
            f"vless://{UUID}@b.io:443?security=tls#two",
        )

    def test_results_matched_by_index(self):
        first, second = self.cells
        results = _parse_results(self.cells, [
            {"type": "cell", "index": second.index, "verdict": "ok", "reason": None},
            {"type": "cell", "index": first.index, "verdict": "fail", "reason": "TCP_TIMEOUT"},
        ])

        self.assertIs(results[first.index].verdict, Verdict.FAIL)
        self.assertIs(results[second.index].verdict, Verdict.OK)

    def test_missing_cell_is_node_error(self):
        first, second = self.cells
        results = _parse_results(self.cells, [
            {"type": "cell", "index": first.index, "verdict": "ok", "reason": None},
            {"type": "log", "line": "не удалось получить ядро xray"},
        ])

        self.assertIs(results[first.index].verdict, Verdict.OK)
        self.assertIs(results[second.index].reason, FailReason.NODE_ERROR)
        self.assertIn("ядро xray", results[second.index].detail)

    def test_empty_output_fails_every_cell(self):
        results = _parse_results(self.cells, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.reason is FailReason.NODE_ERROR for r in results.values()))

    def test_last_answer_wins(self):
        first, _ = self.cells
        results = _parse_results(self.cells, [
            {"type": "cell", "index": first.index, "verdict": "fail", "reason": "TCP_TIMEOUT"},
            {"type": "cell", "index": first.index, "verdict": "ok", "reason": None},
        ])
        self.assertIs(results[first.index].verdict, Verdict.OK)


class CoresForTest(unittest.TestCase):
    def test_mixed_matrix_requires_both_cores(self):
        cells = build_matrix([
            parse_link(f"vless://{UUID}@h.io:443?security=tls#a"),
            parse_link("hysteria2://pw@h.io:443#b"),
        ])
        self.assertEqual(_cores_for(cells), {Core.XRAY, Core.SINGBOX})

    def test_unsupported_config_skipped(self):
        cells = build_matrix([
            parse_link(f"vless://{UUID}@h.io:443?type=kcp&seed=x&headerType=srtp#bad"),
            parse_link(f"vless://{UUID}@h.io:443?security=tls#good"),
        ])
        self.assertEqual(_cores_for(cells), {Core.XRAY})


if __name__ == "__main__":
    unittest.main()
