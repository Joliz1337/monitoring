"""Маршруты поиска IP и ширины канала Cloud.ru (app/routers/ip_search.py).

Голый unittest, без БД и сети. Раньше модуль был необязательным расширением:
его маршруты появлялись, только если удавалось загрузить каждый сервис. Теперь
это обычная часть панели — у каждого провайдера маршруты есть всегда.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app.auth import verify_auth  # noqa: E402
from app.routers import ip_search  # noqa: E402

IP_SEARCH_PROVIDERS = ("/ext", "/yc", "/selectel", "/cloud/evo", "/cloud/adv")
CLOUD_BANDWIDTH = "/cloud/bw"
NGINX_PEER = "172.18.0.4"
RECEIVED_BATCH = {"account_id": 1, "items": []}


def requires_panel_login(dependant) -> bool:
    return any(dep.call is verify_auth or requires_panel_login(dep) for dep in dependant.dependencies)


class RoutesWithoutLoginTest(unittest.TestCase):
    """Без входа в панель доступны только отчёты воркеров, и то лишь изнутри контейнера."""

    def setUp(self) -> None:
        self.open_routes = [
            route for route in ip_search.router.routes if not requires_panel_login(route.dependant)
        ]

    def test_only_worker_reports_skip_the_panel_login(self):
        self.assertTrue(self.open_routes)
        self.assertEqual([route.path for route in self.open_routes if "/internal/" not in route.path], [])

    def test_worker_report_from_outside_the_container_is_refused(self):
        app = FastAPI()
        app.include_router(ip_search.router)
        outsider = TestClient(app)

        for route in self.open_routes:
            # Валидные параметры: отказ должен прийти от проверки источника, а не от валидации
            query = {param.name: "1" for param in route.dependant.query_params}
            body = RECEIVED_BATCH if route.dependant.body_params else None
            response = outsider.post(route.path, params=query, json=body)
            self.assertEqual(response.status_code, 403, f"{route.path}: {response.text}")


class ProviderRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.paths = {route.path for route in ip_search.router.routes}

    def test_every_provider_exposes_its_accounts(self):
        for prefix in (*IP_SEARCH_PROVIDERS, CLOUD_BANDWIDTH):
            self.assertIn(f"{prefix}/accounts", self.paths)

    def test_every_search_provider_accepts_worker_reports(self):
        # По этим путям воркеры докладывают панели выданные и пойманные адреса
        for prefix in IP_SEARCH_PROVIDERS:
            self.assertIn(f"{prefix}/internal/caught-ip", self.paths)
            self.assertIn(f"{prefix}/internal/received-ips", self.paths)


def make_request(peer: str | None, **headers: str) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/_int/ext/internal/caught-ip",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    if peer is not None:
        scope["client"] = (peer, 51234)
    return Request(scope)


class InternalRequestTest(unittest.TestCase):
    """Служебные эндпоинты зовут только воркеры из самого контейнера бэкенда."""

    def test_worker_over_ipv4_loopback_accepted(self):
        self.assertTrue(ip_search._is_internal_request(make_request("127.0.0.1")))

    def test_worker_over_ipv6_loopback_accepted(self):
        self.assertTrue(ip_search._is_internal_request(make_request("::1")))

    def test_public_172_address_rejected(self):
        self.assertFalse(ip_search._is_internal_request(make_request("172.104.5.6")))

    def test_public_172_32_address_rejected(self):
        self.assertFalse(ip_search._is_internal_request(make_request("172.32.0.1")))

    def test_public_10_lookalike_not_prefix_matched(self):
        self.assertFalse(ip_search._is_internal_request(make_request("100.64.0.1")))

    def test_request_via_nginx_with_public_client_rejected(self):
        request = make_request(NGINX_PEER, **{"X-Real-IP": "203.0.113.7", "X-Forwarded-For": "203.0.113.7"})
        self.assertFalse(ip_search._is_internal_request(request))

    def test_spoofed_loopback_via_proxy_headers_rejected(self):
        request = make_request("127.0.0.1", **{"X-Forwarded-For": "203.0.113.7"})
        self.assertFalse(ip_search._is_internal_request(request))

    def test_docker_peer_without_proxy_headers_rejected(self):
        self.assertFalse(ip_search._is_internal_request(make_request(NGINX_PEER)))

    def test_missing_client_rejected(self):
        self.assertFalse(ip_search._is_internal_request(make_request(None)))


if __name__ == "__main__":
    unittest.main()
