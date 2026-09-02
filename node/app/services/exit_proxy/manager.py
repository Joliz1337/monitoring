"""Exit-прокси: SOCKS5 на loopback, пул исходящих IP ноды и автономный выбор выхода.

Xray Remnawave на том же хосте шлёт сюда Google-трафик. Нода сама находит свои
IPv4 (основной и добавленные панелью) и WARP, по расписанию проверяет, как Google
видит каждый выход, и держит трафик на здоровом: выход меняется только когда
текущий «заболел» — или по команде. Панель лишь присылает конфиг и читает
статус; без неё прокси продолжает работать и переключаться.

Состояние — exit_proxy.json рядом с БД трафика (том агента, переживает рестарт
контейнера). Проверки идут curl'ом с хоста: host_check.sh уезжает в
/opt/monitoring/scripts/ при расхождении sha256.
"""

import asyncio
import base64
import hashlib
import json
import logging
import shlex
from collections import deque
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Awaitable, Callable, Optional

from app.config import get_settings
from app.services.exit_proxy.models import (
    WARP_SOCKS_HOST,
    WARP_SOCKS_PORT,
    Candidate,
    CandidateStatus,
    CheckResult,
    ExitEvent,
    ExitProxyConfig,
    ExitProxyStatus,
    ProxyStats,
    SelfTest,
)
from app.services.exit_proxy.selection import (
    REASON_NO_HEALTHY,
    Decision,
    DiscoveredIp,
    choose_exit,
    health,
    merge_candidates,
)
from app.services.exit_proxy.socks_server import Connector, SocksServer, connect_direct, connect_via_socks
from app.services.host_executor import HostExecutor
from app.services.host_files import write_host_file

logger = logging.getLogger(__name__)

STATE_FILE_NAME = "exit_proxy.json"
HOST_SCRIPT = Path(__file__).with_name("host_check.sh").read_text(encoding="utf-8")
HOST_SCRIPT_PATH = "/opt/monitoring/scripts/exit-proxy-check.sh"
EVENTS_LIMIT = 200
STATUS_EVENTS = 20
CHECK_CONCURRENCY = 4
# Один прогон — до семи запросов по check_timeout каждый, плюс запас на запуск curl
PROBE_REQUESTS = 7
PROBE_GRACE_SEC = 15
SELFTEST_TIMEOUT_SEC = 20
WARP_PROBE_TIMEOUT_SEC = 0.3
# Первая проверка после старта агента — не сразу: сеть хоста и WARP ещё поднимаются
STARTUP_DELAY_SEC = 20
FIELD_SEPARATOR = "\x1f"

EVENT_SWITCHED = "switched"
EVENT_MANUAL_SWITCH = "manual_switch"
EVENT_NO_HEALTHY = "no_healthy"
EVENT_RECOVERED = "recovered"
EVENT_STARTED = "started"
EVENT_STOPPED = "stopped"
EVENT_CHECK_FAILED = "check_failed"

DiscoverIps = Callable[[], Awaitable[list[DiscoveredIp]]]
WarpProbe = Callable[[], Awaitable[bool]]


class ExitProxyError(Exception):
    pass


class ExitProxyBusyError(ExitProxyError):
    pass


class ExitProxyValidationError(ExitProxyError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def discover_interface_ips() -> list[DiscoveredIp]:
    """IPv4 default-интерфейса — тот же источник, что у карточки «Сетевые адреса»."""
    from app.services.extra_ips import get_extra_ip_manager

    state = await get_extra_ip_manager().state()
    interface = next((item for item in state.interfaces if item.is_default), None)
    if interface is None and state.interfaces:
        interface = state.interfaces[0]
    if interface is None:
        return []
    return [
        DiscoveredIp(address=addr.address, primary=addr.primary, managed=addr.managed)
        for addr in interface.addresses
        if addr.family == "ipv4" and addr.scope == "global"
    ]


async def warp_socks_present() -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(WARP_SOCKS_HOST, WARP_SOCKS_PORT), WARP_PROBE_TIMEOUT_SEC,
        )
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    return True


class ExitProxyManager:
    def __init__(
        self,
        executor: HostExecutor,
        state_path: Optional[Path] = None,
        discover_ips: DiscoverIps = discover_interface_ips,
        warp_probe: WarpProbe = warp_socks_present,
    ):
        self._executor = executor
        self._state_path = state_path or Path(get_settings().traffic_db_path).parent / STATE_FILE_NAME
        self._discover_ips = discover_ips
        self._warp_probe = warp_probe

        self.config = ExitProxyConfig()
        self.candidates: list[Candidate] = []
        self.results: dict[str, CheckResult] = {}
        self.current: Optional[str] = None
        self.self_test: Optional[SelfTest] = None
        self.last_check_at: Optional[str] = None
        self.last_check_error: Optional[str] = None
        self.listen_error: Optional[str] = None

        self._events: deque[ExitEvent] = deque(maxlen=EVENTS_LIMIT)
        self._discovered: list[DiscoveredIp] = []
        self._warp_present = False
        self._server: Optional[SocksServer] = None
        self._task: Optional[asyncio.Task] = None
        self._background: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._check_lock = asyncio.Lock()
        self._check_requested = False
        self._installed_hash: Optional[str] = None
        self._no_healthy = False

    # ── состояние на диске ──

    def load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            self.config = ExitProxyConfig(**data.get("config", {}))
            self.candidates = [Candidate(**item) for item in data.get("candidates", [])]
            self.results = {key: CheckResult(**value) for key, value in data.get("results", {}).items()}
            self.current = data.get("current")
            self.self_test = SelfTest(**data["self_test"]) if data.get("self_test") else None
            self.last_check_at = data.get("last_check_at")
            self.last_check_error = data.get("last_check_error")
            self._events = deque((ExitEvent(**item) for item in data.get("events", [])), maxlen=EVENTS_LIMIT)
            self._discovered = [DiscoveredIp(**item) for item in data.get("discovered", [])]
            self._warp_present = bool(data.get("warp_present"))
            self._no_healthy = bool(data.get("no_healthy"))
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Exit proxy state %s is unreadable, starting clean: %s", self._state_path, exc)
            self.config = ExitProxyConfig()
            self.candidates, self.results, self.current = [], {}, None

    def _save_state(self) -> None:
        payload = {
            "config": self.config.model_dump(),
            "candidates": [candidate.model_dump() for candidate in self.candidates],
            "results": {key: value.model_dump() for key, value in self.results.items()},
            "current": self.current,
            "self_test": self.self_test.model_dump() if self.self_test else None,
            "last_check_at": self.last_check_at,
            "last_check_error": self.last_check_error,
            "events": [event.model_dump() for event in self._events],
            "discovered": [ip.__dict__ for ip in self._discovered],
            "warp_present": self._warp_present,
            "no_healthy": self._no_healthy,
            "saved_at": _now(),
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._state_path)
        except OSError as exc:
            logger.error("Cannot save exit proxy state %s: %s", self._state_path, exc)

    # ── жизненный цикл ──

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self.load_state()
        if self.config.enabled:
            await self._ensure_server()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        for task in (self._task, self._background):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._task = None
        self._background = None
        await self._stop_server()

    async def _loop(self) -> None:
        delay: float = STARTUP_DELAY_SEC
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self.config.enabled:
                try:
                    await self.run_checks()
                except ExitProxyBusyError:
                    pass
                except Exception as exc:  # noqa: BLE001 — цикл обязан пережить любой сбой проверки
                    logger.error("Exit proxy check cycle failed: %s", exc, exc_info=True)
                    self.last_check_error = str(exc)
            delay = self.config.interval_minutes * 60

    async def _ensure_server(self) -> None:
        if self._server and self._server.listening and self._server.port == self.config.port:
            return
        await self._stop_server()
        server = SocksServer(self.config.port, self._route)
        try:
            await server.start()
        except OSError as exc:
            self.listen_error = f"cannot listen on 127.0.0.1:{self.config.port}: {exc}"
            logger.error("Exit proxy: %s", self.listen_error)
            return
        self._server = server
        self.listen_error = None

    async def _stop_server(self) -> None:
        server, self._server = self._server, None
        if server:
            await server.stop()

    def _route(self) -> Optional[tuple[str, Connector]]:
        current = self.current
        candidate = self._candidate(current) if current else None
        if candidate is None or not candidate.enabled:
            return None
        if candidate.kind == "warp":
            host, port = candidate.address.rsplit(":", 1)
            return candidate.id, partial(connect_via_socks, proxy_host=host, proxy_port=int(port))
        return candidate.id, partial(connect_direct, bind_ip=candidate.address)

    def _candidate(self, candidate_id: Optional[str]) -> Optional[Candidate]:
        return next((candidate for candidate in self.candidates if candidate.id == candidate_id), None)

    # ── конфиг от панели ──

    async def apply_config(self, config: ExitProxyConfig) -> ExitProxyStatus:
        previous = self.config
        self.config = config
        if config.enabled and not self._discovered:
            # Первый прогон проверок займёт минуту; выход нужен сразу — основной IP
            # выбирается по одному лишь списку адресов, проверки уточнят его позже
            await self._discover()
        self.candidates = merge_candidates(
            self._discovered, self._warp_present, config.candidates_order, config.candidates_disabled,
        )
        if config.enabled:
            await self._ensure_server()
            if not previous.enabled:
                self._event(EVENT_STARTED, reason="enabled by panel")
        else:
            await self._stop_server()
            self.listen_error = None
            if previous.enabled:
                self._event(EVENT_STOPPED, reason="disabled by panel")

        # Чёрный список стран, порядок, отключённые кандидаты или pin —
        # пересчитываются по уже имеющимся результатам без новых запросов
        self._reselect()
        checks_changed = (
            previous.custom_checks != config.custom_checks
            or previous.builtin_checks != config.builtin_checks
            or previous.check_timeout != config.check_timeout
        )
        if config.enabled and (checks_changed or not self.results or not previous.enabled):
            self._check_requested = True
            self._wake.set()
        self._save_state()
        return self.status()

    # ── проверки ──

    @property
    def check_in_progress(self) -> bool:
        return self._check_lock.locked() or self._check_requested

    def start_check(self) -> bool:
        """Прогон в фоне по команде панели; False — уже идёт."""
        if self.check_in_progress:
            return False
        self._check_requested = True
        self._background = asyncio.create_task(self._background_check())
        return True

    async def _background_check(self) -> None:
        try:
            await self.run_checks()
        except ExitProxyBusyError:
            pass
        except Exception as exc:  # noqa: BLE001 — фоновая задача, ошибка уходит в статус
            logger.error("Exit proxy on-demand check failed: %s", exc, exc_info=True)
            self.last_check_error = str(exc)

    async def run_checks(self) -> None:
        if self._check_lock.locked():
            raise ExitProxyBusyError("check already in progress")
        async with self._check_lock:
            self._check_requested = False
            await self._run_checks_locked()

    async def _discover(self) -> None:
        try:
            self._discovered = await self._discover_ips()
            self._warp_present = await self._warp_probe()
        except Exception as exc:  # noqa: BLE001 — список адресов не критичен для применения конфига
            logger.warning("Exit proxy: address discovery failed: %s", exc)

    async def _run_checks_locked(self) -> None:
        try:
            await self._ensure_script()
            self._discovered = await self._discover_ips()
            self._warp_present = await self._warp_probe()
        except Exception as exc:  # noqa: BLE001 — любой сбой подготовки = проверки не было
            self.last_check_error = f"discovery failed: {exc}"
            self._event(EVENT_CHECK_FAILED, reason=self.last_check_error)
            self._save_state()
            return

        self.candidates = merge_candidates(
            self._discovered, self._warp_present, self.config.candidates_order, self.config.candidates_disabled,
        )
        semaphore = asyncio.Semaphore(CHECK_CONCURRENCY)

        async def probe(candidate: Candidate) -> tuple[str, CheckResult]:
            async with semaphore:
                return candidate.id, await self._probe(candidate)

        outcomes = await asyncio.gather(*(probe(candidate) for candidate in self.candidates), return_exceptions=True)
        errors: list[str] = []
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                errors.append(str(outcome))
                continue
            candidate_id, result = outcome
            self.results[candidate_id] = result
        live_ids = {candidate.id for candidate in self.candidates}
        self.results = {key: value for key, value in self.results.items() if key in live_ids}
        self.last_check_at = _now()
        self.last_check_error = "; ".join(errors) or None

        self._reselect()
        if self.config.enabled:
            await self._self_test()
        self._save_state()

    async def _ensure_script(self) -> None:
        digest = hashlib.sha256(HOST_SCRIPT.encode("utf-8")).hexdigest()
        if self._installed_hash == digest:
            return
        on_host = await self._executor.execute(
            f"sha256sum {HOST_SCRIPT_PATH} 2>/dev/null | cut -d' ' -f1", timeout=10, shell="bash",
        )
        if on_host.stdout.strip() != digest:
            if not await write_host_file(HOST_SCRIPT_PATH, HOST_SCRIPT, mode="755"):
                raise ExitProxyError("cannot install exit-proxy-check.sh on the host")
        self._installed_hash = digest

    def _check_payload(self) -> str:
        builtin = self.config.builtin_checks
        lines = [FIELD_SEPARATOR.join([
            "BUILTIN",
            "1" if builtin.google_country else "0",
            "1" if builtin.google_captcha else "0",
            "1" if builtin.gemini else "0",
        ])]
        for check in self.config.custom_checks:
            if not check.enabled:
                continue
            lines.append(FIELD_SEPARATOR.join([
                "CHECK", check.name, check.url, ",".join(str(code) for code in check.block_status),
                check.block_regex, check.block_url_regex, str(check.expect_status or ""),
            ]))
        return "\n".join(lines) + "\n"

    async def _probe(self, candidate: Candidate) -> CheckResult:
        payload = base64.b64encode(self._check_payload().encode("utf-8")).decode("ascii")
        timeout = self.config.check_timeout
        command = f"{HOST_SCRIPT_PATH} probe {candidate.kind} {shlex.quote(candidate.address)} {timeout} {payload}"
        result = await self._executor.execute(
            command, timeout=timeout * PROBE_REQUESTS + PROBE_GRACE_SEC, shell="bash",
        )
        checked_at = _now()
        stdout = result.stdout.strip()
        line = stdout.splitlines()[-1] if stdout else ""
        if not result.success or not line:
            detail = (result.stderr or result.error or "").strip() or f"exit code {result.exit_code}"
            return CheckResult(ok=False, error=f"probe failed: {detail[:200]}", checked_at=checked_at)
        try:
            return CheckResult(**json.loads(line), checked_at=checked_at)
        except (ValueError, TypeError) as exc:
            return CheckResult(ok=False, error=f"unreadable probe output: {exc}", checked_at=checked_at)

    async def _self_test(self) -> None:
        if not self._server or not self._server.listening:
            self.self_test = SelfTest(ok=False, at=_now(), error=self.listen_error or "proxy is not listening")
            return
        candidate = self._candidate(self.current)
        if candidate is None:
            self.self_test = SelfTest(ok=False, at=_now(), error="no exit selected")
            return
        result = await self._executor.execute(
            f"{HOST_SCRIPT_PATH} selftest {self.config.port} {SELFTEST_TIMEOUT_SEC}",
            timeout=SELFTEST_TIMEOUT_SEC + 10, shell="bash",
        )
        try:
            data = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
        except ValueError:
            data = {}
        if not data.get("ok"):
            self.self_test = SelfTest(ok=False, at=_now(), error=data.get("error") or "self-test did not run")
            return

        seen_ip, seen_warp = data.get("ip"), data.get("warp")
        if candidate.kind == "warp":
            expected, ok = "warp=on", seen_warp == "on"
        else:
            # Ожидаем то, что Google видел через этот выход: за NAT адрес интерфейса и внешний различаются
            probe = self.results.get(candidate.id)
            expected = (probe.ip if probe and probe.ip else candidate.address)
            ok = seen_ip == expected
        self.self_test = SelfTest(
            ok=ok, ip=seen_ip, warp=seen_warp, expected=expected, at=_now(),
            error=None if ok else "traffic leaves through an unexpected exit",
        )

    # ── выбор выхода ──

    def _health_map(self) -> dict[str, Optional[bool]]:
        return {
            candidate.id: health(self.results.get(candidate.id), self.config.blocked_countries, self.config.builtin_checks)
            for candidate in self.candidates
        }

    def _reselect(self) -> None:
        decision = choose_exit(
            self.candidates, self._health_map(), self.current, self.config.select_mode, self.config.pinned_candidate,
        )
        self._apply_decision(decision)

    def _apply_decision(self, decision: Decision) -> None:
        previous = self.current
        switched = decision.candidate != previous
        if switched:
            self.current = decision.candidate
            dropped = self._server.drop_connections(except_exit=self.current) if self._server else 0
            self._event(
                EVENT_SWITCHED, from_candidate=previous, to_candidate=self.current,
                reason=decision.reason, dropped_connections=dropped,
            )
            logger.info(
                "Exit proxy: exit %s -> %s (%s), dropped %s connections",
                previous, self.current, decision.reason, dropped,
            )
        no_healthy = decision.reason == REASON_NO_HEALTHY
        if no_healthy and not self._no_healthy:
            self._event(EVENT_NO_HEALTHY, to_candidate=self.current, reason="every candidate failed its checks; first by priority is in use")
        elif self._no_healthy and not no_healthy and not switched:
            self._event(EVENT_RECOVERED, to_candidate=self.current, reason=decision.reason)
        self._no_healthy = no_healthy

    async def switch(self, candidate_id: str) -> ExitProxyStatus:
        candidate = self._candidate(candidate_id)
        if candidate is None:
            raise ExitProxyValidationError(f"unknown candidate '{candidate_id}'")
        if not candidate.enabled:
            raise ExitProxyValidationError(f"candidate '{candidate_id}' is disabled")
        if self.config.select_mode == "manual":
            self.config = self.config.model_copy(update={"pinned_candidate": candidate_id})
        previous = self.current
        if previous != candidate_id:
            self.current = candidate_id
            dropped = self._server.drop_connections(except_exit=candidate_id) if self._server else 0
            self._event(
                EVENT_MANUAL_SWITCH, from_candidate=previous, to_candidate=candidate_id,
                reason="switched by operator", dropped_connections=dropped,
            )
            logger.info("Exit proxy: manual switch %s -> %s, dropped %s connections", previous, candidate_id, dropped)
        if self.config.enabled:
            await self._self_test()
        self._save_state()
        return self.status()

    # ── статус ──

    def _event(self, kind: str, **fields) -> None:
        self._events.append(ExitEvent(at=_now(), kind=kind, **fields))

    def events(self, limit: int) -> list[ExitEvent]:
        return list(reversed(self._events))[:limit]

    def status(self) -> ExitProxyStatus:
        health_by_id = self._health_map()
        server = self._server
        return ExitProxyStatus(
            enabled=self.config.enabled,
            listening=bool(server and server.listening),
            listen_error=self.listen_error,
            port=self.config.port,
            current=self.current,
            select_mode=self.config.select_mode,
            pinned_candidate=self.config.pinned_candidate,
            candidates=[
                CandidateStatus(
                    **candidate.model_dump(), healthy=health_by_id.get(candidate.id),
                    last_check=self.results.get(candidate.id),
                )
                for candidate in self.candidates
            ],
            warp_present=self._warp_present,
            check_in_progress=self.check_in_progress,
            last_check_at=self.last_check_at,
            last_check_error=self.last_check_error,
            self_test=self.self_test,
            stats=ProxyStats(
                active_connections=server.active_connections if server else 0,
                total_connections=server.total_connections if server else 0,
                failed_connections=server.failed_connections if server else 0,
            ),
            events=list(reversed(self._events))[:STATUS_EVENTS],
            script_installed=self._installed_hash is not None,
        )


_manager: Optional[ExitProxyManager] = None


def get_exit_proxy_manager() -> ExitProxyManager:
    global _manager
    if _manager is None:
        from app.services.host_executor import get_host_executor
        _manager = ExitProxyManager(get_host_executor())
    return _manager
