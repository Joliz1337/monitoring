"""Tests for public addresses behind NAT (services/nat_addresses).

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Ошибка здесь тихая и опасная в обе стороны: не найденный адрес — соседи режут
подключения ноды, принятый на веру чужой адрес — белый список открывает ему все
порты на всех нодах.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.nat_addresses import (  # noqa: E402
    STALE_AFTER_SEC,
    UNREACHABLE_RETRY_SEC,
    NatAddressProbe,
    confirmed_public_ip,
    fetch_echo,
    is_private_ipv4,
    parse_public_ip,
)

SERVICES = (("a.example", "/"), ("b.example", "/"), ("c.example", "/"), ("d.example", "/"))


class ParsePublicIpTest(unittest.TestCase):
    def test_plain_and_quoted_answers(self):
        self.assertEqual(parse_public_ip("89.232.163.73\n"), "89.232.163.73")
        self.assertEqual(parse_public_ip('"89.232.163.73"'), "89.232.163.73")

    def test_private_broken_and_empty_answers_give_nothing(self):
        for body in ("127.0.0.1", "192.168.0.106", "999.1.1.1", "<html>loading</html>", "", None):
            with self.subTest(body=body):
                self.assertIsNone(parse_public_ip(body))


class IsPrivateIpv4Test(unittest.TestCase):
    def test_only_non_global_ipv4_is_private(self):
        self.assertTrue(is_private_ipv4("192.168.0.106"))
        self.assertTrue(is_private_ipv4("10.0.1.15"))
        self.assertTrue(is_private_ipv4("100.64.0.1"))
        self.assertFalse(is_private_ipv4("89.232.163.73"))
        self.assertFalse(is_private_ipv4("2001:db8::1"))
        self.assertFalse(is_private_ipv4("garbage"))


def fake_fetch(answers: dict[str, str]):
    calls: list[tuple[str, str]] = []

    async def fetch(source: str, host: str, path: str):
        calls.append((source, host))
        return answers.get(host)

    return fetch, calls


class ConfirmedPublicIpTest(unittest.IsolatedAsyncioTestCase):
    async def test_two_agreeing_services_are_enough(self):
        fetch, calls = fake_fetch({"a.example": "89.232.163.73", "b.example": "89.232.163.73\n"})

        public = await confirmed_public_ip("192.168.0.106", fetch, SERVICES)

        self.assertEqual(public, "89.232.163.73")
        self.assertEqual(calls, [("192.168.0.106", "a.example"), ("192.168.0.106", "b.example")])

    async def test_one_lying_service_does_not_confirm_its_address(self):
        fetch, _ = fake_fetch({
            "a.example": "6.6.6.6", "b.example": "89.232.163.73", "c.example": "89.232.163.73",
        })

        self.assertEqual(await confirmed_public_ip("192.168.0.106", fetch, SERVICES), "89.232.163.73")

    async def test_single_answer_or_none_is_not_enough(self):
        for answers in ({"c.example": "89.232.163.73"}, {}, {"a.example": "6.6.6.6", "b.example": "7.7.7.7"}):
            with self.subTest(answers=answers):
                fetch, _ = fake_fetch(answers)
                self.assertIsNone(await confirmed_public_ip("192.168.0.106", fetch, SERVICES))


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class NatAddressProbeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.answers: dict[str, str] = {"192.168.0.106": "89.232.163.73"}
        self.resolved: list[str] = []

        async def resolve(source: str):
            self.resolved.append(source)
            return self.answers.get(source)

        self.probe = NatAddressProbe(resolve=resolve, clock=self.clock)

    async def test_only_private_addresses_are_resolved(self):
        await self.probe.probe_once(["192.168.0.106", "5.9.0.10"])

        self.assertEqual(self.resolved, ["192.168.0.106"])
        self.assertEqual(self.probe.public_address("192.168.0.106"), "89.232.163.73")
        self.assertIsNone(self.probe.public_address("5.9.0.10"))

    async def test_address_without_internet_is_retried_rarely(self):
        await self.probe.probe_once(["10.0.0.5"])
        self.clock.now += UNREACHABLE_RETRY_SEC - 1
        await self.probe.probe_once(["10.0.0.5"])
        self.clock.now += 1
        await self.probe.probe_once(["10.0.0.5"])

        self.assertEqual(self.resolved, ["10.0.0.5", "10.0.0.5"])
        self.assertIsNone(self.probe.public_address("10.0.0.5"))

    async def test_one_failed_attempt_keeps_the_address(self):
        await self.probe.probe_once(["192.168.0.106"])
        self.answers.clear()
        self.clock.now += 60
        await self.probe.probe_once(["192.168.0.106"])

        self.assertEqual(self.probe.public_address("192.168.0.106"), "89.232.163.73")

    async def test_unconfirmed_address_expires(self):
        await self.probe.probe_once(["192.168.0.106"])
        self.answers.clear()
        for _ in range(STALE_AFTER_SEC // 60 + 1):
            self.clock.now += 60
            await self.probe.probe_once(["192.168.0.106"])

        self.assertIsNone(self.probe.public_address("192.168.0.106"))

    async def test_changed_public_address_replaces_old_one(self):
        await self.probe.probe_once(["192.168.0.106"])
        self.answers["192.168.0.106"] = "87.242.94.175"
        self.clock.now += 60
        await self.probe.probe_once(["192.168.0.106"])

        self.assertEqual(self.probe.public_address("192.168.0.106"), "87.242.94.175")

    async def test_address_gone_from_interfaces_is_forgotten(self):
        await self.probe.probe_once(["192.168.0.106"])
        await self.probe.probe_once([])

        self.assertIsNone(self.probe.public_address("192.168.0.106"))


class FetchEchoTest(unittest.TestCase):
    def test_connection_error_gives_nothing(self):
        # 192.0.2.1 нет ни на одной карте: bind() падает до выхода в сеть
        with mock.patch("app.services.nat_addresses.REQUEST_TIMEOUT_SEC", 1):
            self.assertIsNone(fetch_echo("192.0.2.1", "localhost", "/"))


if __name__ == "__main__":
    unittest.main()
