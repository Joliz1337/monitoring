"""Tests for HAProxyManager live stats (show stat / show info parsing and fallbacks).

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Парсинг CSV и выбор пути сокета — чистые преобразования, ошибка в них тихая:
секция статистики в панели покажет пустоту или неверные статусы бекендов.
"""

import os
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.haproxy_manager import HAProxyManager  # noqa: E402


def make_manager() -> HAProxyManager:
    """Менеджер без побочных эффектов конструктора."""
    manager = HAProxyManager.__new__(HAProxyManager)
    manager._stats_cache = None
    manager._stats_cache_time = 0
    manager._stats_cache_ttl = 2.0
    return manager


# Реалистичный срез вывода `show stat`: trailing comma в каждой строке,
# незнакомые парсеру колонки (qcur, dreq, act), пустые числовые поля
SHOW_STAT_CSV = """\
# pxname,svname,qcur,qmax,scur,smax,slim,stot,bin,bout,dreq,dresp,ereq,econ,eresp,status,weight,act,bck,lastchg,downtime,rate,rate_max,check_status,addr,mode,
web_balancer,FRONTEND,,,3,12,2000,1543,123456,654321,0,0,0,,,OPEN,,,,,,5,9,,,tcp,
web_balancer,web1,0,0,2,8,,1000,100000,500000,0,0,,0,0,UP,10,1,0,3600,0,2,7,L4OK,10.0.0.6:8080,tcp,
web_balancer,web2,0,0,0,2,,543,23456,154321,0,0,,1,2,DOWN,10,0,1,120,300,0,3,L4TOU,10.0.0.7:8080,tcp,
web_balancer,BACKEND,0,0,2,8,200,1543,123456,654321,0,0,,1,2,UP,20,1,1,3600,0,5,9,,,tcp,
tcp_relay,FRONTEND,,,1,4,2000,100,1111,2222,0,0,0,,,OPEN,,,,,,1,2,,,tcp,
backend_tcp_relay,srv1,0,0,1,4,,100,1111,2222,0,0,,0,0,no check,1,1,0,7200,0,1,2,,10.0.0.5:443,tcp,
backend_tcp_relay,BACKEND,0,0,1,4,200,100,1111,2222,0,0,,0,0,UP,1,1,0,7200,0,1,2,,,tcp,
"""

SHOW_INFO_TEXT = """\
Name: HAProxy
Version: 2.8.5-1ubuntu3
Release_date: 2023/12/09
Uptime: 0d 2h03m10s
Uptime_sec: 7390
CurrConns: 42
line without separator
"""


class ShowStatParsingTests(unittest.TestCase):
    def parse(self, text: str):
        return {p["name"]: p for p in HAProxyManager._parse_show_stat(text)}

    def test_rows_are_grouped_by_proxy_and_classified(self):
        proxies = self.parse(SHOW_STAT_CSV)
        self.assertEqual(set(proxies), {"web_balancer", "tcp_relay", "backend_tcp_relay"})

        balancer = proxies["web_balancer"]
        self.assertEqual(balancer["mode"], "tcp")
        self.assertEqual(balancer["frontend"]["kind"], "frontend")
        self.assertEqual(balancer["backend"]["kind"], "backend")
        self.assertEqual([s["name"] for s in balancer["servers"]], ["web1", "web2"])

        # Обычное правило: frontend и backend живут в разных pxname
        self.assertIsNone(proxies["tcp_relay"]["backend"])
        self.assertIsNone(proxies["backend_tcp_relay"]["frontend"])
        self.assertEqual(proxies["backend_tcp_relay"]["servers"][0]["name"], "srv1")

    def test_server_fields_are_parsed(self):
        up, down = self.parse(SHOW_STAT_CSV)["web_balancer"]["servers"]

        self.assertEqual(up["status"], "UP")
        self.assertEqual(up["check_status"], "L4OK")
        self.assertEqual(up["addr"], "10.0.0.6:8080")
        self.assertEqual((up["scur"], up["smax"], up["stot"]), (2, 8, 1000))
        self.assertEqual((up["bin"], up["bout"]), (100000, 500000))
        self.assertEqual((up["rate"], up["rate_max"]), (2, 7))
        self.assertEqual(up["weight"], 10)
        self.assertEqual(up["lastchg"], 3600)
        self.assertFalse(up["backup"])

        self.assertEqual(down["status"], "DOWN")
        self.assertEqual(down["check_status"], "L4TOU")
        self.assertEqual((down["econ"], down["eresp"]), (1, 2))
        self.assertEqual(down["downtime"], 300)
        self.assertTrue(down["backup"])

    def test_empty_numeric_fields_become_none_or_zero(self):
        balancer = self.parse(SHOW_STAT_CSV)["web_balancer"]
        self.assertIsNone(balancer["servers"][0]["slim"])
        self.assertEqual(balancer["backend"]["slim"], 200)
        # У фронтенда weight/econ пустые — Optional-поля остаются None
        self.assertIsNone(balancer["frontend"]["weight"])
        self.assertIsNone(balancer["frontend"]["econ"])

    def test_addr_only_for_servers(self):
        balancer = self.parse(SHOW_STAT_CSV)["web_balancer"]
        self.assertIsNone(balancer["frontend"]["addr"])
        self.assertIsNone(balancer["backend"]["addr"])

    def test_missing_addr_column_yields_none(self):
        # haproxy < 1.7 не отдаёт колонку addr — парсер по заголовку не падает
        csv_without_addr = (
            "# pxname,svname,scur,status,bck,\n"
            "b,srv1,3,UP,0,\n"
        )
        proxy = self.parse(csv_without_addr)["b"]
        self.assertIsNone(proxy["servers"][0]["addr"])
        self.assertEqual(proxy["servers"][0]["scur"], 3)

    def test_garbage_and_empty_input(self):
        self.assertEqual(HAProxyManager._parse_show_stat(""), [])
        self.assertEqual(HAProxyManager._parse_show_stat("Unknown command.\n"), [])


class ShowInfoParsingTests(unittest.TestCase):
    def test_fields_extracted(self):
        info = HAProxyManager._parse_show_info(SHOW_INFO_TEXT)
        self.assertEqual(info["haproxy_version"], "2.8.5-1ubuntu3")
        self.assertEqual(info["uptime_sec"], 7390)
        self.assertEqual(info["curr_conns"], 42)

    def test_empty_input_yields_nones(self):
        info = HAProxyManager._parse_show_info("")
        self.assertIsNone(info["haproxy_version"])
        self.assertIsNone(info["uptime_sec"])


class StatsSocketPathTests(unittest.TestCase):
    def path_from(self, config: str):
        manager = make_manager()
        with mock.patch.object(HAProxyManager, "_read_config", return_value=config):
            return manager._stats_socket_path_from_config()

    def test_path_extracted_from_config(self):
        config = "global\n    stats socket /var/run/haproxy.sock mode 660 level admin\n"
        self.assertEqual(self.path_from(config), "/var/run/haproxy.sock")

    def test_none_when_line_absent(self):
        self.assertIsNone(self.path_from("global\n    no log\n"))

    def test_candidates_prefer_host_run_path(self):
        candidates = HAProxyManager._stats_socket_candidates("/var/run/haproxy.sock")
        self.assertEqual(candidates, [
            "/proc/1/root/run/haproxy.sock",
            "/proc/1/root/var/run/haproxy.sock",
            "/var/run/haproxy.sock",
        ])

    def test_candidates_deduplicated_for_run_path(self):
        candidates = HAProxyManager._stats_socket_candidates("/run/haproxy.sock")
        self.assertEqual(candidates, ["/proc/1/root/run/haproxy.sock", "/run/haproxy.sock"])


CONFIG_WITH_SOCKET = "global\n    stats socket /var/run/haproxy.sock mode 660 level admin\n"


class GetStatsTests(unittest.TestCase):
    def get_stats(self, config: str = CONFIG_WITH_SOCKET, query=None, is_running: bool = True):
        manager = make_manager()
        with mock.patch.object(HAProxyManager, "_read_config", return_value=config), \
             mock.patch.object(HAProxyManager, "_query_stats_socket", side_effect=query), \
             mock.patch.object(HAProxyManager, "is_running", return_value=is_running):
            return manager.get_stats()

    def test_socket_not_configured(self):
        result = self.get_stats(config="global\n    no log\n")
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "socket_not_configured")

    def test_haproxy_stopped_when_all_candidates_fail(self):
        result = self.get_stats(query=FileNotFoundError, is_running=False)
        self.assertEqual(result["reason"], "haproxy_stopped")

    def test_socket_unavailable_when_running_without_socket(self):
        result = self.get_stats(query=ConnectionRefusedError, is_running=True)
        self.assertEqual(result["reason"], "socket_unavailable")

    def test_timeout_reported(self):
        result = self.get_stats(query=socket.timeout)
        self.assertEqual(result["reason"], "timeout")

    def test_success_merges_stat_and_info(self):
        def query(command, path):
            return SHOW_STAT_CSV if command == "show stat" else SHOW_INFO_TEXT

        result = self.get_stats(query=query)
        self.assertTrue(result["available"])
        self.assertEqual(result["haproxy_version"], "2.8.5-1ubuntu3")
        self.assertEqual(len(result["proxies"]), 3)

    def test_show_info_failure_is_not_fatal(self):
        def query(command, path):
            if command == "show info":
                raise ConnectionRefusedError()
            return SHOW_STAT_CSV

        result = self.get_stats(query=query)
        self.assertTrue(result["available"])
        self.assertNotIn("haproxy_version", result)
        self.assertEqual(len(result["proxies"]), 3)

    def test_cache_prevents_repeated_socket_queries(self):
        manager = make_manager()
        query = mock.Mock(return_value=SHOW_STAT_CSV)
        with mock.patch.object(HAProxyManager, "_read_config", return_value=CONFIG_WITH_SOCKET), \
             mock.patch.object(HAProxyManager, "_query_stats_socket", query):
            first = manager.get_stats()
            calls_after_first = query.call_count
            second = manager.get_stats()

        self.assertIs(first, second)
        self.assertEqual(query.call_count, calls_after_first)


if __name__ == "__main__":
    unittest.main()
