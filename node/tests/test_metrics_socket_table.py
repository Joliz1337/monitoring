"""Таблица сокетов в /api/metrics живёт в своём кэше, остальной блок system от неё не зависит.

Счётчики соединений и занятость эфемерных портов — разбор /proc/net/{tcp,udp}*:
на ноде с сотнями тысяч сокетов это доли секунды на каждый опрос панели. Срок
жизни разбора растёт вместе с его ценой, а дешёвые поля блока — имя ОС,
open_files, аптайм — сохраняют свои пять секунд, каким бы дорогим он ни был.

Хост подменён каталогом: /proc и /etc читаются из временной папки.

Запуск из node/:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.metrics import SystemInfo  # noqa: E402
from app.services import metrics_collector  # noqa: E402
from app.services.metrics_collector import MetricsCollector  # noqa: E402
from app.services.nat_addresses import NatAddressProbe  # noqa: E402

# 203.0.113.10 — адрес ноды, 142.250.185.78 — цель; hex в порядке байтов хоста, как в /proc
NODE = "0A7100CB"
PEER = "4EB9FA8E"
FIRST_EPHEMERAL_PORT = 0x9C40
ESTABLISHED = "01"

TCP_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
UDP_HEADER = "   sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops\n"
UDP_ROW = " 1024: 00000000:0035 00000000:0000 07 00000000:00000000 00:00000000 00000000     0        0 4321 2 0000000000000000 0\n"


def tcp_row(local: str, remote: str, state: str) -> str:
    return f"   0: {local} {remote} {state} 00000000:00000000 00:00000000 00000000     0        0 1234 1 0000 20 0 0 10 -1\n"


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeHost:
    """Корень файловой системы хоста во временном каталоге."""

    def __init__(self, root: Path):
        self.root = root
        self.write("etc/os-release", 'PRETTY_NAME="Ubuntu 24.04 LTS"\n')
        self.write("etc/timezone", "Europe/Berlin\n")
        self.write("proc/sys/kernel/random/boot_id", "7c1d4a52-0000-4000-8000-000000000001\n")
        self.write_tcp(established=0)

    def write(self, path: str, content: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def write_tcp(self, established: int) -> None:
        rows = [
            tcp_row(f"{NODE}:{FIRST_EPHEMERAL_PORT + number:04X}", f"{PEER}:01BB", ESTABLISHED)
            for number in range(established)
        ]
        self.write("proc/net/tcp", TCP_HEADER + "".join(rows))


def collector_on(host: FakeHost, clock: FakeClock) -> MetricsCollector:
    settings = SimpleNamespace(host_proc=str(host.root / "proc"), node_name="test")
    with mock.patch.object(metrics_collector, "get_settings", return_value=settings):
        return MetricsCollector(
            rate_sampler=SimpleNamespace(), loss_probe=SimpleNamespace(),
            nat_addresses=NatAddressProbe(), clock=clock,
        )


class SocketTableInSystemBlockTests(unittest.TestCase):

    def setUp(self):
        host_dir = tempfile.TemporaryDirectory()
        self.addCleanup(host_dir.cleanup)
        self.host = FakeHost(Path(host_dir.name))
        self.clock = FakeClock()
        self.collector = collector_on(self.host, self.clock)

    def established(self) -> int:
        return self.collector.get_system_info()["connections"]["established"]

    def test_quiet_node_rereads_the_socket_table_after_five_seconds(self):
        self.host.write_tcp(established=2)
        self.assertEqual(self.established(), 2)

        self.host.write_tcp(established=3)
        self.clock.now += 4.75
        self.assertEqual(self.established(), 2)

        self.clock.now += 0.25
        self.assertEqual(self.established(), 3)

    def test_cheap_fields_are_refreshed_while_the_socket_table_is_still_cached(self):
        self.host.write_tcp(established=2)
        # От настоящего времени: psutil на Windows считает от time.time() момент загрузки
        wall_clock = FakeClock(now=time.time())
        with mock.patch.object(metrics_collector.time, "time", wall_clock):
            before = self.collector.get_system_info()
            self.host.write("etc/os-release", 'PRETTY_NAME="Ubuntu 24.04.1 LTS"\n')
            self.host.write_tcp(established=3)
            # Прошли пять секунд блока system; часы кэша таблицы сокетов стоят
            wall_clock.now += 5.0
            after = self.collector.get_system_info()

        self.assertEqual((before["os"], after["os"]), ("Ubuntu 24.04 LTS", "Ubuntu 24.04.1 LTS"))
        self.assertEqual(after["connections"]["established"], 2)

    def test_every_poll_gets_the_whole_system_block(self):
        self.host.write_tcp(established=2)
        self.host.write("proc/net/udp", UDP_HEADER + UDP_ROW * 3)

        # Второй опрос обслуживается из кэша: первый не должен «израсходовать» срез
        for poll in ("parsed", "cached"):
            with self.subTest(poll=poll):
                system = self.collector.get_system_info()

                self.assertEqual(set(system), set(SystemInfo.model_fields))
                SystemInfo.model_validate(system)
                self.assertEqual(
                    system["connections"], {"established": 2, "listen": 0, "time_wait": 0, "other": 0}
                )
                self.assertEqual(set(system["connections_detailed"]), {"tcp", "udp"})
                self.assertEqual(system["connections_detailed"]["tcp"]["total"], 2)
                self.assertEqual(system["connections_detailed"]["udp"]["total"], 3)
                self.assertIsNotNone(system["ephemeral_ports"])
                self.assertEqual([source["used"] for source in system["ephemeral_ports"]["sources"]], [2])


if __name__ == "__main__":
    unittest.main()
