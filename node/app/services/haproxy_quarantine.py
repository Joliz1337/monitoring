"""Вывод адресов из балансировки HAProxy по списку от панели.

Панель видит, что релей не доходит до адреса (потери, частые падения), и
присылает список таких адресов. Нода переводит их серверы в обслуживание
командой `set server … state maint` через admin-сокет: без reload, новые
клиенты и клиенты из stick-table уходят на другие серверы, а открытые
соединения закрывает `shutdown sessions server` — иначе клиент висел бы на
больном сервере, пока соединение не умрёт само.

Список декларативный и со сроком: чего в нём нет или что панель перестала
подтверждать, нода возвращает в работу сама. Состояние лежит на диске — после
перезапуска агента сервер, переведённый в обслуживание, не должен остаться в
нём навсегда. Источник правды о строках — живой `show stat`: приведение
смотрит на текущий статус, поэтому reload HAProxy (новый воркер со всеми
серверами в работе) лечится тем же проходом, что и обычная отправка панели.
"""

import asyncio
import json
import logging
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Sequence

from app.config import get_settings
from app.services.haproxy_down_counter import RowKey, Target, get_down_counter
from app.services.haproxy_manager import get_haproxy_manager
from app.services.haproxy_runtime_weights import NO_CHECK_STATUS, PERMISSION_DENIED, SocketPermissionError
from app.services.loss_probe import parse_addr

logger = logging.getLogger(__name__)

RECONCILE_SECONDS = 5
STATE_FILE_NAME = "haproxy_quarantine.json"
MAINT_STATUS = "MAINT"


class SkipReason(str, Enum):
    LAST_SERVER = "last_server"
    SHARE_LIMIT = "share_limit"
    MANUAL_MAINT = "manual_maint"
    ERROR = "error"


@dataclass(frozen=True)
class AppliedRow:
    backend: str
    server: str
    # Соединений на строке в момент отключения; 0 — строка отключена раньше
    sessions: int


@dataclass(frozen=True)
class SkippedBackend:
    backend: str
    reason: SkipReason


@dataclass
class TargetResult:
    ip: str
    port: int
    # Адрес есть среди серверов HAProxy; False — отключать нечего
    found: bool
    applied: list[AppliedRow] = field(default_factory=list)
    skipped: list[SkippedBackend] = field(default_factory=list)


@dataclass
class _Plan:
    rows: dict[Target, list[tuple[str, dict]]] = field(default_factory=dict)
    skipped: dict[Target, list[SkippedBackend]] = field(default_factory=dict)
    found: set[Target] = field(default_factory=set)

    def row_keys(self) -> set[RowKey]:
        return {(backend, row["name"]) for rows in self.rows.values() for backend, row in rows}


def _in_maintenance(row: dict) -> bool:
    return (row.get("status") or "").startswith(MAINT_STATUS)


def _serves(row: dict) -> bool:
    status = row.get("status") or ""
    alive = status.startswith("UP") or status == NO_CHECK_STATUS
    return alive and not row.get("backup") and (row.get("weight") or 0) > 0


def _serving_left(servers: Sequence[dict], removed: set[Target]) -> bool:
    return any(_serves(row) and parse_addr(row.get("addr") or "") not in removed for row in servers)


def plan_quarantine(proxies: Sequence[dict], targets: Sequence[Target], max_share_pct: int) -> _Plan:
    """Какие адреса можно вывести в каждом backend. Адреса идут в порядке
    панели (худшие первыми): слотов меньше, чем желающих, — достаются первым."""
    plan = _Plan()
    for proxy in proxies:
        servers = proxy.get("servers") or []
        rows_by_target: dict[Target, list[dict]] = {}
        for row in servers:
            target = parse_addr(row.get("addr") or "")
            if target is not None:
                rows_by_target.setdefault(target, []).append(row)
        limit = len(rows_by_target) * max_share_pct // 100
        accepted: set[Target] = set()
        for target in targets:
            if target not in rows_by_target:
                continue
            plan.found.add(target)
            if len(accepted) >= limit:
                reason = SkipReason.SHARE_LIMIT
            elif not _serving_left(servers, accepted | {target}):
                reason = SkipReason.LAST_SERVER
            else:
                accepted.add(target)
                plan.rows.setdefault(target, []).extend((proxy["name"], row) for row in rows_by_target[target])
                continue
            plan.skipped.setdefault(target, []).append(SkippedBackend(proxy["name"], reason))
    return plan


class Quarantine:
    def __init__(
        self,
        run_command: Callable[[str], str],
        read_proxies: Callable[[], list[dict]],
        state_path: Path,
        clock: Callable[[], float] = time.time,
        on_maint: Callable[[RowKey], None] = lambda _row: None,
        on_release: Callable[[Target], None] = lambda _target: None,
    ):
        self._run_command = run_command
        self._read_proxies = read_proxies
        self._state_path = state_path
        self._clock = clock
        self._on_maint = on_maint
        self._on_release = on_release
        # Адрес → срок; порядок — порядок списка панели
        self._leases: dict[Target, float] = {}
        # Строки, которые перевёл в обслуживание этот менеджер, и их адрес
        self._owned: dict[RowKey, Target] = {}
        self._owned_view: frozenset[RowKey] = frozenset()
        self._max_share_pct = 0
        self._drop_sessions = False
        self._saved: Optional[str] = None
        self._failing = False
        # apply и фоновый круг идут из тред-пула одновременно
        self._lock = threading.Lock()
        self._task: Optional[asyncio.Task] = None
        self._load()

    def apply(self, targets: Sequence[Target], ttl_seconds: int, max_share_pct: int,
              drop_sessions: bool) -> list[TargetResult]:
        """Полный список отключённых адресов релея; срез show stat читается до
        любых изменений — мёртвый сокет не должен менять сроки."""
        with self._lock:
            proxies = self._read_proxies()
            now = self._clock()
            self._leases = {target: now + ttl_seconds for target in dict.fromkeys(targets)}
            self._max_share_pct = max_share_pct
            self._drop_sessions = drop_sessions
            return self._enforce(proxies, now)

    def reconcile(self) -> None:
        """Истёкшие сроки — вернуть; действующие — привести HAProxy к ним: после
        reload отключить заново, при упавшем соседе вернуть последний сервер."""
        with self._lock:
            if not self._leases and not self._owned:
                return
            self._enforce(self._read_proxies(), self._clock())

    def quarantined_rows(self) -> frozenset[RowKey]:
        """Снимок без замка: зовётся из цикла событий на каждый запрос статистики,
        а замок на время зависшего сокета держит фоновый круг."""
        return self._owned_view

    def _enforce(self, proxies: Sequence[dict], now: float) -> list[TargetResult]:
        self._leases = {target: deadline for target, deadline in self._leases.items() if deadline > now}
        try:
            plan = plan_quarantine(proxies, list(self._leases), self._max_share_pct)
            self._return_rows(proxies, plan.row_keys())
            return self._disable_rows(plan)
        finally:
            self._owned_view = frozenset(self._owned)
            self._save()

    def _return_rows(self, proxies: Sequence[dict], wanted: set[RowKey]) -> None:
        existing = {(proxy["name"], row["name"]) for proxy in proxies for row in proxy.get("servers") or []}
        returned: set[Target] = set()
        for row in [row for row in self._owned if row not in wanted]:
            if row in existing:
                # Ответ с ошибкой — сервера больше нет, держать его за собой незачем
                self._command(f"set server {row[0]}/{row[1]} state ready")
            returned.add(self._owned.pop(row))
        still_out = set(self._owned.values())
        for target in sorted(returned):
            if target not in self._leases and target not in still_out:
                self._on_release(target)

    def _disable_rows(self, plan: _Plan) -> list[TargetResult]:
        results = []
        for target in self._leases:
            result = TargetResult(target[0], target[1], target in plan.found,
                                  skipped=list(plan.skipped.get(target, [])))
            for backend, row in plan.rows.get(target, []):
                key = (backend, row["name"])
                outcome = self._disable_row(key, row, target)
                if isinstance(outcome, AppliedRow):
                    result.applied.append(outcome)
                elif SkippedBackend(backend, outcome) not in result.skipped:
                    result.skipped.append(SkippedBackend(backend, outcome))
            results.append(result)
        return results

    def _disable_row(self, key: RowKey, row: dict, target: Target) -> AppliedRow | SkipReason:
        backend, server = key
        if _in_maintenance(row):
            # Чужое обслуживание (disabled в конфиге, ручной disable server) не наше:
            # ни отключать повторно, ни возвращать его потом нельзя
            return AppliedRow(backend, server, 0) if key in self._owned else SkipReason.MANUAL_MAINT
        if self._command(f"set server {backend}/{server} state maint") is not None:
            return SkipReason.ERROR
        self._owned[key] = target
        self._on_maint(key)
        if self._drop_sessions:
            self._command(f"shutdown sessions server {backend}/{server}")
        return AppliedRow(backend, server, row.get("scur") or 0)

    def _command(self, command: str) -> Optional[str]:
        """None — успех; иначе текст ошибки HAProxy."""
        reply = self._run_command(command).strip()
        if PERMISSION_DENIED in reply:
            raise SocketPermissionError(reply)
        return reply or None

    def _serialized(self) -> str:
        return json.dumps({
            "max_share_pct": self._max_share_pct,
            "drop_sessions": self._drop_sessions,
            "leases": [{"ip": ip, "port": port, "deadline": deadline}
                       for (ip, port), deadline in self._leases.items()],
            "owned": [{"backend": backend, "server": server, "ip": ip, "port": port}
                      for (backend, server), (ip, port) in self._owned.items()],
        })

    def _save(self) -> None:
        state = self._serialized()
        if state == self._saved:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(state, encoding="utf-8")
            tmp.replace(self._state_path)
            self._saved = state
        except OSError as e:
            logger.error("Cannot save quarantine state %s: %s", self._state_path, e)

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            leases = {(str(item["ip"]), int(item["port"])): float(item["deadline"]) for item in data["leases"]}
            owned = {(str(item["backend"]), str(item["server"])): (str(item["ip"]), int(item["port"]))
                     for item in data["owned"]}
            max_share_pct, drop_sessions = int(data["max_share_pct"]), bool(data["drop_sessions"])
        except (OSError, ValueError, KeyError, TypeError) as e:
            logger.error("Quarantine state %s is unreadable, starting clean: %s", self._state_path, e)
            return
        self._leases, self._owned = leases, owned
        self._owned_view = frozenset(owned)
        self._max_share_pct, self._drop_sessions = max_share_pct, drop_sessions
        self._saved = self._serialized()

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
            await asyncio.sleep(RECONCILE_SECONDS)
            try:
                await asyncio.to_thread(self.reconcile)
            except (OSError, SocketPermissionError) as e:
                # Сокет молчит (reload, HAProxy остановлен): сроки ждут следующего круга
                if not self._failing:
                    logger.warning("Quarantine round skipped, will retry: %s", e)
                self._failing = True
                continue
            except Exception:
                # Круг не должен умереть: без него серверы некому вернуть по сроку
                logger.exception("Quarantine round failed, will retry")
                continue
            self._failing = False


_quarantine: Optional[Quarantine] = None


def get_quarantine() -> Quarantine:
    global _quarantine
    if _quarantine is None:
        manager = get_haproxy_manager()
        counter = get_down_counter()
        _quarantine = Quarantine(
            manager.runtime_command,
            manager.read_live_proxies,
            Path(get_settings().traffic_db_path).parent / STATE_FILE_NAME,
            on_maint=counter.skip_next,
            on_release=counter.reset,
        )
    return _quarantine
