"""Публичные адреса ноды за NAT: под каким IP её локальные адреса выходят наружу.

Зачем. В облаках (Cloud.ru, VK, Yandex) на карте ВМ только адрес вида 192.168.x
или 10.x, а публичный IP облако подставляет у себя. Панель видит адреса карт и не
знает, под каким IP нода ходит к соседям, — в белые списки он не попадает, и
соседи, закрытые по умолчанию, молча режут её подключения.

Как. Раз в минуту с каждого приватного адреса физических карт — запрос к сервису
«какой у меня IP», сокет привязан к этому адресу: ответ показывает, под каким
публичным IP выходит именно он. Адрес принимается, только когда его назвали два
разных сервиса: белый список открывает ему все порты на всех нодах парка, и
ответа одного сервиса для этого мало. С адреса, который наружу не выходит
(внутренняя сеть хостера), спрашивают реже. Приватных адресов нет — цикл
простаивает и никуда не ходит.
"""

import asyncio
import http.client
import ipaddress
import logging
import re
import ssl
import time
from collections import Counter
from http import HTTPStatus
from typing import Awaitable, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

PROBE_INTERVAL_SEC = 60
REQUEST_TIMEOUT_SEC = 5
CONFIRMATIONS = 2
# Адрес без выхода наружу: каждую минуту он стоил бы четырёх таймаутов
UNREACHABLE_RETRY_SEC = 600
# Не подтверждённый столько времени адрес забывается: облако могло отвязать IP,
# а белый список не должен годами держать чужой адрес. Больше UNREACHABLE_RETRY_SEC,
# чтобы одна неудачная попытка не выкидывала адрес из списков
STALE_AFTER_SEC = 900
MAX_BODY_BYTES = 1024
USER_AGENT = "monitoring-node"

# Разные владельцы: сбой или подмена ответа у одного не подтверждает адрес сама
ECHO_SERVICES: tuple[tuple[str, str], ...] = (
    ("checkip.amazonaws.com", "/"),
    ("api.ipify.org", "/"),
    ("ipv4.icanhazip.com", "/"),
    ("ipv4-internet.yandex.net", "/api/v0/ip"),
)

_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_TLS = ssl.create_default_context()

Resolve = Callable[[str], Awaitable[Optional[str]]]
EchoFetch = Callable[[str, str, str], Awaitable[Optional[str]]]


def is_private_ipv4(address: str) -> bool:
    try:
        return not ipaddress.IPv4Address(address).is_global
    except ValueError:
        return False


def parse_public_ip(body: Optional[str]) -> Optional[str]:
    """Первый IPv4 в ответе, если он публичный. Яндекс отдаёт адрес в кавычках."""
    match = _IPV4.search(body or "")
    if not match:
        return None
    try:
        address = ipaddress.IPv4Address(match.group())
    except ValueError:
        return None
    return str(address) if address.is_global else None


def fetch_echo(source: str, host: str, path: str) -> Optional[str]:
    connection = http.client.HTTPSConnection(
        host, timeout=REQUEST_TIMEOUT_SEC, source_address=(source, 0), context=_TLS,
    )
    try:
        connection.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "text/plain"})
        response = connection.getresponse()
        if response.status != HTTPStatus.OK:
            return None
        return response.read(MAX_BODY_BYTES).decode("ascii", errors="replace")
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()


async def fetch_echo_async(source: str, host: str, path: str) -> Optional[str]:
    return await asyncio.to_thread(fetch_echo, source, host, path)


async def confirmed_public_ip(
    source: str,
    fetch: EchoFetch = fetch_echo_async,
    services: Iterable[tuple[str, str]] = ECHO_SERVICES,
) -> Optional[str]:
    """Публичный IP, который назвали CONFIRMATIONS разных сервисов, иначе None.
    Сервисы опрашиваются по очереди: обычно хватает первых двух."""
    votes: Counter[str] = Counter()
    for host, path in services:
        public = parse_public_ip(await fetch(source, host, path))
        if public is None:
            continue
        votes[public] += 1
        if votes[public] >= CONFIRMATIONS:
            return public
    return None


class NatAddressProbe:
    def __init__(self, resolve: Resolve = confirmed_public_ip, clock: Callable[[], float] = time.monotonic):
        self._resolve = resolve
        self._clock = clock
        self._public: dict[str, tuple[str, float]] = {}
        self._retry_at: dict[str, float] = {}
        self._task: Optional[asyncio.Task] = None

    def public_address(self, local: str) -> Optional[str]:
        entry = self._public.get(local)
        return entry[0] if entry else None

    async def probe_once(self, addresses: Iterable[str]) -> None:
        local = {address for address in addresses if is_private_ipv4(address)}
        for gone in set(self._public) - local:
            del self._public[gone]
        for gone in set(self._retry_at) - local:
            del self._retry_at[gone]

        now = self._clock()
        due = sorted(address for address in local if self._retry_at.get(address, 0) <= now)
        results = await asyncio.gather(*(self._resolve(address) for address in due))

        now = self._clock()
        for address, public in zip(due, results):
            if public is None:
                self._retry_at[address] = now + UNREACHABLE_RETRY_SEC
                continue
            self._retry_at.pop(address, None)
            previous = self.public_address(address)
            self._public[address] = (public, now)
            if public != previous:
                logger.info("nat address found: local=%s public=%s previous=%s", address, public, previous)

        for address, (public, confirmed_at) in list(self._public.items()):
            if now - confirmed_at > STALE_AFTER_SEC:
                del self._public[address]
                logger.warning("nat address expired: local=%s public=%s", address, public)

    async def start(self, address_source: Callable[[], list[str]]) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(address_source))

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self, address_source: Callable[[], list[str]]) -> None:
        while True:
            try:
                await self.probe_once(await asyncio.to_thread(address_source))
            except Exception as exc:
                logger.error("nat address probe failed: %s", exc, exc_info=True)
            await asyncio.sleep(PROBE_INTERVAL_SEC)


_probe: Optional[NatAddressProbe] = None


def get_nat_address_probe() -> NatAddressProbe:
    global _probe
    if _probe is None:
        _probe = NatAddressProbe()
    return _probe
