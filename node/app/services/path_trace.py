"""Трасса от ноды до адреса — mtr по кнопке «Трасса» на странице «Потери» панели.

Нода гоняет mtr раундами по одному (`-c 1 -j`) и после каждого раунда копит итог
по узлам, поэтому окно панели видит путь постепенно, а не одним куском через
полминуты. Режим TCP на нужный порт — так идёт настоящий трафик; ICMP по пути
фильтруют и ограничивают иначе. Владельца узла (номер и название AS) нода узнаёт
DNS-запросами к Team Cymru и кеширует на время жизни процесса.

Трасса запускается только по запросу: 15 раундов по одному пакету на узел.
"""

import asyncio
import ipaddress
import json
import logging
import random
import struct
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Optional

from app.services.host_executor import HostExecutor, get_host_executor

logger = logging.getLogger(__name__)

ROUNDS = 15
ROUND_TIMEOUT_SEC = 30
MAX_ACTIVE_TRACES = 2
TRACE_TTL_SEC = 600
MAX_KEPT_TRACES = 20
MTR_INSTALL_TIMEOUT_SEC = 180
NO_REPLY_HOST = "???"

# Team Cymru на холодном кеше резолвера отвечает секунды
DNS_TIMEOUT_SEC = 3.0
DNS_FALLBACK_SERVER = "1.1.1.1"
RESOLV_CONF = Path("/etc/resolv.conf")
CYMRU_ORIGIN_ZONE = "origin.asn.cymru.com"
CYMRU_ORIGIN6_ZONE = "origin6.asn.cymru.com"
CYMRU_ASN_ZONE = "asn.cymru.com"

MTR_INSTALL_COMMAND = (
    "command -v mtr >/dev/null 2>&1 || "
    "{ export DEBIAN_FRONTEND=noninteractive; apt-get install -y -qq mtr-tiny >/dev/null 2>&1 || "
    "{ apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq mtr-tiny >/dev/null 2>&1; }; }; "
    "command -v mtr >/dev/null 2>&1"
)


class TraceState(str, Enum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class TraceBusyError(RuntimeError):
    pass


def mtr_round_command(ip: str, port: int) -> str:
    # ip и port уже проверены моделью запроса — в строку попадают только они.
    # -G 1: ждать ответов секунду после последнего пакета, а не 5 по умолчанию
    return f"mtr -n -T -P {int(port)} -c 1 -G 1 -j {ip}"


def parse_round(output: str) -> list[tuple[int, Optional[str], Optional[float]]]:
    """(номер узла, адрес или None, задержка мс или None) из JSON одного раунда mtr."""
    hubs = json.loads(output)["report"]["hubs"]
    result = []
    for hub in hubs:
        host = hub.get("host")
        host = None if not host or host == NO_REPLY_HOST else str(host)
        answered = host is not None and float(hub.get("Loss%", 100)) < 100
        result.append((int(hub["count"]), host, float(hub["Last"]) if answered else None))
    return result


@dataclass
class HopStats:
    hop: int
    hosts: Counter = field(default_factory=Counter)
    sent: int = 0
    received: int = 0
    rtt_sum: float = 0.0
    best: Optional[float] = None
    worst: Optional[float] = None

    def record(self, host: Optional[str], rtt_ms: Optional[float]) -> None:
        self.sent += 1
        if host:
            self.hosts[host] += 1
        if rtt_ms is None:
            return
        self.received += 1
        self.rtt_sum += rtt_ms
        self.best = rtt_ms if self.best is None else min(self.best, rtt_ms)
        self.worst = rtt_ms if self.worst is None else max(self.worst, rtt_ms)

    @property
    def host(self) -> Optional[str]:
        return self.hosts.most_common(1)[0][0] if self.hosts else None


@dataclass(frozen=True)
class AsnInfo:
    asn: str
    name: Optional[str]


@dataclass
class PathTrace:
    id: str
    ip: str
    port: int
    created_at: float
    state: TraceState = TraceState.RUNNING
    rounds_done: int = 0
    hops: dict[int, HopStats] = field(default_factory=dict)
    error: Optional[str] = None
    finished_at: Optional[float] = None

    def record_round(self, replies: list[tuple[int, Optional[str], Optional[float]]]) -> None:
        for hop, host, rtt_ms in replies:
            self.hops.setdefault(hop, HopStats(hop)).record(host, rtt_ms)
        self.rounds_done += 1

    def to_dict(self, asn_of: Callable[[str], Optional[AsnInfo]]) -> dict:
        hops = []
        for hop in sorted(self.hops.values(), key=lambda h: h.hop):
            info = asn_of(hop.host) if hop.host else None
            hops.append({
                "hop": hop.hop,
                "host": hop.host,
                "asn": info.asn if info else None,
                "as_name": info.name if info else None,
                "sent": hop.sent,
                "received": hop.received,
                "loss_pct": round((hop.sent - hop.received) * 100 / hop.sent, 1) if hop.sent else 0.0,
                "avg_ms": round(hop.rtt_sum / hop.received, 1) if hop.received else None,
                "best_ms": hop.best,
                "worst_ms": hop.worst,
            })
        return {
            "id": self.id, "ip": self.ip, "port": self.port, "state": self.state.value, "error": self.error,
            "rounds_done": self.rounds_done, "rounds_total": ROUNDS, "hops": hops,
        }


# ---------------------------------------------------------------------------
# DNS TXT — владельцы узлов через Team Cymru, без внешних зависимостей
# ---------------------------------------------------------------------------

def build_txt_query(name: str, query_id: int) -> bytes:
    header = struct.pack(">HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.strip(".").split(".")) + b"\0"
    return header + qname + struct.pack(">HH", 16, 1)


def _skip_name(packet: bytes, offset: int) -> int:
    while True:
        length = packet[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:
            return offset + 2
        offset += length + 1


def parse_txt_answer(packet: bytes, query_id: int) -> list[str]:
    response_id, flags, questions, answers = struct.unpack(">HHHH", packet[:8])
    if response_id != query_id or flags & 0x000F:
        return []
    offset = 12
    for _ in range(questions):
        offset = _skip_name(packet, offset) + 4
    texts = []
    for _ in range(answers):
        offset = _skip_name(packet, offset)
        record_type, _, _, length = struct.unpack(">HHIH", packet[offset:offset + 10])
        offset += 10
        if record_type == 16:
            data, position, parts = packet[offset:offset + length], 0, []
            while position < len(data):
                size = data[position]
                parts.append(data[position + 1:position + 1 + size].decode("utf-8", "replace"))
                position += size + 1
            texts.append("".join(parts))
        offset += length
    return texts


def system_nameserver() -> str:
    try:
        for line in RESOLV_CONF.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "nameserver":
                return parts[1]
    except OSError:
        pass
    return DNS_FALLBACK_SERVER


async def dns_txt(name: str, nameserver: str, timeout: float = DNS_TIMEOUT_SEC) -> list[str]:
    loop = asyncio.get_running_loop()
    query_id = random.randint(0, 0xFFFF)
    answer: asyncio.Future = loop.create_future()

    class Receiver(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            if not answer.done():
                answer.set_result(data)

        def error_received(self, exc):
            if not answer.done():
                answer.set_exception(exc)

    transport, _ = await loop.create_datagram_endpoint(Receiver, remote_addr=(nameserver, 53))
    try:
        transport.sendto(build_txt_query(name, query_id))
        return parse_txt_answer(await asyncio.wait_for(answer, timeout), query_id)
    finally:
        transport.close()


def origin_query_name(ip: str) -> Optional[str]:
    address = ipaddress.ip_address(ip)
    if not address.is_global:
        return None
    if address.version == 4:
        return ".".join(reversed(ip.split("."))) + "." + CYMRU_ORIGIN_ZONE
    nibbles = address.exploded.replace(":", "")
    return ".".join(reversed(nibbles)) + "." + CYMRU_ORIGIN6_ZONE


class AsnResolver:
    """IP → AS и её название. Ответ (в том числе «AS не найдена») кешируется на
    время жизни процесса; сбой DNS — нет: адрес спросится в следующем раунде."""

    def __init__(self, txt_lookup: Callable[[str], Awaitable[list[str]]]):
        self._txt = txt_lookup
        self._by_ip: dict[str, Optional[AsnInfo]] = {}
        self._names: dict[str, Optional[str]] = {}
        self._in_flight: set[str] = set()

    def known(self, ip: str) -> Optional[AsnInfo]:
        return self._by_ip.get(ip)

    async def resolve(self, ip: str) -> None:
        if ip in self._by_ip or ip in self._in_flight:
            return
        query = origin_query_name(ip)
        if not query:
            self._by_ip[ip] = None
            return
        self._in_flight.add(ip)
        try:
            records = await self._txt(query)
            if not records:
                self._by_ip[ip] = None
                return
            asn = records[0].split("|")[0].split()[0].strip()
            if asn not in self._names:
                names = await self._txt(f"AS{asn}.{CYMRU_ASN_ZONE}")
                self._names[asn] = names[0].split("|")[-1].strip() if names else None
            self._by_ip[ip] = AsnInfo(f"AS{asn}", self._names[asn])
        except (OSError, asyncio.TimeoutError, IndexError, UnicodeError, struct.error) as exc:
            logger.debug("asn_lookup_failed ip=%s error=%s", ip, exc)
        finally:
            self._in_flight.discard(ip)


# ---------------------------------------------------------------------------
# Менеджер трасс
# ---------------------------------------------------------------------------

class PathTraceManager:
    def __init__(
        self,
        executor: HostExecutor,
        resolver: AsnResolver,
        clock: Callable[[], float] = time.time,
    ):
        self._executor = executor
        self._resolver = resolver
        self._clock = clock
        self._traces: dict[str, PathTrace] = {}
        self._tasks: set[asyncio.Task] = set()

    def start(self, ip: str, port: int) -> PathTrace:
        self._prune()
        active = sum(trace.state is TraceState.RUNNING for trace in self._traces.values())
        if active >= MAX_ACTIVE_TRACES:
            raise TraceBusyError(f"{active} traces are already running on this node")
        trace = PathTrace(id=uuid.uuid4().hex, ip=ip, port=port, created_at=self._clock())
        self._traces[trace.id] = trace
        task = asyncio.create_task(self.run(trace))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return trace

    def get(self, trace_id: str) -> Optional[PathTrace]:
        return self._traces.get(trace_id)

    def view(self, trace: PathTrace) -> dict:
        return trace.to_dict(self._resolver.known)

    def _prune(self) -> None:
        now = self._clock()
        finished = sorted(
            (t for t in self._traces.values() if t.state is not TraceState.RUNNING),
            key=lambda t: t.created_at, reverse=True,
        )
        for index, trace in enumerate(finished):
            if index >= MAX_KEPT_TRACES or now - (trace.finished_at or trace.created_at) > TRACE_TTL_SEC:
                del self._traces[trace.id]

    async def run(self, trace: PathTrace) -> None:
        try:
            installed = await self._executor.execute(MTR_INSTALL_COMMAND, timeout=MTR_INSTALL_TIMEOUT_SEC)
            if not installed.success:
                raise RuntimeError("mtr is not installed and could not be installed (apt-get install mtr-tiny)")
            command = mtr_round_command(trace.ip, trace.port)
            for _ in range(ROUNDS):
                result = await self._executor.execute(command, timeout=ROUND_TIMEOUT_SEC)
                if not result.success:
                    raise RuntimeError((result.stderr or result.error or "mtr failed").strip()[:300])
                trace.record_round(parse_round(result.stdout))
                await asyncio.gather(*(
                    self._resolver.resolve(hop.host) for hop in trace.hops.values() if hop.host
                ))
            trace.state = TraceState.DONE
        except Exception as exc:
            trace.state = TraceState.FAILED
            trace.error = str(exc)
            logger.warning("path_trace_failed ip=%s port=%s error=%s", trace.ip, trace.port, exc)
        finally:
            trace.finished_at = self._clock()


async def _system_txt(name: str) -> list[str]:
    return await dns_txt(name, system_nameserver())


_manager: Optional[PathTraceManager] = None


def get_path_trace_manager() -> PathTraceManager:
    global _manager
    if _manager is None:
        _manager = PathTraceManager(get_host_executor(), AsnResolver(_system_txt))
    return _manager
