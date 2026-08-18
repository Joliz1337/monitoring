"""Искусственный лимит полосы интерфейса — шейпер `tc` на хосте.

Ограничивается исходящее направление дефолтного интерфейса; на релее это оба
направления транзита (к цели и обратно к клиенту оба выходят через него), а
хостер на своих счётчиках видит ровную полку вместо пиков. Предпочитается
`cake` (честное деление по потокам, короткая очередь), при отсутствии модуля
`sch_cake` — `tbf`. Ниже лимита шейпер невидим: очередь пуста, задержки нет.

Состояние — в `/opt/monitoring/configs/bandwidth-limit.env` на хосте, чтобы
пережить перезапуск агента и перезагрузку; агент переприменяет лимит при
старте и подтверждает раз в BANDWIDTH_RECHECK_INTERVAL_SEC — `ethtool -L`
из тюнинга или ручное `tc qdisc del` сбрасывают корневой qdisc.
"""

import asyncio
import json
import logging
import shlex
from pathlib import Path
from typing import Optional

from app.services.cpu_affinity import default_interface
from app.services.host_executor import HostExecutor
from app.services.host_files import write_host_file

logger = logging.getLogger(__name__)

STATE_FILE = Path("/opt/monitoring/configs/bandwidth-limit.env")
KEY_MBIT = "BANDWIDTH_LIMIT_MBIT"
KEY_IFACE = "BANDWIDTH_LIMIT_IFACE"

MIN_MBIT = 1
MAX_MBIT = 100_000
BANDWIDTH_RECHECK_INTERVAL_SEC = 120
TC_TIMEOUT_SEC = 15

QDISC_CAKE = "cake"
QDISC_TBF = "tbf"


def render_state(mbit: int, iface: str) -> str:
    return f"{KEY_MBIT}={mbit}\n{KEY_IFACE}={iface}\n"


def parse_state(text: str) -> tuple[int, str]:
    """(мбит, интерфейс); 0 — лимит выключен."""
    mbit, iface = 0, ""
    for line in (text or "").splitlines():
        name, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip().strip("\"'")
        if name.strip() == KEY_MBIT and value.isdigit():
            mbit = int(value)
        elif name.strip() == KEY_IFACE:
            iface = value
    return mbit, iface


def parse_tc_root(tc_json: str) -> tuple[Optional[str], Optional[int]]:
    """Корневой qdisc из `tc -j qdisc show dev X`: (вид, лимит в Мбит) — лимит
    только для наших видов (cake отдаёт bandwidth в бит/с, tbf — rate в бит/с)."""
    try:
        entries = json.loads(tc_json or "[]")
    except ValueError:
        return None, None
    for entry in entries:
        if entry.get("root") is not True and entry.get("parent") is not None:
            continue
        kind = entry.get("kind")
        options = entry.get("options") or {}
        bits = options.get("bandwidth") if kind == QDISC_CAKE else options.get("rate") if kind == QDISC_TBF else None
        mbit = round(int(bits) / 1_000_000) if isinstance(bits, (int, float)) and bits else None
        return kind, mbit
    return None, None


class BandwidthLimiter:
    def __init__(self, executor: HostExecutor):
        self._executor = executor
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    # ── чтение ──

    async def desired(self) -> tuple[int, str]:
        result = await self._executor.execute(f"cat {STATE_FILE}", timeout=5)
        if not (result.success and result.exit_code == 0):
            return 0, ""
        return parse_state(result.stdout)

    async def applied(self, iface: str) -> tuple[Optional[str], Optional[int]]:
        result = await self._executor.execute(f"tc -j qdisc show dev {shlex.quote(iface)}", timeout=TC_TIMEOUT_SEC)
        if not (result.success and result.exit_code == 0):
            return None, None
        return parse_tc_root(result.stdout)

    async def state(self) -> dict:
        mbit, iface = await self.desired()
        iface = iface or default_interface() or ""
        kind, applied_mbit = (await self.applied(iface)) if iface else (None, None)
        limited = kind in (QDISC_CAKE, QDISC_TBF)
        return {
            "enabled": mbit > 0,
            "mbit": mbit,
            "iface": iface,
            "applied": limited,
            "applied_mbit": applied_mbit if limited else None,
            "qdisc": kind if limited else None,
            # расхождение: хотим лимит, а корневой qdisc не наш или не с той полосой
            "in_sync": (not mbit and not limited) or (limited and applied_mbit == mbit),
        }

    # ── запись ──

    async def set_limit(self, mbit: int) -> dict:
        """0 — снять лимит. Состояние пишется на хост до применения: если tc
        упадёт, самолечение повторит попытку, а не потеряет намерение."""
        async with self._lock:
            iface = default_interface()
            if not iface:
                return {"success": False, "message": "cannot detect default interface"}
            written = await write_host_file(str(STATE_FILE), render_state(mbit, iface), mode="644")
            if not written:
                return {"success": False, "message": "cannot write bandwidth-limit state on host"}
            if mbit <= 0:
                await self._remove(iface)
                return {"success": True, "message": "Bandwidth limit removed"}
            error = await self._apply(iface, mbit)
            if error:
                return {"success": False, "message": error}
            return {"success": True, "message": f"Bandwidth limit {mbit} Mbit/s applied on {iface}"}

    async def _apply(self, iface: str, mbit: int) -> Optional[str]:
        dev = shlex.quote(iface)
        cake = await self._executor.execute(
            f"tc qdisc replace dev {dev} root cake bandwidth {mbit}mbit besteffort", timeout=TC_TIMEOUT_SEC,
        )
        if cake.success and cake.exit_code == 0:
            return None
        # sch_cake отсутствует (старое/урезанное ядро) — tbf есть везде
        tbf = await self._executor.execute(
            f"tc qdisc replace dev {dev} root tbf rate {mbit}mbit burst 512kb latency 50ms", timeout=TC_TIMEOUT_SEC,
        )
        if tbf.success and tbf.exit_code == 0:
            logger.warning("bandwidth limit: cake unavailable on %s, using tbf (%s)", iface, cake.stderr or cake.error)
            return None
        return f"tc failed: {tbf.stderr or tbf.error or cake.stderr or cake.error or 'unknown error'}"

    async def _remove(self, iface: str) -> None:
        # Снимаем только свой шейпер: чужой корневой qdisc (mq/fq_codel хоста) не трогаем
        kind, _ = await self.applied(iface)
        if kind in (QDISC_CAKE, QDISC_TBF):
            await self._executor.execute(f"tc qdisc del dev {shlex.quote(iface)} root", timeout=TC_TIMEOUT_SEC)

    async def ensure(self) -> Optional[str]:
        """Вернуть лимит, если он задан, а корневой qdisc сброшен. None — чинить нечего."""
        async with self._lock:
            mbit, iface = await self.desired()
            if mbit <= 0 or not iface:
                return None
            kind, applied_mbit = await self.applied(iface)
            if kind in (QDISC_CAKE, QDISC_TBF) and applied_mbit == mbit:
                return None
            error = await self._apply(iface, mbit)
            if error:
                logger.error("bandwidth limit self-heal failed: %s", error)
                return f"self-heal failed: {error}"
            return f"re-applied {mbit} Mbit/s on {iface}"

    # ── фон ──

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        action = await self.ensure()
        if action:
            logger.info("bandwidth limit at startup: %s", action)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(BANDWIDTH_RECHECK_INTERVAL_SEC)
            try:
                action = await self.ensure()
                if action:
                    logger.warning("bandwidth limit: %s", action)
            except Exception as exc:
                logger.error("bandwidth limit recheck failed: %s", exc, exc_info=True)


_limiter: Optional[BandwidthLimiter] = None


def get_bandwidth_limiter() -> BandwidthLimiter:
    global _limiter
    if _limiter is None:
        from app.services.host_executor import get_host_executor
        _limiter = BandwidthLimiter(get_host_executor())
    return _limiter
