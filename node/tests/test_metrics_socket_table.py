"""Таблица сокетов в /api/metrics живёт в своём кэше, остальной блок system от неё не зависит.

Счётчики соединений и занятость эфемерных портов — разбор /proc/net/{tcp,udp}*:
на ноде с сотнями тысяч сокетов это доли секунды на каждый опрос панели. Срок
жизни разбора растёт вместе с его ценой, а дешёвые поля блока — имя ОС,
open_files, аптайм — сохраняют свои пять секунд, каким бы дорогим он ни был.

Хост подменён каталогом: /proc и /etc читаются из временной папки.

Запуск из node/:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import socket
import sys
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.metrics import SystemInfo  # noqa: E402
from app.services import metrics_collector  # noqa: E402
from app.services.adaptive_cache import AdaptiveTtlCache  # noqa: E402
from app.services.metrics_collector import MetricsCollector  # noqa: E402
from app.services.nat_addresses import NatAddressProbe  # noqa: E402

# 203.0.113.10 — адрес ноды, 142.250.185.78 — цель; hex в порядке байтов хоста, как в /proc
NODE = "0A7100CB"
PEER = "4EB9FA8E"
FIRST_EPHEMERAL_PORT = 0x9C40
ESTABLISHED = "01"
LISTEN = "0A"

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
        collector = MetricsCollector(
            rate_sampler=SimpleNamespace(), loss_probe=SimpleNamespace(),
            nat_addresses=NatAddressProbe(), clock=clock,
        )
    # Фоновый пересчёт таблицы сокетов — без потока: к возврату из опроса он уже отработал
    collector._socket_table = AdaptiveTtlCache(
        collector._read_host_connections, clock=clock, spawn=lambda refresh: refresh(),
    )
    return collector


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
        self.assertEqual(self.established(), 2)

        # Срок вышел: этот опрос ещё получает прошлый срез, свежий — следующий
        self.clock.now += 0.25
        self.assertEqual(self.established(), 2)
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


class SocketTableParseTests(unittest.TestCase):
    """Разбор идёт построчно, одним проходом: и счётчики состояний, и учёт эфемерных портов."""

    def setUp(self):
        host_dir = tempfile.TemporaryDirectory()
        self.addCleanup(host_dir.cleanup)
        self.host = FakeHost(Path(host_dir.name))
        self.collector = collector_on(self.host, FakeClock())

    def test_states_are_counted_across_tcp_and_tcp6(self):
        states = ["01", "01", "02", "03", "04", "05", "06", "07", "08", "09", "0A", "0B"]
        self.host.write("proc/net/tcp", TCP_HEADER + "".join(
            tcp_row(f"{NODE}:{FIRST_EPHEMERAL_PORT + number:04X}", f"{PEER}:01BB", state)
            for number, state in enumerate(states)
        ))
        mapped_node = "0000000000000000FFFF0000" + NODE
        self.host.write("proc/net/tcp6", TCP_HEADER + tcp_row(f"{mapped_node}:9D00", f"{mapped_node}:01BB", ESTABLISHED))

        self.assertEqual(self.collector._read_host_connections()["tcp"], {
            "total": 13, "established": 3, "listen": 1, "time_wait": 1, "close_wait": 1,
            "syn_sent": 1, "syn_recv": 1, "fin_wait": 2, "other": 3,
        })

    def test_inbound_connections_to_a_listening_port_are_not_ephemeral_usage(self):
        # Порт слушателя лежит внутри эфемерного диапазона, как у inbound Xray на
        # vpn-профиле: его клиенты исходящих портов не занимают
        listener = tcp_row("00000000:C350", "00000000:0000", LISTEN)
        inbound = [tcp_row(f"{NODE}:C350", f"{PEER}:{0x8000 + number:04X}", ESTABLISHED) for number in range(3)]
        outbound = [
            tcp_row(f"{NODE}:{FIRST_EPHEMERAL_PORT + number:04X}", f"{PEER}:01BB", ESTABLISHED)
            for number in range(2)
        ]
        self.host.write("proc/net/tcp", TCP_HEADER + listener + "".join(inbound + outbound))

        table = self.collector._read_host_connections()

        self.assertEqual((table["tcp"]["listen"], table["tcp"]["established"]), (1, 5))
        self.assertEqual([source["used"] for source in table["ephemeral_ports"]["sources"]], [2])

    def test_node_without_tcp_tables_reports_no_ephemeral_block(self):
        (self.host.root / "proc/net/tcp").unlink()

        table = self.collector._read_host_connections()

        self.assertEqual(table["tcp"]["total"], 0)
        self.assertIsNone(table["ephemeral_ports"])

    def test_table_is_parsed_without_holding_the_file_in_memory(self):
        # На миллионе сокетов файл — полтораста мегабайт: целиком, да ещё списком
        # строк, он стоил бы агенту сотен мегабайт на каждый разбор
        self.host.write_tcp(established=20_000)
        table_size = (self.host.root / "proc/net/tcp").stat().st_size

        tracemalloc.start()
        self.addCleanup(tracemalloc.stop)
        table = self.collector._read_host_connections()
        _, peak = tracemalloc.get_traced_memory()

        self.assertEqual(table["tcp"]["established"], 20_000)
        self.assertLess(peak, table_size // 4)


@unittest.skipUnless(sys.platform == "linux", "порядок строк задаёт ядро Linux")
class KernelRowOrderTests(unittest.TestCase):
    """Разбор в один проход узнаёт слушающие порты по ходу чтения — это верно,
    пока ядро печатает слушающие сокеты раньше соединений."""

    def test_listening_sockets_come_before_connections(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        for _ in range(2):
            client = socket.create_connection(listener.getsockname())
            self.addCleanup(client.close)
            self.addCleanup(listener.accept()[0].close)
        # Слушатель, открытый позже соединений, всё равно печатается раньше них
        late_listener = socket.socket()
        self.addCleanup(late_listener.close)
        late_listener.bind(("127.0.0.1", 0))
        late_listener.listen(1)

        states = [row.split()[3] for row in Path("/proc/net/tcp").read_text().splitlines()[1:]]

        first_connection = next(index for index, state in enumerate(states) if state != LISTEN)
        self.assertGreaterEqual(states.count(LISTEN), 2)
        self.assertNotIn(LISTEN, states[first_connection:])


if __name__ == "__main__":
    unittest.main()
