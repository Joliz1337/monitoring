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
import re
import signal
import socket
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol as TypingProtocol

from app.services.xray_test import core_manager, core_output, probes
from app.services.xray_test.config_builder import BatchEntry, build_batch, build_config
from app.services.xray_test.config_builder.batch import INBOUND_TAG, OUTBOUND_TAG
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
from app.services.xray_test.report import ResultSink, report as _report
from app.services.xray_test.sanitize import sanitize_output

logger = logging.getLogger(__name__)

WORK_DIR = Path("/app/data/xray-test/run")
MAX_CONCURRENT_CORES = 32
# Пачка проверок на один процесс ядра. Ядро держит сколько угодно inbound'ов,
# поэтому процесс на ячейку — чистые накладные расходы: запуск, память, CPU.
# Шестнадцать выбрано под пул портов ноды (7501-7532) с запасом.
BATCH_SIZE = 16
# Замер скорости качает десять мегабайт: через общий процесс такие закачки
# толкаются локтями и портят замер друг другу
MAX_CONCURRENT_SPEED = 2
CORE_START_TIMEOUT = 5.0
CELL_TIMEOUT = 40.0
CORE_MAX_LIFETIME = 90.0
SWEEP_INTERVAL = 30.0
TERM_GRACE = 3.0
READY_POLL_INTERVAL = 0.05
CORE_LOG_TAIL = 400
CORE_LOG_LINES = 40

_core_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CORES)
_speed_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SPEED)
_live_processes: dict[asyncio.subprocess.Process, float] = {}
_sweeper_task: Optional[asyncio.Task] = None


class CoreRunner(TypingProtocol):
    """Общий контракт для прогона на панели и на ноде."""

    async def probe(self, cell: TestCell, options: probes.ProbeOptions) -> CellResult:
        ...

    async def probe_batch(
        self,
        cells: list[TestCell],
        options: probes.ProbeOptions,
        on_result: ResultSink = None,
    ) -> list[CellResult]:
        ...


@dataclass
class _Ready:
    """Ячейка после проб, которым ядро не нужно. `core=None` — уже готово."""

    cell: TestCell
    result: CellResult
    core: Optional[Core]


@dataclass
class _LaunchedCore:
    process: asyncio.subprocess.Process
    ports: list[int]
    workdir: tempfile.TemporaryDirectory
    started_at: float = field(default_factory=time.monotonic)
    # Вывод копится фоном: причина отказа появляется в нём уже после старта,
    # а читать поток по факту провала поздно — ядро к тому моменту убито
    output: list[str] = field(default_factory=list)
    reader: Optional[asyncio.Task] = None

    @property
    def port(self) -> int:
        return self.ports[0]


class LocalCoreRunner:
    """Прогон на самой панели.

    Порт берётся у системы, ячейки быстрые и однородные, поэтому выгодно брать
    их пачками: один процесс ядра на пачку экономит запуски, а простой от
    медленной ячейки внутри пачки здесь незаметен.
    """

    batch_size = BATCH_SIZE
    workers = 8

    async def probe(self, cell: TestCell, options: probes.ProbeOptions) -> CellResult:
        results = await self.probe_batch([cell], options)
        return results[0]

    async def probe_batch(
        self,
        cells: list[TestCell],
        options: probes.ProbeOptions,
        on_result: ResultSink = None,
    ) -> list[CellResult]:
        """Пачка проверок: один процесс ядра на всех, кому он вообще нужен.

        Готовые вердикты отдаются приёмнику по мере готовности: ждать всю пачку
        значит заполнять таблицу рывками, а на медленной точке — молчать минутами.

        Пробы до ядра (DNS, TCP, TLS) идут параллельно и часть ячеек отсеивают
        ещё до запуска. Остальные группируются по ядру — Xray и sing-box в один
        процесс не сложить — и разбираются кусками по `BATCH_SIZE`.
        """
        done: dict[int, CellResult] = {}
        pending: list[_Ready] = []

        for prepared in await asyncio.gather(*(self._prepare(c, options) for c in cells)):
            done[prepared.cell.index] = prepared.result
            if prepared.core is None:
                _report(prepared.result, on_result)
            else:
                pending.append(prepared)

        by_core: dict[Core, list[_Ready]] = {}
        for item in pending:
            by_core.setdefault(item.core, []).append(item)  # type: ignore[arg-type]

        for core, items in by_core.items():
            try:
                binary = await core_manager.ensure_core(core)
            except CoreDownloadError as exc:
                for item in items:
                    done[item.cell.index] = _report(_fail(
                        item.result, FailReason.CORE_START_FAILED, str(exc)
                    ), on_result)
                continue
            for start in range(0, len(items), BATCH_SIZE):
                chunk = items[start:start + BATCH_SIZE]
                done.update(await self._run_chunk(chunk, options, core, binary, on_result))

        return [done[cell.index] for cell in cells]

    async def _prepare(self, cell: TestCell, options: probes.ProbeOptions) -> _Ready:
        """Всё, что можно узнать без ядра. `core=None` — ячейка уже завершена."""
        endpoint = cell.endpoint
        result = _empty_result(cell)

        dns = await probes.resolve_address(endpoint.address)
        result.resolved_ip = dns.ip
        result.timings.dns_ms = dns.elapsed_ms
        if dns.error:
            return _Ready(cell, _fail(result, FailReason.DNS_FAIL, dns.error), None)

        if options.tcp and not endpoint.is_udp_protocol:
            tcp = await probes.tcp_ping(endpoint.address, endpoint.port, options.attempts)
            result.timings.tcp_min_ms = tcp.min_ms
            result.timings.tcp_avg_ms = tcp.avg_ms
            result.timings.tcp_jitter_ms = tcp.jitter_ms
            if not tcp.alive:
                return _Ready(cell, _fail(
                    result, tcp.reason or FailReason.TCP_REFUSED, tcp.error or ""
                ), None)

        if options.tls_inspect and endpoint.tls.security.value != "none":
            result.tls_info = await probes.inspect_tls(
                endpoint.address, endpoint.port, endpoint.effective_sni
            )

        if not options.http:
            result.verdict = Verdict.OK
            return _Ready(cell, result, None)

        try:
            core = core_manager.select_core(endpoint)
        except UnsupportedConfigError as exc:
            return _Ready(cell, _fail(result, FailReason.UNSUPPORTED, str(exc)), None)

        result.core = core.value
        return _Ready(cell, result, core)

    async def _run_chunk(
        self,
        chunk: list[_Ready],
        options: probes.ProbeOptions,
        core: Core,
        binary: Path,
        on_result: ResultSink = None,
    ) -> dict[int, CellResult]:
        if len(chunk) > 1:
            batched = await self._try_batch(chunk, options, core, binary, on_result)
            if batched is not None:
                return batched

        # Поодиночке: либо в пачке нечего объединять, либо она не поднялась и
        # надо понять, какая именно конфигурация виновата
        async def one(item: _Ready) -> CellResult:
            async with _core_semaphore:
                return _report(await self._probe_through_core(
                    item.cell, options, item.result, core, binary
                ), on_result)

        results = await asyncio.gather(*(one(item) for item in chunk))
        return {item.cell.index: result for item, result in zip(chunk, results)}

    async def _try_batch(
        self,
        chunk: list[_Ready],
        options: probes.ProbeOptions,
        core: Core,
        binary: Path,
        on_result: ResultSink = None,
    ) -> Optional[dict[int, CellResult]]:
        """Пачка одним процессом. `None` — не поднялась, зовите поодиночке."""
        async with _core_semaphore:
            try:
                launched = await self._launch_batch(chunk, core, binary)
            except _CoreStartError as exc:
                logger.info(
                    "xray-test: пачка из %d не поднялась (%s), проверяю по одной",
                    len(chunk), exc.detail,
                )
                return None

            try:
                results = await asyncio.gather(*(
                    self._probe_slot(launched, item, options, position, on_result)
                    for position, item in enumerate(chunk)
                ))
                return {item.cell.index: result for item, result in zip(chunk, results)}
            finally:
                await _shutdown_core(launched)

    async def _probe_slot(
        self,
        launched: _LaunchedCore,
        item: _Ready,
        options: probes.ProbeOptions,
        position: int,
        on_result: ResultSink = None,
    ) -> CellResult:
        port = launched.ports[position]
        # Слот в тегах конфига — номер ячейки, а не место в пачке: он же
        # приходит с ноды, и по нему из общего лога отбираются свои строки
        slot = str(item.cell.index)
        try:
            async with asyncio.timeout(CELL_TIMEOUT):
                return _report(await self._explain(
                    item.cell,
                    await self._run_probes(launched, options, item.result, port, slot),
                ), on_result)
        except asyncio.TimeoutError:
            core_detail, hint = _core_reason(launched, slot)
            return _report(await self._explain(item.cell, _fail(
                item.result, FailReason.HTTP_TIMEOUT,
                core_detail or "проверка не уложилась в отведённое время", hint,
            )), on_result)

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
                    cell, await self._run_probes(launched, options, result, launched.port)
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
        self,
        launched: _LaunchedCore,
        options: probes.ProbeOptions,
        result: CellResult,
        port: int,
        slot: Optional[str] = None,
    ) -> CellResult:
        http = await probes.http_through_proxy(port, options.extra_headers)
        result.http_status = http.status
        result.timings.handshake_ms = http.handshake_ms
        result.timings.rtt_ms = http.rtt_ms

        if http.reason is not None:
            # Текст исключения клиента часто пуст, а настоящая причина — в логе
            # ядра: «certificate is valid for …, not …», «connection refused»
            core_detail, hint = _core_reason(launched, slot)
            detail = core_detail or http.error or ""
            if launched.process.returncode:
                detail = f"{detail} (ядро завершилось с кодом {launched.process.returncode})".strip()
            return _fail(result, http.reason, detail, hint)

        if options.exit_identity:
            identity = await probes.exit_identity(port)
            result.exit_ip = identity.ip
            result.exit_country = identity.country
            result.exit_asn = identity.asn

        if options.speed:
            async with _speed_semaphore:
                result.timings.speed_mbps = await probes.download_speed(port)

        return _apply_verdict(result, options)

    async def _launch(self, cell: TestCell, core: Core, binary: Path) -> _LaunchedCore:
        port = _free_port()
        return await self._spawn(
            build_config(cell.endpoint, core, port), [port], binary, prefix="cell-"
        )

    async def _launch_batch(
        self, chunk: list[_Ready], core: Core, binary: Path
    ) -> _LaunchedCore:
        ports = [_free_port() for _ in chunk]
        entries = [
            BatchEntry(str(slot), item.cell.endpoint, port)
            for slot, (item, port) in enumerate(zip(chunk, ports))
        ]
        return await self._spawn(
            build_batch(entries, core), ports, binary, prefix="batch-"
        )

    async def _spawn(
        self, config: dict, ports: list[int], binary: Path, *, prefix: str
    ) -> _LaunchedCore:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        workdir = tempfile.TemporaryDirectory(dir=str(WORK_DIR), prefix=prefix)

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
        launched = _LaunchedCore(process=process, ports=ports, workdir=workdir)
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
    """Готово, только когда открыты все порты пачки.

    Половина открытых портов хуже отказа: часть проверок пошла бы в ядро,
    которое ещё не дослушало остальные, и получила бы отказ ни за что.
    """
    deadline = time.monotonic() + CORE_START_TIMEOUT
    remaining = list(launched.ports)
    while time.monotonic() < deadline:
        if launched.process.returncode is not None:
            return False
        remaining = [port for port in remaining if not await _port_open(port)]
        if not remaining:
            return True
        await asyncio.sleep(READY_POLL_INTERVAL)
    return False


async def _port_open(port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


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


def _core_reason(
    launched: Optional[_LaunchedCore], slot: Optional[str] = None
) -> tuple[str, Optional[str]]:
    if launched is None or not launched.output:
        return "", None
    lines = _lines_for_slot(launched.output, slot)
    if not lines:
        return "", None
    detail, hint = core_output.extract_reason("\n".join(lines))
    return sanitize_output(detail)[:CORE_LOG_TAIL], hint


def _lines_for_slot(output: list[str], slot: Optional[str]) -> list[str]:
    """Строки лога, относящиеся к одной проверке пачки.

    Ядро помечает строки соединения тегами `[mon-test-in-N -> mon-test-out-N]`,
    и без такого отбора причина отказа одной конфигурации досталась бы соседней.
    Строки вовсе без тега — общие: не поднялся транспорт, не разобрался конфиг;
    они отдаются любому слоту, потому что относятся ко всей пачке.
    """
    if slot is None:
        return output

    tags = f"(?:{re.escape(INBOUND_TAG)}|{re.escape(OUTBOUND_TAG)})"
    own = re.compile(rf"{tags}-{re.escape(slot)}(?![0-9])")
    any_slot = re.compile(rf"{tags}-[0-9]+")

    mine = [line for line in output if own.search(line)]
    if mine:
        return mine
    return [line for line in output if not any_slot.search(line)]


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
