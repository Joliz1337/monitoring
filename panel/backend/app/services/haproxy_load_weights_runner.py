"""Фоновая рассылка весов по нагрузке на релеи HAProxy.

Коллектор метрик после каждого цикла отдаёт сюда нагрузку нод и будит цикл.
Веса меняются на релее командой HAProxy на лету: без reload, открытые
соединения остаются на своих серверах. Нода держит присланные веса ограниченный
срок и сама вернёт веса из конфига, если панель замолчит.
"""

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional

import httpx
from sqlalchemy import Select, select
from sqlalchemy.orm import defer

from app.database import async_session
from app.models import HAProxyConfigProfile, Server
from app.services.haproxy_addresses import ServerAddresses, load_ips, render_for_server
from app.services.haproxy_config import LOAD_WEIGHTS_MARKER
from app.services.haproxy_load_weights import (
    WEIGHT_RESEND_SECONDS,
    WEIGHT_TTL_SECONDS,
    LineWeight,
    NodeLoad,
    WeightKey,
    relay_weights,
    rule_views,
    should_push,
)
from app.services.haproxy_profile_sync import is_server_online
from app.services.http_client import get_node_client, node_auth_headers
from app.services.node_capabilities import Capability, server_allows
from app.services.reserved_ports_sync import _version_tuple
from app.services.server_ip_owners import load_ip_owners

logger = logging.getLogger(__name__)

MIN_NODE_VERSION_HAPROXY_WEIGHTS = "10.35.0"
RUNTIME_WEIGHTS_PATH = "/api/haproxy/runtime/weights"

# Нагрузка старше трёх циклов сбора — нода молчит, и судить по ней нельзя
STALE_CYCLES = 3
DEFAULT_COLLECT_INTERVAL = 10
PUSH_CONCURRENCY = 50
PUSH_TIMEOUT_SEC = 10.0
# Рукопожатие SOCKS5-прокси httpx-таймаутами не покрыто и способно повиснуть навсегда
PUSH_HARD_TIMEOUT_SEC = 15.0
NODE_MAX_ITEMS = 4096
# Карта «адрес → сервер» разбирает last_metrics всего парка — раз в цикл это дорого
OWNERS_CACHE_SECONDS = 60
# Подставные выходные адреса: имена копий _oN зависят только от их числа, а не от
# самих адресов, поэтому релеи с одинаковым числом выходных IP считаются один раз
PLACEHOLDER_SOURCE_NET = "192.0.2."
SYNCED = "synced"

ShapeKey = tuple[str, int]


def relay_select() -> Select:
    """Релеи профилей без last_metrics: метрики нужны коллектору, а здесь каждые
    10 секунд они тянули бы из базы десятки килобайт на сервер впустую."""
    return (
        select(Server)
        .options(defer(Server.last_metrics))
        .where(Server.is_active == True, Server.active_haproxy_profile_id.isnot(None))  # noqa: E712
    )


@dataclass
class RelayTarget:
    server: Server
    config_content: str


@dataclass
class CycleInput:
    relays: list[RelayTarget]
    profiles: dict[int, str]
    owners: dict[str, int]
    capacities: dict[int, int]


@dataclass
class _Pushed:
    weights: dict[WeightKey, int]
    at: float


class LoadWeightsRunner:
    def __init__(
        self,
        session_factory=async_session,
        client_factory: Callable[[Server], httpx.AsyncClient] = get_node_client,
        clock: Callable[[], float] = time.monotonic,
        max_items_per_request: int = NODE_MAX_ITEMS,
    ):
        self._session_factory = session_factory
        self._client_factory = client_factory
        self._clock = clock
        self._max_items = max_items_per_request
        self._loads: dict[int, tuple[NodeLoad, float]] = {}
        self._stale_after = STALE_CYCLES * DEFAULT_COLLECT_INTERVAL
        self._pushed: dict[int, _Pushed] = {}
        self._forced: set[int] = set()
        # Нода, ответившая 404, не умеет веса до смены версии агента
        self._unsupported: dict[int, Optional[str]] = {}
        self._failing: set[int] = set()
        self._views: dict[int, dict[str, list[LineWeight]]] = {}
        self._views_at: Optional[datetime] = None
        self._owners: dict[str, int] = {}
        self._owners_at: Optional[float] = None
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def observe(self, loads: Mapping[int, NodeLoad], interval_seconds: int) -> None:
        now = self._clock()
        for server_id, load in loads.items():
            self._loads[server_id] = (load, now)
        self._stale_after = STALE_CYCLES * interval_seconds
        self._wake.set()

    def request_push(self, server_id: int) -> None:
        """После reload на релее веса вернулись к конфигу — прислать заново, не дожидаясь шага."""
        self._forced.add(server_id)
        self._wake.set()

    def profile_view(self, profile_id: int) -> dict:
        rules = self._views.get(profile_id, {})
        return {
            "rules": {name: [asdict(line) for line in lines] for name, lines in rules.items()},
            "updated_at": self._views_at.isoformat() if self._views_at else None,
        }

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        while True:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), WEIGHT_RESEND_SECONDS)
            self._wake.clear()
            try:
                await self.run_once()
            except Exception:
                logger.exception("load weights cycle failed")

    async def run_once(self) -> None:
        data = await self._load_cycle_input()
        if data is None:
            self._views, self._pushed = {}, {}
            return
        await self.push_cycle(data)

    async def _load_cycle_input(self) -> Optional[CycleInput]:
        """None — ни в одном профиле нет правил с весами по нагрузке: парк не трогаем."""
        async with self._session_factory() as db:
            rows = (await db.execute(select(HAProxyConfigProfile.id, HAProxyConfigProfile.config_content))).all()
            profiles = {profile_id: config or "" for profile_id, config in rows}
            if not any(LOAD_WEIGHTS_MARKER in config for config in profiles.values()):
                return None
            relays = (await db.execute(relay_select())).scalars().all()
            capacities = dict((await db.execute(
                select(Server.id, Server.link_capacity_mbit).where(Server.link_capacity_mbit.isnot(None))
            )).all())
            if self._owners_at is None or self._clock() - self._owners_at >= OWNERS_CACHE_SECONDS:
                self._owners = {address: owner.id for address, owner in (await load_ip_owners(db)).items()}
                self._owners_at = self._clock()
        return CycleInput(
            relays=[RelayTarget(s, profiles[s.active_haproxy_profile_id]) for s in relays
                    if s.active_haproxy_profile_id in profiles],
            profiles=profiles,
            owners=self._owners,
            capacities=capacities,
        )

    async def push_cycle(self, data: CycleInput) -> None:
        loads = self._fresh_loads()
        self._views = {
            profile_id: rule_views(config, data.owners, data.capacities, loads)
            for profile_id, config in data.profiles.items() if LOAD_WEIGHTS_MARKER in config
        }
        self._views_at = datetime.now(timezone.utc)

        sem = asyncio.Semaphore(PUSH_CONCURRENCY)
        shapes: dict[ShapeKey, dict[WeightKey, int]] = {}

        async def push_one(target: RelayTarget) -> None:
            async with sem:
                try:
                    await self._push_relay(target, data, loads, shapes)
                except Exception:
                    # Один сбойный релей (например, без mTLS-контекста) не должен
                    # оставлять без весов весь парк
                    if target.server.id not in self._failing:
                        self._failing.add(target.server.id)
                        logger.exception("load weights: relay %s failed", target.server.name)

        await asyncio.gather(*(push_one(t) for t in data.relays if self._eligible(t.server)))

    def _fresh_loads(self) -> dict[int, NodeLoad]:
        now = self._clock()
        return {sid: load for sid, (load, at) in self._loads.items() if now - at <= self._stale_after}

    def _eligible(self, server: Server) -> bool:
        # Синхронизация не прошла — на релее старый конфиг, и веса нового попали бы
        # не в те бэкенды (а при hash-алгоритме перекинули бы клиентов)
        if server.haproxy_sync_status != SYNCED:
            return False
        if not is_server_online(server) or not server_allows(server, Capability.HAPROXY, write=True):
            return False
        version = server.node_version
        if not version or _version_tuple(version) < _version_tuple(MIN_NODE_VERSION_HAPROXY_WEIGHTS):
            return False
        return not (server.id in self._unsupported and self._unsupported[server.id] == version)

    async def _push_relay(
        self,
        target: RelayTarget,
        data: CycleInput,
        loads: Mapping[int, NodeLoad],
        shapes: dict[ShapeKey, dict[WeightKey, int]],
    ) -> None:
        server = target.server
        if LOAD_WEIGHTS_MARKER not in target.config_content:
            # Режим выключили — reload после синхронизации сам вернул веса из конфига
            self._pushed.pop(server.id, None)
            self._forced.discard(server.id)
            return
        shape = (target.config_content, len(load_ips(server.haproxy_source_ips)))
        if shape not in shapes:
            placeholders = ServerAddresses(source=tuple(f"{PLACEHOLDER_SOURCE_NET}{i + 1}" for i in range(shape[1])))
            rendered = render_for_server(target.config_content, placeholders)
            shapes[shape] = relay_weights(rendered, data.owners, data.capacities, loads)
        current = shapes[shape]
        previous = self._pushed.get(server.id)
        since = None if previous is None else self._clock() - previous.at
        if not should_push(previous.weights if previous else None, current, since, server.id in self._forced):
            return

        items = [{"backend": backend, "server": name, "weight": weight} for (backend, name), weight in current.items()]
        guarded: list[str] = []
        for start in range(0, len(items), self._max_items):
            batch_guarded = await self._send(server, items[start:start + self._max_items])
            if batch_guarded is None:
                return
            guarded += batch_guarded

        self._pushed[server.id] = _Pushed(dict(current), self._clock())
        self._forced.discard(server.id)
        self._failing.discard(server.id)
        if guarded:
            logger.info("load weights: relay %s restored config weights, no live weighted server in %s",
                        server.name, guarded)

    async def _send(self, server: Server, items: list[dict]) -> Optional[list[str]]:
        """guarded_backends ответа ноды; None — отправка не удалась."""
        try:
            response = await asyncio.wait_for(
                self._client_factory(server).put(
                    f"{server.url}{RUNTIME_WEIGHTS_PATH}",
                    json={"weights": items, "ttl_seconds": WEIGHT_TTL_SECONDS},
                    headers=node_auth_headers(server),
                    timeout=PUSH_TIMEOUT_SEC,
                ),
                PUSH_HARD_TIMEOUT_SEC,
            )
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            self._warn_once(server, f"request failed: {e!r}")
            return None
        if response.status_code == 404:
            self._unsupported[server.id] = server.node_version
            logger.info("load weights: node %s (%s) has no runtime weights endpoint", server.name, server.node_version)
            return None
        if response.status_code != 200:
            self._warn_once(server, f"HTTP {response.status_code}: {response.text[:200]}")
            return None
        try:
            return list(response.json().get("guarded_backends") or [])
        except (ValueError, AttributeError):
            return []

    def _warn_once(self, server: Server, reason: str) -> None:
        if server.id in self._failing:
            return
        self._failing.add(server.id)
        logger.warning("load weights: push to %s failed: %s", server.name, reason)


_runner: Optional[LoadWeightsRunner] = None


def get_load_weights_runner() -> LoadWeightsRunner:
    global _runner
    if _runner is None:
        _runner = LoadWeightsRunner()
    return _runner
