"""Запуск прокси-ядра и снятие метрик через него.

Процесс ядра — самое опасное место раздела: на большой подписке их сотни, и
каждый забытый живёт вечно, занимая CPU и порт. Поэтому три рубежа уборки:
finally у каждой ячейки, реестр живых процессов с добиванием на shutdown и
периодический сборщик по возрасту.

Ядро запускается в своей process group (start_new_session), чтобы убивать всю
группу целиком: xray порождает дочерние процессы, и kill по одному pid оставил
бы их сиротами.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol as TypingProtocol

from app.services.xray_test import core_manager, core_output, probes
from app.services.xray_test.config_builder import build_config
from app.services.xray_test.errors import CoreDownloadError, UnsupportedConfigError
from app.services.xray_test.models import (
    CellResult,
    Core,
    FailReason,
    ProbeTimings,
    Security,
    TestCell,
    Verdict,
)
from app.services.xray_test.sanitize import sanitize_output

logger = logging.getLogger(__name__)

WORK_DIR = Path("/app/data/xray-test/run")
MAX_CONCURRENT_CORES = 32
CORE_START_TIMEOUT = 5.0
CELL_TIMEOUT = 40.0
CORE_MAX_LIFETIME = 90.0
SWEEP_INTERVAL = 30.0
TERM_GRACE = 3.0
READY_POLL_INTERVAL = 0.05
CORE_LOG_TAIL = 400
CORE_LOG_LINES = 40

_core_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CORES)
_live_processes: dict[asyncio.subprocess.Process, float] = {}
_sweeper_task: Optional[asyncio.Task] = None


class CoreRunner(TypingProtocol):
    """Общий контракт для прогона на панели и на ноде."""

    async def probe(self, cell: TestCell, options: probes.ProbeOptions) -> CellResult:
        ...


@dataclass
class _LaunchedCore:
    process: asyncio.subprocess.Process
    port: int
    workdir: tempfile.TemporaryDirectory
    started_at: float = field(default_factory=time.monotonic)
    # Вывод копится фоном: причина отказа появляется в нём уже после старта,
    # а читать поток по факту провала поздно — ядро к тому моменту убито
    output: list[str] = field(default_factory=list)
    reader: Optional[asyncio.Task] = None


class LocalCoreRunner:
    """Прогон на самой панели."""

    async def probe(self, cell: TestCell, options: probes.ProbeOptions) -> CellResult:
        endpoint = cell.endpoint
        result = _empty_result(cell)

        dns = await probes.resolve_address(endpoint.address)
        result.resolved_ip = dns.ip
        result.timings.dns_ms = dns.elapsed_ms
        if dns.error:
            return _fail(result, FailReason.DNS_FAIL, dns.error)

        if options.tcp and not endpoint.is_udp_protocol:
            tcp = await probes.tcp_ping(endpoint.address, endpoint.port, options.attempts)
            result.timings.tcp_min_ms = tcp.min_ms
            result.timings.tcp_avg_ms = tcp.avg_ms
            result.timings.tcp_jitter_ms = tcp.jitter_ms
            if not tcp.alive:
                return _fail(result, tcp.reason or FailReason.TCP_REFUSED, tcp.error or "")

        if options.tls_inspect and endpoint.tls.security.value != "none":
            result.tls_info = await probes.inspect_tls(
                endpoint.address, endpoint.port, endpoint.effective_sni
            )

        if not options.http:
            result.verdict = Verdict.OK
            return result

        try:
            core = core_manager.select_core(endpoint)
        except UnsupportedConfigError as exc:
            return _fail(result, FailReason.UNSUPPORTED, str(exc))
        result.core = core.value

        try:
            binary = await core_manager.ensure_core(core)
        except CoreDownloadError as exc:
            return _fail(result, FailReason.CORE_START_FAILED, str(exc))

        async with _core_semaphore:
            return await self._probe_through_core(cell, options, result, core, binary)

    async def _probe_through_core(
        self,
        cell: TestCell,
        options: probes.ProbeOptions,
        result: CellResult,
        core: Core,
        binary: Path,
    ) -> CellResult:
        launched: Optional[_LaunchedCore] = None
        try:
            launched = await self._launch(cell, core, binary)
        except _CoreStartError as exc:
            return await self._explain(cell, _fail(result, exc.reason, exc.detail, exc.hint))

        try:
            async with asyncio.timeout(CELL_TIMEOUT):
                return await self._explain(
                    cell, await self._run_probes(launched, options, result)
                )
        except asyncio.TimeoutError:
            core_detail, hint = _core_reason(launched)
            return await self._explain(cell, _fail(
                result, FailReason.HTTP_TIMEOUT,
                core_detail or "проверка не уложилась в отведённое время", hint,
            ))
        finally:
            await _shutdown_core(launched)

    async def _explain(self, cell: TestCell, result: CellResult) -> CellResult:
        """Отделить блокировку по пути от неподходящих параметров ключа.

        У REALITY есть запасной ход: «неправильному» клиенту сервер отдаёт
        настоящий сайт-маскировку. Значит живой и достижимый сервер обязан
        ответить на обычное TLS-рукопожатие с его SNI — а если молчит даже оно
        при живом TCP-порте, соединение душат по пути, и дело не в ключе.
        """
        if result.verdict is not Verdict.FAIL or result.timings.tcp_min_ms is None:
            return result
        if cell.endpoint.tls.security is Security.NONE:
            return result

        tls = result.tls_info
        if tls is None:
            tls = await probes.inspect_tls(
                cell.endpoint.address, cell.endpoint.port, cell.endpoint.effective_sni
            )
            result.tls_info = tls

        if tls.reachable:
            # Сервер жив и отвечает маскировкой — значит не подходят параметры
            result.hint = result.hint or "KEY_PARAMS"
            return result

        result.reason = FailReason.DPI_BLOCK
        result.hint = "DPI_BLOCK"
        if tls.error:
            result.detail = (result.detail or tls.error)[:CORE_LOG_TAIL]
        return result

    async def _run_probes(
        self, launched: _LaunchedCore, options: probes.ProbeOptions, result: CellResult
    ) -> CellResult:
        http = await probes.http_through_proxy(launched.port, options.extra_headers)
        result.http_status = http.status
        result.timings.handshake_ms = http.handshake_ms
        result.timings.rtt_ms = http.rtt_ms

        if http.reason is not None:
            # Текст исключения клиента часто пуст, а настоящая причина — в логе
            # ядра: «certificate is valid for …, not …», «connection refused»
            core_detail, hint = _core_reason(launched)
            detail = core_detail or http.error or ""
            if launched.process.returncode:
                detail = f"{detail} (ядро завершилось с кодом {launched.process.returncode})".strip()
            return _fail(result, http.reason, detail, hint)

        if options.exit_identity:
            identity = await probes.exit_identity(launched.port)
            result.exit_ip = identity.ip
            result.exit_country = identity.country
            result.exit_asn = identity.asn

        if options.speed:
            result.timings.speed_mbps = await probes.download_speed(launched.port)

        return _apply_verdict(result, options)

    async def _launch(self, cell: TestCell, core: Core, binary: Path) -> _LaunchedCore:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        workdir = tempfile.TemporaryDirectory(dir=str(WORK_DIR), prefix="cell-")
        port = _free_port()

        config = build_config(cell.endpoint, core, port)
        config_path = Path(workdir.name) / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        config_path.chmod(0o600)

        try:
            process = await asyncio.create_subprocess_exec(
                str(binary), "run", "-c", str(config_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=workdir.name,
                start_new_session=True,
            )
        except OSError as exc:
            workdir.cleanup()
            raise _CoreStartError(FailReason.CORE_START_FAILED, str(exc)) from exc

        _live_processes[process] = time.monotonic()
        launched = _LaunchedCore(process=process, port=port, workdir=workdir)
        launched.reader = asyncio.create_task(_pump_output(launched))

        if not await _wait_ready(launched):
            detail, hint = _core_reason(launched)
            await _shutdown_core(launched)
            reason = (
                FailReason.CORE_CRASHED if process.returncode is not None
                else FailReason.CORE_START_FAILED
            )
            raise _CoreStartError(
                reason, detail or "ядро не открыло локальный порт", hint
            )
        return launched


class _CoreStartError(Exception):
    def __init__(self, reason: FailReason, detail: str, hint: Optional[str] = None) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.hint = hint


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_ready(launched: _LaunchedCore) -> bool:
    deadline = time.monotonic() + CORE_START_TIMEOUT
    while time.monotonic() < deadline:
        if launched.process.returncode is not None:
            return False
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", launched.port)
        except OSError:
            await asyncio.sleep(READY_POLL_INTERVAL)
            continue
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return True
    return False


async def _pump_output(launched: _LaunchedCore) -> None:
    """Копить вывод ядра построчно, храня только хвост."""
    stream = launched.process.stdout
    if stream is None:
        return
    try:
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text or core_output.is_noise(text):
                continue
            launched.output.append(text)
            if len(launched.output) > CORE_LOG_LINES:
                del launched.output[: len(launched.output) - CORE_LOG_LINES]
    except (asyncio.CancelledError, OSError, ValueError):
        return


def _core_reason(launched: Optional[_LaunchedCore]) -> tuple[str, Optional[str]]:
    if launched is None or not launched.output:
        return "", None
    detail, hint = core_output.extract_reason("\n".join(launched.output))
    return sanitize_output(detail)[:CORE_LOG_TAIL], hint


async def _shutdown_core(launched: Optional[_LaunchedCore]) -> None:
    if launched is None:
        return

    if launched.reader is not None:
        launched.reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await launched.reader

    process = launched.process
    _live_processes.pop(process, None)
    if process.returncode is None:
        _kill_group(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=TERM_GRACE)
        except asyncio.TimeoutError:
            _kill_group(process, signal.SIGKILL)
            with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
                await asyncio.wait_for(process.wait(), timeout=TERM_GRACE)

    with contextlib.suppress(OSError):
        launched.workdir.cleanup()


def _kill_group(process: asyncio.subprocess.Process, sig: int) -> None:
    """Убиваем всю группу: ядро могло породить дочерние процессы."""
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()


async def _sweep_loop() -> None:
    """Страховка от процесса, пережившего свой finally.

    Ячейка ограничена CELL_TIMEOUT, поэтому ядро старше CORE_MAX_LIFETIME — это
    уже не работающая проверка, а утечка.
    """
    while True:
        await asyncio.sleep(SWEEP_INTERVAL)
        now = time.monotonic()
        for process, started_at in list(_live_processes.items()):
            if process.returncode is not None:
                _live_processes.pop(process, None)
            elif now - started_at > CORE_MAX_LIFETIME:
                logger.warning("xray-test: killing stale core pid=%s", process.pid)
                _kill_group(process, signal.SIGKILL)
                _live_processes.pop(process, None)


async def start_xray_test_service() -> None:
    global _sweeper_task
    if _sweeper_task is None:
        _sweeper_task = asyncio.create_task(_sweep_loop())


async def stop_xray_test_service() -> None:
    """Добить все ядра при остановке панели."""
    global _sweeper_task
    if _sweeper_task is not None:
        _sweeper_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _sweeper_task
        _sweeper_task = None

    for process in list(_live_processes):
        _kill_group(process, signal.SIGKILL)
    _live_processes.clear()


def _empty_result(cell: TestCell) -> CellResult:
    endpoint = cell.endpoint
    return CellResult(
        index=cell.index,
        remark=endpoint.remark,
        protocol=endpoint.protocol.value,
        address=endpoint.address,
        port=endpoint.port,
        sni=cell.sni_label or endpoint.tls.sni,
        sni_from_config=cell.sni_label is None,
        transport=endpoint.transport.kind.value,
        security=endpoint.tls.security.value,
        timings=ProbeTimings(),
        link=cell.link,
        location=cell.location,
        location_name=cell.location_name,
    )


def _fail(
    result: CellResult,
    reason: FailReason,
    detail: str,
    hint: Optional[str] = None,
) -> CellResult:
    result.verdict = Verdict.FAIL
    result.reason = reason
    result.detail = sanitize_output(detail)[:CORE_LOG_TAIL]
    result.hint = hint or core_output.detect_hint(detail)
    return result


def _apply_verdict(result: CellResult, options: probes.ProbeOptions) -> CellResult:
    """Медленный, но живой канал — отдельная категория, а не «работает».

    Оговорка без объяснения бесполезна: у неё тоже проставляется причина, иначе
    «с оговорками» выглядит как вердикт без повода.
    """
    if (result.timings.rtt_ms or 0) > probes.DEGRADED_RTT_MS:
        result.verdict = Verdict.DEGRADED
        result.reason = FailReason.SLOW_RTT
        result.hint = "SLOW_RTT"
        return result

    if options.exit_identity and not result.exit_ip:
        result.verdict = Verdict.DEGRADED
        result.reason = FailReason.EXIT_IP_UNKNOWN
        result.hint = "EXIT_IP_UNKNOWN"
        return result

    result.verdict = Verdict.OK
    return result
