"""Транзакции доп. IP: гейт версии ноды, адрес и порт ноды из URL, дедлайн с
запасом на расхождение часов, вычитание уже стоящих адресов, отбор адресов
хостера для удаления и снятых — для возврата, снимок задачи, отказ старой ноде
в адресах на опущенной карте, повтор apply, пока соединение с нодой не открывается.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.services.network_addresses import AddressSpec
    from app.services.network_transactions import (
        APPLY_CONNECT_ATTEMPTS,
        DEADLINE_GRACE_SECONDS,
        MIN_NODE_VERSION_NETWORK,
        MIN_NODE_VERSION_NETWORK_GATEWAY,
        MIN_NODE_VERSION_NETWORK_HOSTER_REMOVAL,
        MIN_NODE_VERSION_NETWORK_LINK_UP,
        ROLLBACK_TIMEOUT_SEC,
        JobPhase,
        LinkUpUnsupportedError,
        NetworkJob,
        NodeConnectError,
        NodeUnreachableError,
        TransactionStatus,
        deadline_passed,
        gateway_conflicts,
        hoster_on_interface,
        missing_on_interface,
        node_api_port,
        node_host,
        node_supports_hoster_removal,
        node_supports_link_up,
        node_supports_network,
        node_supports_network_gateway,
        parse_deadline,
        present_on_interface,
        start_apply,
        suppressed_on_interface,
        _request,
        _run_job,
    )
except ImportError as e:  # pragma: no cover
    raise unittest.SkipTest(f"network_transactions requires the panel runtime: {e}")


class VersionGateTests(unittest.TestCase):
    def test_gate(self):
        self.assertTrue(node_supports_network(MIN_NODE_VERSION_NETWORK))
        self.assertTrue(node_supports_network("10.30.1"))
        self.assertFalse(node_supports_network("10.28.9"))
        self.assertFalse(node_supports_network(None))
        self.assertFalse(node_supports_network(""))


class UrlTests(unittest.TestCase):
    def test_host_and_port(self):
        self.assertEqual(node_host("https://1.2.3.4:9100"), "1.2.3.4")
        self.assertEqual(node_api_port("https://1.2.3.4:9100"), 9100)
        self.assertEqual(node_host("https://node.example.com"), "node.example.com")
        self.assertEqual(node_api_port("https://node.example.com"), 443)
        self.assertEqual(node_host("http://[2001:db8::1]:9100"), "2001:db8::1")
        self.assertEqual(node_api_port("http://node.example.com"), 80)
        self.assertIsNone(node_host(""))


class DeadlineTests(unittest.TestCase):
    NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)

    def test_parse_forms(self):
        self.assertEqual(parse_deadline("2026-09-02T12:02:00Z", self.NOW), self.NOW + timedelta(minutes=2))
        self.assertEqual(parse_deadline("2026-09-02T12:02:00", self.NOW), self.NOW + timedelta(minutes=2))
        self.assertEqual(parse_deadline("2026-09-02T15:02:00+03:00", self.NOW), self.NOW + timedelta(minutes=2))
        self.assertEqual(parse_deadline(None, self.NOW), self.NOW + timedelta(seconds=ROLLBACK_TIMEOUT_SEC))
        self.assertEqual(parse_deadline("garbage", self.NOW), self.NOW + timedelta(seconds=ROLLBACK_TIMEOUT_SEC))

    def test_passed_with_grace(self):
        deadline = self.NOW
        self.assertFalse(deadline_passed(deadline, self.NOW + timedelta(seconds=DEADLINE_GRACE_SECONDS - 1)))
        self.assertTrue(deadline_passed(deadline, self.NOW + timedelta(seconds=DEADLINE_GRACE_SECONDS + 1)))
        self.assertFalse(deadline_passed(None, self.NOW))


class InterfaceFilterTests(unittest.TestCase):
    IFACE = {"name": "eth0", "addresses": [
        {"address": "1.2.3.4", "prefix": 24, "managed": False},
        {"address": "1.2.3.5", "prefix": 32, "managed": True},
    ]}

    def test_missing_present_and_hoster(self):
        specs = [AddressSpec("1.2.3.4", 24), AddressSpec("1.2.3.5", 32), AddressSpec("1.2.3.6", 32)]
        self.assertEqual([s.cidr for s in missing_on_interface(specs, self.IFACE)], ["1.2.3.6/32"])
        self.assertEqual([s.cidr for s in present_on_interface(specs, self.IFACE)], ["1.2.3.4/24", "1.2.3.5/32"])
        self.assertEqual([s.cidr for s in hoster_on_interface(specs, self.IFACE)], ["1.2.3.4/24"])
        self.assertEqual(missing_on_interface(specs, {"name": "eth0"}), specs)

    def test_restore_only_what_the_node_suppressed_on_this_interface(self):
        state = {"suppressed": [{"interface": "eth0", "address": "1.2.3.7", "prefix": 32},
                                {"interface": "eth1", "address": "1.2.3.8", "prefix": 32}]}
        specs = [AddressSpec("1.2.3.7", 32), AddressSpec("1.2.3.8", 32), AddressSpec("1.2.3.7", 24)]
        self.assertEqual([s.cidr for s in suppressed_on_interface(specs, state, "eth0")], ["1.2.3.7/32"])
        self.assertEqual(suppressed_on_interface(specs, {}, "eth0"), [])

    def test_hoster_removal_gate(self):
        self.assertTrue(node_supports_hoster_removal(MIN_NODE_VERSION_NETWORK_HOSTER_REMOVAL))
        self.assertFalse(node_supports_hoster_removal("10.30.9"))
        self.assertFalse(node_supports_hoster_removal(None))


class LinkUpGateTests(unittest.TestCase):
    STATE = {"interfaces": [{"name": "eth0", "is_up": True, "addresses": []},
                            {"name": "ens4", "is_up": False, "addresses": []}]}

    def test_gate(self):
        self.assertTrue(node_supports_link_up(MIN_NODE_VERSION_NETWORK_LINK_UP))
        self.assertFalse(node_supports_link_up("10.31.9"))
        self.assertFalse(node_supports_link_up(None))

    def test_old_node_gets_no_addresses_on_a_down_card(self):
        server = SimpleNamespace(id=9001, node_version="10.31.0", url="https://1.2.3.4:9100")
        with patch("app.services.network_transactions.fetch_state", AsyncMock(return_value=self.STATE)):
            with self.assertRaises(LinkUpUnsupportedError):
                asyncio.run(start_apply(server, interface="ens4", add=[AddressSpec("5.6.7.8", 32)],
                                        remove=[], restore=[]))


def failing_client(error: Exception) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class ConnectRetryTests(unittest.TestCase):
    SERVER = SimpleNamespace(id=9002, node_version="10.32.0", url="https://1.2.3.4:9100")
    MODULE = "app.services.network_transactions"

    def job(self) -> NetworkJob:
        return NetworkJob(id="9002-1", server_id=9002, interface="ens4",
                          add=[AddressSpec("5.6.7.8", 32)], remove=[], started_at=0.0)

    def request_error(self, error: Exception) -> Exception:
        async def call():
            async with failing_client(error) as client:
                await _request(client, "POST", "https://1.2.3.4:9100/x", headers={}, timeout=1.0)
        with self.assertRaises(NodeUnreachableError) as ctx:
            asyncio.run(call())
        return ctx.exception

    def test_only_unopened_connections_count_as_not_sent(self):
        for error in (httpx.ConnectError("refused"), httpx.ConnectTimeout("syn lost"), httpx.PoolTimeout("busy")):
            self.assertIsInstance(self.request_error(error), NodeConnectError, type(error).__name__)
        # Запрос мог дойти: повторять нельзя, решает цикл подтверждения
        for error in (httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("dropped")):
            self.assertNotIsInstance(self.request_error(error), NodeConnectError, type(error).__name__)

    def run_job(self, send_apply: AsyncMock, fetch_state: AsyncMock) -> NetworkJob:
        job = self.job()
        with (
            patch(f"{self.MODULE}.send_apply", send_apply),
            patch(f"{self.MODULE}.fetch_state", fetch_state),
            patch(f"{self.MODULE}.send_confirm", AsyncMock(return_value={"status": "confirmed"})),
            patch(f"{self.MODULE}.APPLY_CONNECT_RETRY_DELAY_SEC", 0),
            patch(f"{self.MODULE}._reachability", AsyncMock(return_value=None)),
        ):
            asyncio.run(_run_job(self.SERVER, job, {}))
        return job

    def test_apply_is_retried_until_the_connection_opens(self):
        send_apply = AsyncMock(side_effect=[NodeConnectError("syn lost"), NodeConnectError("syn lost"),
                                            {"success": True, "transaction_id": "tx1", "deadline_at": None}])
        state = {"transaction": {"id": "tx1", "status": "pending"}}
        job = self.run_job(send_apply, AsyncMock(return_value=state))
        self.assertEqual(send_apply.await_count, 3)
        self.assertEqual(job.status.value, "confirmed")
        # Счётчик попыток после отправки считает уже пробы связи
        self.assertEqual((job.attempts, job.last_error), (0, None))

    def test_gives_up_without_waiting_for_a_transaction_that_was_never_sent(self):
        send_apply = AsyncMock(side_effect=NodeConnectError("syn lost"))
        fetch_state = AsyncMock()
        job = self.run_job(send_apply, fetch_state)
        self.assertEqual(send_apply.await_count, APPLY_CONNECT_ATTEMPTS)
        self.assertEqual(job.status.value, "failed")
        self.assertIn("не удалось подключиться к ноде", job.message)
        fetch_state.assert_not_awaited()

    def test_lost_response_is_not_resent(self):
        send_apply = AsyncMock(side_effect=NodeUnreachableError("read timeout", timeout=True))
        state = {"transaction": {"id": "tx1", "status": "pending"}}
        job = self.run_job(send_apply, AsyncMock(return_value=state))
        self.assertEqual(send_apply.await_count, 1)
        self.assertEqual(job.status.value, "confirmed")


class GatewayTests(unittest.TestCase):
    IFACE = {"name": "eth0", "addresses": [
        {"address": "1.2.3.4", "prefix": 24, "managed": False},
        {"address": "5.6.7.8", "prefix": 32, "managed": True, "gateway": "5.6.7.1"},
        {"address": "1.2.3.9", "prefix": 32, "managed": True},
    ]}
    DEFAULTS = {"ipv4": "1.2.3.1"}

    def conflicts(self, *specs: AddressSpec) -> list[str]:
        return gateway_conflicts(list(specs), self.IFACE, self.DEFAULTS)

    def test_same_gateway_or_new_address_is_fine(self):
        self.assertEqual(self.conflicts(AddressSpec("5.6.7.8", 32, "5.6.7.1"), AddressSpec("9.9.9.9", 32, "9.9.9.1")), [])
        # Шлюз основного адреса — то же самое, что без шлюза
        self.assertEqual(self.conflicts(AddressSpec("1.2.3.9", 32, "1.2.3.1"), AddressSpec("1.2.3.4", 24)), [])

    def test_changing_gateway_of_a_present_address_is_refused(self):
        problems = self.conflicts(AddressSpec("5.6.7.8", 32, "5.6.7.9"), AddressSpec("5.6.7.8", 32),
                                  AddressSpec("1.2.3.9", 32, "1.2.3.254"))
        self.assertEqual(len(problems), 3)
        self.assertIn("через шлюз 5.6.7.1", problems[0])
        self.assertIn("без своего шлюза", problems[2])

    def test_hoster_address_cannot_get_a_gateway(self):
        problems = self.conflicts(AddressSpec("1.2.3.4", 24, "1.2.3.254"))
        self.assertEqual(len(problems), 1)
        self.assertIn("не панелью", problems[0])

    def test_version_gate_and_payload(self):
        self.assertTrue(node_supports_network_gateway(MIN_NODE_VERSION_NETWORK_GATEWAY))
        self.assertFalse(node_supports_network_gateway("10.30.0"))
        self.assertFalse(node_supports_network_gateway(None))
        self.assertEqual(AddressSpec("5.6.7.8", 32, "5.6.7.1").payload(),
                         {"address": "5.6.7.8", "prefix": 32, "gateway": "5.6.7.1"})
        self.assertEqual(AddressSpec("5.6.7.8", 32).payload(), {"address": "5.6.7.8", "prefix": 32})


class SnapshotTests(unittest.TestCase):
    def test_snapshot_is_serialisable(self):
        job = NetworkJob(id="1-1", server_id=1, interface="eth0", add=[AddressSpec("1.2.3.6", 32)], remove=[],
                         restore=[AddressSpec("1.2.3.7", 32)], started_at=1788000000.0)
        job.deadline_at = datetime(2026, 9, 2, 12, 2, tzinfo=timezone.utc)
        snapshot = job.snapshot()
        self.assertEqual(snapshot["phase"], JobPhase.APPLYING.value)
        self.assertEqual(snapshot["status"], TransactionStatus.PENDING.value)
        self.assertEqual(snapshot["added"], [{"address": "1.2.3.6", "prefix": 32}])
        self.assertEqual(snapshot["restored"], [{"address": "1.2.3.7", "prefix": 32}])
        self.assertEqual(snapshot["deadline_at"], "2026-09-02T12:02:00Z")
        self.assertTrue(snapshot["started_at"].endswith("Z"))
        self.assertIsNone(snapshot["reachability"])
        import json
        json.dumps(snapshot)


if __name__ == "__main__":
    unittest.main()
