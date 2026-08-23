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
)
from app.services.xray_test.parsers import parse_link  # noqa: E402
from app.services.xray_test.probes import ProbeOptions  # noqa: E402

UUID = "11111111-2222-3333-4444-555555555555"


def _cell(link: str = ""):
    return build_matrix([parse_link(link or f"vless://{UUID}@h.io:443?security=tls#node")])[0]


def _ticket(core: Core = Core.XRAY) -> BundleTicket:
    return BundleTicket(
        token="tok", url="https://panel.example/api/xray-test/bundle/tok",
        sha256="a" * 64, version="26.3.27", core=core,
    )


class PayloadTest(unittest.TestCase):
    def _rows(self, **kwargs) -> list[list[str]]:
        options = ProbeOptions(**kwargs) if kwargs else ProbeOptions()
        payload = _build_payload(_cell(), Core.XRAY, _ticket(), options, 7501)
        decoded = base64.b64decode(payload).decode()
        return [line.split("\t") for line in decoded.splitlines()]

    def test_core_row_carries_url_and_digest(self):
        core_row = next(row for row in self._rows() if row[0] == "CORE")
        self.assertEqual(core_row[1], "xray")
        self.assertEqual(core_row[2], "26.3.27")
        self.assertTrue(core_row[3].startswith("https://"))
        self.assertEqual(len(core_row[4]), 64)

    def test_options_row_reflects_flags(self):
        row = next(r for r in self._rows(tcp=False, http=True, exit_identity=False, speed=True)
                   if r[0] == "OPTS")
        self.assertEqual(row[1:6], ["0", "1", "0", "1", "7501"])

    def test_cell_row_contains_valid_config(self):
        row = next(row for row in self._rows() if row[0] == "CELL")
        config = json.loads(base64.b64decode(row[6]).decode())

        self.assertEqual(row[3], "h.io")
        self.assertEqual(row[4], "443")
        self.assertEqual(row[5], "0")  # не UDP-протокол
        self.assertEqual(config["inbounds"][0]["listen"], "127.0.0.1")
        self.assertEqual(config["inbounds"][0]["port"], 7501)

    def test_udp_protocol_flagged(self):
        payload = _build_payload(
            _cell("hysteria2://pw@h.io:443#hy"), Core.SINGBOX, _ticket(Core.SINGBOX),
            ProbeOptions(), 7502,
        )
        row = next(
            line.split("\t") for line in base64.b64decode(payload).decode().splitlines()
            if line.startswith("CELL")
        )
        self.assertEqual(row[5], "1")

    def test_no_tabs_or_newlines_break_rows(self):
        """Каждая строка задания обязана остаться одной строкой."""
        payload = _build_payload(_cell(), Core.XRAY, _ticket(), ProbeOptions(), 7501)
        lines = base64.b64decode(payload).decode().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual([line.split("\t")[0] for line in lines], ["CORE", "OPTS", "CELL"])


class ParseResultTest(unittest.TestCase):
    def test_successful_cell(self):
        events = [{
            "type": "cell", "index": 0, "verdict": "ok", "reason": None, "detail": "",
            "tcp_min_ms": 12, "handshake_ms": 240, "rtt_ms": 95, "http_status": 204,
            "exit_ip": "203.0.113.7", "exit_country": "NL", "speed_mbps": 85.5,
        }]
        result = _parse_result(_cell(), events)

        self.assertIs(result.verdict, Verdict.OK)
        self.assertIsNone(result.reason)
        self.assertEqual(result.exit_ip, "203.0.113.7")
        self.assertEqual(result.exit_country, "NL")
        self.assertEqual(result.timings.rtt_ms, 95)
        self.assertEqual(result.timings.speed_mbps, 85.5)

    def test_failed_cell_keeps_reason(self):
        events = [{
            "type": "cell", "index": 0, "verdict": "fail",
            "reason": "CORE_START_FAILED", "detail": "invalid config",
        }]
        result = _parse_result(_cell(), events)

        self.assertIs(result.verdict, Verdict.FAIL)
        self.assertIs(result.reason, FailReason.CORE_START_FAILED)
        self.assertIn("invalid config", result.detail)

    def test_secrets_from_node_output_masked(self):
        events = [{
            "type": "cell", "index": 0, "verdict": "fail", "reason": "CORE_CRASHED",
            "detail": f'failed to parse id {UUID}',
        }]
        result = _parse_result(_cell(), events)
        self.assertNotIn(UUID, result.detail)

    def test_missing_cell_falls_back_to_log(self):
        events = [{"type": "log", "line": "не удалось получить ядро xray"}]
        result = _parse_result(_cell(), events)

        self.assertIs(result.verdict, Verdict.FAIL)
        self.assertIs(result.reason, FailReason.NODE_ERROR)
        self.assertIn("ядро xray", result.detail)

    def test_empty_output_is_node_error(self):
        result = _parse_result(_cell(), [])
        self.assertIs(result.reason, FailReason.NODE_ERROR)

    def test_last_cell_wins(self):
        events = [
            {"type": "cell", "index": 0, "verdict": "fail", "reason": "TCP_TIMEOUT"},
            {"type": "cell", "index": 0, "verdict": "ok", "reason": None},
        ]
        self.assertIs(_parse_result(_cell(), events).verdict, Verdict.OK)


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
