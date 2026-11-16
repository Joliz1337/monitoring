"""Tests for PUT /api/haproxy/runtime/quarantine and the quarantine mark in stats.

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Маршрут — граница с панелью: по его ответу она решает, отключён адрес или нет.
Ошибка сокета, выданная за «адреса нет», закрыла бы запись панели, а сервер
остался бы в обслуживании.
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app.capabilities import parse_capabilities  # noqa: E402
from app.models.haproxy import QuarantineRequest  # noqa: E402
from app.routers import haproxy as haproxy_router  # noqa: E402
from app.services.haproxy_manager import StatsSocketUnavailableError  # noqa: E402
from app.services.haproxy_quarantine import AppliedRow, SkippedBackend, SkipReason, TargetResult  # noqa: E402
from app.services.haproxy_runtime_weights import SocketPermissionError  # noqa: E402

PATH = "/api/haproxy/runtime/quarantine"


def body(ip="10.0.0.5", port=443, ttl=180, share=50, count=1, **extra) -> dict:
    return {"targets": [{"ip": ip, "port": port}] * count, "ttl_seconds": ttl, "max_share_pct": share, **extra}


class RequestModelTests(unittest.TestCase):
    def test_valid(self):
        request = QuarantineRequest(**body(ip="62.50.146.225", port=8443, drop_sessions=False))
        self.assertEqual((request.targets[0].ip, request.targets[0].port), ("62.50.146.225", 8443))
        self.assertFalse(request.drop_sessions)

    def test_sessions_are_dropped_unless_told_otherwise(self):
        self.assertTrue(QuarantineRequest(**body()).drop_sessions)

    def test_empty_list_is_a_valid_release_of_everything(self):
        self.assertEqual(QuarantineRequest(**body(count=0)).targets, [])

    def test_limits_accepted(self):
        QuarantineRequest(**body(ttl=10, share=0, count=256))
        QuarantineRequest(**body(ttl=3600, share=100, ip="2001:db8::1"))

    def test_rejects(self):
        for bad in (body(port=0), body(port=65536), body(ttl=5), body(ttl=3601), body(share=101), body(share=-1),
                    body(count=257), body(ip="example.com"), body(ip="0.0.0.0"), body(ip="224.0.0.1")):
            with self.subTest(payload=bad), self.assertRaises(ValidationError):
                QuarantineRequest(**bad)

    def test_port_is_required(self):
        with self.assertRaises(ValidationError):
            QuarantineRequest(targets=[{"ip": "10.0.0.5"}], ttl_seconds=180, max_share_pct=50)

    def test_address_is_not_dead_unless_the_panel_says_so(self):
        # Панель старой версии пометки не шлёт — для неё предел доли действует на все адреса
        self.assertFalse(QuarantineRequest(**body()).targets[0].dead)


class RouteTests(unittest.TestCase):
    def call(self, quarantine: mock.Mock, **overrides):
        request = QuarantineRequest(**body(**overrides))
        with mock.patch.object(haproxy_router, "get_quarantine", return_value=quarantine):
            return asyncio.run(haproxy_router.set_quarantine(request))

    def test_passes_the_list_and_returns_what_was_done(self):
        result = TargetResult(
            "10.0.0.5", 443, True,
            applied=[AppliedRow("backend_tcp_lb", "srv1", 7)],
            skipped=[SkippedBackend("backend_tcp_other", SkipReason.LAST_SERVER)],
        )
        quarantine = mock.Mock(apply=mock.Mock(return_value=[result]))
        response = self.call(quarantine, drop_sessions=False)

        quarantine.apply.assert_called_once_with([("10.0.0.5", 443)], 180, 50, False, frozenset())
        self.assertEqual(response.model_dump(), {"results": [{
            "ip": "10.0.0.5", "port": 443, "found": True,
            "applied": [{"backend": "backend_tcp_lb", "server": "srv1", "sessions": 7}],
            "skipped": [{"backend": "backend_tcp_other", "reason": "last_server"}],
        }]})

    def test_dead_marks_reach_the_manager(self):
        request = QuarantineRequest(
            targets=[{"ip": "10.0.0.5", "port": 443, "dead": True}, {"ip": "10.0.0.6", "port": 443}],
            ttl_seconds=180, max_share_pct=50,
        )
        quarantine = mock.Mock(apply=mock.Mock(return_value=[]))
        with mock.patch.object(haproxy_router, "get_quarantine", return_value=quarantine):
            asyncio.run(haproxy_router.set_quarantine(request))

        quarantine.apply.assert_called_once_with(
            [("10.0.0.5", 443), ("10.0.0.6", 443)], 180, 50, True, frozenset({("10.0.0.5", 443)}),
        )

    def test_missing_socket_is_503_not_an_empty_answer(self):
        for error in (StatsSocketUnavailableError("no socket"), OSError("socket is gone"), TimeoutError()):
            with self.subTest(error=error), self.assertRaises(HTTPException) as caught:
                self.call(mock.Mock(apply=mock.Mock(side_effect=error)))
            self.assertEqual(caught.exception.status_code, 503)

    def test_socket_without_admin_level_is_409(self):
        quarantine = mock.Mock(apply=mock.Mock(side_effect=SocketPermissionError("Permission denied")))
        with self.assertRaises(HTTPException) as caught:
            self.call(quarantine)
        self.assertEqual(caught.exception.status_code, 409)

    def test_needs_write_access_to_haproxy(self):
        self.assertIsNone(parse_capabilities("").check("PUT", PATH))
        self.assertIsNone(parse_capabilities("haproxy").check("PUT", PATH))
        self.assertIsNotNone(parse_capabilities("haproxy:ro").check("PUT", PATH))
        self.assertIsNotNone(parse_capabilities("system").check("PUT", PATH))


STATS_DICT = {
    "available": True,
    "proxies": [{
        "name": "backend_tcp_lb",
        "backend": {"name": "BACKEND", "kind": "backend", "status": "UP"},
        "servers": [
            {"name": "srv1", "kind": "server", "status": "MAINT", "addr": "10.0.0.5:443"},
            {"name": "srv2", "kind": "server", "status": "MAINT", "addr": "10.0.0.6:443"},
        ],
    }],
}


class StatsMarkTests(unittest.TestCase):
    def test_only_rows_disabled_by_the_panel_are_marked(self):
        manager = mock.Mock(get_stats=mock.Mock(return_value=STATS_DICT))
        probe = mock.Mock(annotate_haproxy=lambda stats: stats)
        quarantine = mock.Mock(quarantined_rows=mock.Mock(return_value=frozenset({("backend_tcp_lb", "srv1")})))
        with mock.patch.object(haproxy_router, "get_haproxy_manager", return_value=manager),              mock.patch.object(haproxy_router, "get_loss_probe", return_value=probe),              mock.patch.object(haproxy_router, "get_quarantine", return_value=quarantine):
            response = asyncio.run(haproxy_router.get_haproxy_stats())

        ours, manual = response.proxies[0].servers
        # srv2 в обслуживании по чужой воле (disabled в конфиге) — метки нет
        self.assertEqual((ours.quarantined, manual.quarantined), (True, False))
        self.assertNotIn("quarantined", STATS_DICT["proxies"][0]["servers"][0])


if __name__ == "__main__":
    unittest.main()
