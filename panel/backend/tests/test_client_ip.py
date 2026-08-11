"""Тесты определения IP клиента за nginx панели.

Свойство, ради которого тест существует: заголовкам верим только от внутреннего
пира, и подделать свой IP клиент не может. Ошибка в любую сторону дорогая —
слишком доверчиво — любой обходит бан и банит чужих, слишком строго — все
клиенты схлопываются в адрес контейнера nginx и пять неверных паролей закрывают
вход всем сразу.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from starlette.requests import Request  # noqa: E402

from app.security import get_client_ip  # noqa: E402

NGINX_PEER = "172.18.0.4"
CLIENT = "203.0.113.7"
SPOOFED = "198.51.100.99"


def make_request(peer: str | None, **headers: str) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/login",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    if peer is not None:
        scope["client"] = (peer, 51234)
    return Request(scope)


class ClientIpTests(unittest.TestCase):
    def test_real_ip_from_internal_proxy(self):
        request = make_request(NGINX_PEER, **{"X-Real-IP": CLIENT})
        self.assertEqual(get_client_ip(request), CLIENT)

    def test_real_ip_wins_over_forwarded_chain(self):
        """X-Real-IP nginx ставит из $remote_addr — он и приоритетный источник."""
        request = make_request(
            NGINX_PEER,
            **{"X-Real-IP": CLIENT, "X-Forwarded-For": f"{SPOOFED}, {CLIENT}"},
        )
        self.assertEqual(get_client_ip(request), CLIENT)

    def test_forwarded_chain_takes_last_hop(self):
        """Первый элемент XFF прислал сам клиент, последний дописал наш nginx."""
        request = make_request(NGINX_PEER, **{"X-Forwarded-For": f"{SPOOFED}, {CLIENT}"})
        self.assertEqual(get_client_ip(request), CLIENT)

    def test_headers_ignored_from_external_peer(self):
        """Прямое подключение снаружи: заголовки клиента — не источник истины."""
        request = make_request(
            CLIENT,
            **{"X-Real-IP": SPOOFED, "X-Forwarded-For": SPOOFED},
        )
        self.assertEqual(get_client_ip(request), CLIENT)

    def test_loopback_peer_is_trusted(self):
        request = make_request("127.0.0.1", **{"X-Real-IP": CLIENT})
        self.assertEqual(get_client_ip(request), CLIENT)

    def test_ipv4_mapped_peer_is_trusted(self):
        request = make_request("::ffff:172.18.0.4", **{"X-Real-IP": CLIENT})
        self.assertEqual(get_client_ip(request), CLIENT)

    def test_client_ranges_are_not_trusted_proxies(self):
        """CGNAT и документационные диапазоны — адреса клиентов, а не нашей инфраструктуры
        (ipaddress.is_private считает их приватными, поэтому список сетей задан явно)."""
        for peer in ("100.64.12.7", "192.0.2.15", "198.18.5.1"):
            with self.subTest(peer=peer):
                request = make_request(peer, **{"X-Real-IP": SPOOFED})
                self.assertEqual(get_client_ip(request), peer)

    def test_garbage_headers_fall_back_to_peer(self):
        """Мусор в заголовке не должен попадать в ключи бана и в логи."""
        request = make_request(
            NGINX_PEER,
            **{"X-Real-IP": "not-an-ip", "X-Forwarded-For": "<script>, unknown"},
        )
        self.assertEqual(get_client_ip(request), NGINX_PEER)

    def test_no_headers_falls_back_to_peer(self):
        self.assertEqual(get_client_ip(make_request(NGINX_PEER)), NGINX_PEER)

    def test_missing_client(self):
        self.assertEqual(get_client_ip(make_request(None)), "unknown")


if __name__ == "__main__":
    unittest.main()
