"""Прогон проверок на ноде — тест из нужной локации, а не из ЦОДа панели.

Своего эндпоинта у ноды для этого нет: используется общий канал исполнения
команд, а исполнитель (`configs/xray-test-runner.sh`) доставляется панелью и
версионируется, как сторож анти-DDoS. Ядра нода тоже получает от панели, в
GitHub не ходит.

Конфиги ядра генерирует панель: единственный генератор на оба места запуска
означает, что «работает на панели» и «работает на ноде» проверяют одно и то же.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from base64 import b64encode
from typing import Optional

import httpx

from app.models import Server
from app.services.http_client import get_external_client, get_node_client, node_auth_headers
from app.services.node_capabilities import learn_from_denial
from app.services.update_channel import current_branch, github_configs_base
from app.services.xray_test import bundle, core_manager, core_output
from app.services.xray_test.config_builder import BatchEntry, build_batch
from app.services.xray_test.errors import XrayTestError
from app.services.xray_test.models import CellResult, Core, FailReason, TestCell, Verdict
from app.services.xray_test.probes import ProbeOptions
from app.services.xray_test.sanitize import sanitize_output

logger = logging.getLogger(__name__)

RUNNER_PATH = "/opt/monitoring-node/tools/xray-test-runner.sh"
CORES_DIR = "/opt/monitoring-node/tools/cores"
RUNNER_SOURCE = "xray-test-runner.sh"
# Порты зарезервированы от эфемерной выдачи (configs/tune-sysctl.sh): иначе
# исходящее соединение ноды могло бы занять порт ровно между проверками.
# Размер пула — это и есть потолок параллельных проверок на одной ноде.
PORT_POOL = tuple(range(7501, 7533))
# Пачка на ноде: столько проверок уходит одним заданием. Больше пула портов
# брать нельзя — задание просто не получит портов на всех.
NODE_BATCH_SIZE = 16
# Исполнитель гонит проверки внутри задания по восемь (PARALLEL_CELLS)
NODE_PARALLEL_CELLS = 8
# Худший случай на проверку: TCP-пинги, запрос с повтором по таймауту и выходной
# IP. Замер скорости качает десять мегабайт и добавляется отдельно.
CELL_BUDGET = 60
SPEED_BUDGET = 25
EXEC_OVERHEAD = 30
EXEC_TIMEOUT_CAP = 600
# Запас поверх задания: нода должна успеть дослать последние строки
STREAM_GRACE = 30
CORE_INSTALL_TIMEOUT = 300
COMMAND_LIMIT = 60000
# Задание для исполнителя: строки через перевод, поля внутри строки — табом
SEP = "\t"
LINE_SEP = "\n"

_version_re = re.compile(r'RUNNER_VERSION="([0-9.]+)"')
_runner_cache: dict[int, str] = {}
_runner_source: Optional[str] = None
_source_lock = asyncio.Lock()


class NodeExecError(XrayTestError):
    code = "NODE_ERROR"


class NodeCoreRunner:
    """Раннер, исполняющий проверки на конкретном сервере.

    Порты берутся из общего пула: параллельные ячейки не должны сесть на один
    и тот же локальный порт ноды.
    """

    def __init__(self, server: Server) -> None:
        self.server = server
        self._ports: asyncio.Queue[int] = asyncio.Queue()
        for port in PORT_POOL:
            self._ports.put_nowait(port)
        # Рабочих над очередью восемь, и без ограничения все восемь полезли бы
        # на ноду одновременно — восемь потоков `execute-stream` и вчетверо
        # больше запросов портов, чем в пуле. Слотов ровно столько, сколько
        # пачек пул может обслужить одновременно.
        self._batch_slots = asyncio.Semaphore(max(1, len(PORT_POOL) // NODE_BATCH_SIZE))
        self._prepared = False
        self._prepare_lock = asyncio.Lock()
        self._tickets: dict[Core, bundle.BundleTicket] = {}

    async def prepare(self, cells: list[TestCell]) -> None:
        """Доставить исполнитель и заранее положить на ноду нужные ядра.

        Ядро ставится здесь, а не по ходу проверок: иначе параллельные ячейки
        начинают тянуть один и тот же файл одновременно и мешают друг другу.
        """
        async with self._prepare_lock:
            if self._prepared:
                return
            await _ensure_runner(self.server)
            for core in _cores_for(cells):
                ticket = await bundle.issue_ticket(core)
                self._tickets[core] = ticket
                await _ensure_core_on_node(self.server, ticket)
            self._prepared = True

    async def probe(self, cell: TestCell, options: ProbeOptions) -> CellResult:
        results = await self.probe_batch([cell], options)
        return results[0]

    async def probe_batch(
        self, cells: list[TestCell], options: ProbeOptions
    ) -> list[CellResult]:
        """Пачка проверок одним вызовом: ядро на ноде поднимается один раз.

        Раньше на каждую ячейку уходил свой `execute-stream` со своим процессом
        ядра — на боевой ноде это заметная нагрузка на ровном месте. Теперь
        нода получает один конфиг, где у каждой проверки свой socks-порт.
        """
        done: dict[int, CellResult] = {}
        by_core: dict[Core, list[TestCell]] = {}

        for cell in cells:
            try:
                core = core_manager.select_core(cell.endpoint)
            except XrayTestError as exc:
                done[cell.index] = _fail(cell, FailReason.UNSUPPORTED, str(exc))
                continue
            by_core.setdefault(core, []).append(cell)

        for core, group in by_core.items():
            for start in range(0, len(group), NODE_BATCH_SIZE):
                chunk = group[start:start + NODE_BATCH_SIZE]
                done.update(await self._run_chunk(chunk, core, options))

        return [done[cell.index] for cell in cells]

    async def _run_chunk(
        self, chunk: list[TestCell], core: Core, options: ProbeOptions
    ) -> dict[int, CellResult]:
        results = await self._execute_chunk(chunk, core, options)

        # Ядро не поднялось — гоним по одной, чтобы отказ достался виновной
        # конфигурации, а не всей пачке скопом
        if len(chunk) > 1 and all(
            result.reason is FailReason.CORE_START_FAILED for result in results.values()
        ):
            logger.info(
                "xray-test: пачка из %d не поднялась на %s, проверяю по одной",
                len(chunk), self.server.name,
            )
            singles = await asyncio.gather(*(
                self._execute_chunk([cell], core, options) for cell in chunk
            ))
            return {index: result for part in singles for index, result in part.items()}

        return results

    async def _execute_chunk(
        self, chunk: list[TestCell], core: Core, options: ProbeOptions
    ) -> dict[int, CellResult]:
        ticket = self._tickets.get(core)
        if ticket is None:
            # Ядро понадобилось уже после подготовки — выписываем ссылку на лету
            ticket = await bundle.issue_ticket(core)
            self._tickets[core] = ticket

        async with self._batch_slots:
            ports = [await self._ports.get() for _ in chunk]
            try:
                payload = _build_payload(chunk, ports, core, ticket, options)
                lines = await _execute(self.server, payload, _exec_timeout(len(chunk), options))
            except XrayTestError as exc:
                return {
                    cell.index: _fail(cell, FailReason.NODE_ERROR, str(exc)) for cell in chunk
                }
            finally:
                for port in ports:
                    self._ports.put_nowait(port)

        return _parse_results(chunk, lines)


def _cores_for(cells: list[TestCell]) -> set[Core]:
    cores: set[Core] = set()
    for cell in cells:
        try:
            cores.add(core_manager.select_core(cell.endpoint))
        except XrayTestError:
            continue
    return cores


def _build_payload(
    chunk: list[TestCell],
    ports: list[int],
    core: Core,
    ticket: bundle.BundleTicket,
    options: ProbeOptions,
) -> str:
    """Одно задание на пачку: общий конфиг ядра плюс строка на каждую проверку.

    Номер ячейки служит слотом: он же стоит в тегах конфига, и по нему
    исполнитель отбирает из общего лога ядра строки нужной проверки.
    """
    entries = [
        BatchEntry(str(cell.index), cell.endpoint, port)
        for cell, port in zip(chunk, ports)
    ]
    config_b64 = b64encode(json.dumps(build_batch(entries, core)).encode()).decode()

    rows = [
        SEP.join(["CORE", core.value, ticket.version, ticket.url, ticket.sha256]),
        SEP.join([
            "OPTS",
            "1" if options.tcp else "0",
            "1" if options.http else "0",
            "1" if options.exit_identity else "0",
            "1" if options.speed else "0",
        ]),
        SEP.join(["CONF", core.value, config_b64]),
    ]
    rows.extend(
        SEP.join([
            "CELL",
            str(cell.index),
            core.value,
            cell.endpoint.address,
            str(cell.endpoint.port),
            "1" if cell.endpoint.is_udp_protocol else "0",
            str(port),
            # SNI нужен исполнителю отдельно: по нему проверяется запасной ход
            # REALITY, отличающий блокировку от неподходящих параметров
            cell.endpoint.effective_sni,
        ])
        for cell, port in zip(chunk, ports)
    )
    return b64encode(LINE_SEP.join(rows).encode()).decode()



def _exec_timeout(count: int, options: ProbeOptions) -> int:
    """Потолок времени на задание — по числу волн внутри него.

    Один таймаут на любую пачку либо резал большие задания, либо заставлял ждать
    минутами там, где всё давно кончилось.
    """
    waves = max(1, -(-count // NODE_PARALLEL_CELLS))
    per_wave = CELL_BUDGET + (SPEED_BUDGET if options.speed else 0)
    return min(EXEC_TIMEOUT_CAP, EXEC_OVERHEAD + waves * per_wave)


async def _execute(server: Server, payload: str, budget: int) -> list[dict]:
    """Задание на ноду с жёстким потолком по времени на весь вызов.

    Потаймаутного чтения мало: оборванный исполнитель оставляет за собой
    фоновые процессы, они держат stdout открытым, поток с ноды не закрывается —
    и панель ждёт его вечно, а вместе с ней встаёт весь прогон. Потолок на
    вызов целиком снимает этот класс зависаний независимо от поведения ноды.
    """
    try:
        async with asyncio.timeout(budget + STREAM_GRACE):
            return await _stream(server, payload, budget)
    except asyncio.TimeoutError as exc:
        raise NodeExecError("Нода не завершила задание в отведённое время") from exc


async def _stream(server: Server, payload: str, budget: int) -> list[dict]:
    command = f"timeout -k 5 {budget} {RUNNER_PATH} {payload}"
    if len(command) > COMMAND_LIMIT:
        raise NodeExecError("Конфигурация слишком велика для передачи на ноду")

    body = {"command": command, "timeout": budget, "shell": "bash"}
    # Читаем дольше, чем живёт задание: иначе панель бросит поток раньше, чем
    # исполнитель успеет отдать последние строки
    timeout = httpx.Timeout(connect=10.0, read=budget + 20, write=10.0, pool=10.0)
    events: list[dict] = []

    try:
        client = get_node_client(server)
        async with client.stream(
            "POST", f"{server.url}/api/system/execute-stream",
            headers=node_auth_headers(server), json=body, timeout=timeout,
        ) as response:
            if response.status_code != 200:
                raw = (await response.aread()).decode("utf-8", errors="replace")
                await learn_from_denial(server.id, response.status_code, _maybe_json(raw))
                raise NodeExecError(f"Нода ответила HTTP {response.status_code}: {raw[:200]}")

            event_name = ""
            async for line in response.aiter_lines():
                line = line.rstrip("\r")
                if line.startswith("event:"):
                    event_name = line[len("event:"):].strip()
                elif line.startswith("data:") and event_name in ("stdout", "stderr"):
                    payload_line = _extract_line(line[len("data:"):].strip())
                    parsed = _maybe_json(payload_line)
                    if isinstance(parsed, dict):
                        events.append(parsed)
    except httpx.TimeoutException as exc:
        raise NodeExecError("Таймаут соединения с нодой") from exc
    except httpx.RequestError as exc:
        raise NodeExecError(f"Ошибка соединения с нодой: {exc}") from exc

    return events


async def _ensure_core_on_node(server: Server, ticket: bundle.BundleTicket) -> None:
    """Положить бинарник ядра на ноду до начала проверок.

    Скачивание идёт с панели: у ноды может не быть доступа к GitHub. Подмену
    ловит сверка SHA-256, поэтому `--insecure` здесь безопасен — сертификат
    панели бывает самоподписанным.
    """
    path = f"{CORES_DIR}/{ticket.core.value}-{ticket.version}"
    command = (
        f"set -e; mkdir -p {CORES_DIR}; "
        f"if [ -x {path} ] && [ \"$(sha256sum {path} | cut -d' ' -f1)\" = {ticket.sha256} ]; "
        f"then echo present; exit 0; fi; "
        f"curl -fsSL --insecure --max-time 300 '{ticket.url}' -o {path}.tmp; "
        f"[ \"$(sha256sum {path}.tmp | cut -d' ' -f1)\" = {ticket.sha256} ] "
        f"|| {{ rm -f {path}.tmp; echo 'контрольная сумма не совпала' >&2; exit 1; }}; "
        f"chmod 0755 {path}.tmp && mv -f {path}.tmp {path} && echo installed"
    )

    try:
        result = await _run_command(server, command, timeout=CORE_INSTALL_TIMEOUT)
    except NodeExecError as exc:
        raise NodeExecError(
            f"Ядро {ticket.core.value} {ticket.version} не доставлено на ноду: {exc}"
        ) from exc

    if "installed" in result:
        logger.info(
            "xray-test: core %s %s delivered to server %s",
            ticket.core.value, ticket.version, server.id,
        )


async def _ensure_runner(server: Server) -> None:
    """Сверить версию исполнителя на ноде и при расхождении переустановить."""
    source = await _load_runner_source()
    wanted = _version_of(source)

    if _runner_cache.get(server.id) == wanted:
        return

    installed = await _run_command(server, f"{RUNNER_PATH} version 2>/dev/null || true")
    if installed.strip() == wanted:
        _runner_cache[server.id] = wanted
        return

    encoded = b64encode(source.encode()).decode()
    install = (
        f"mkdir -p $(dirname {RUNNER_PATH}) && "
        f"printf '%s' '{encoded}' | base64 -d > {RUNNER_PATH} && "
        f"chmod 700 {RUNNER_PATH} && {RUNNER_PATH} version"
    )
    result = await _run_command(server, install)
    if result.strip() != wanted:
        raise NodeExecError(
            f"Исполнитель установлен, но вернул версию {result.strip() or '(пусто)'!r} "
            f"вместо {wanted}"
        )

    _runner_cache[server.id] = wanted
    logger.info("xray-test: runner %s installed on server %s", wanted, server.id)


async def _load_runner_source() -> str:
    global _runner_source
    async with _source_lock:
        if _runner_source is not None:
            return _runner_source

        url = f"{github_configs_base()}/{RUNNER_SOURCE}"
        try:
            response = await get_external_client().get(url, timeout=30.0)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Своей копии configs/ у панели нет (установщик кладёт только panel/),
            # поэтому исполнитель берётся с GitHub — из ветки текущего канала
            raise NodeExecError(
                f"Исполнитель проверок недоступен в канале «{current_branch()}» "
                f"({exc.response.status_code} по {url}). Если панель на стабильном канале, "
                f"функция появится после релиза"
            ) from exc
        except httpx.HTTPError as exc:
            raise NodeExecError(f"Не скачать исполнитель проверок с {url}: {exc}") from exc

        _runner_source = response.text
        return _runner_source


def _version_of(source: str) -> str:
    match = _version_re.search(source)
    if not match:
        raise NodeExecError("В исполнителе не найдена версия")
    return match.group(1)


async def _run_command(server: Server, command: str, timeout: int = 60) -> str:
    body = {"command": command, "timeout": timeout, "shell": "bash"}
    try:
        client = get_node_client(server)
        response = await client.post(
            f"{server.url}/api/system/execute",
            headers=node_auth_headers(server), json=body, timeout=float(timeout + 10),
        )
    except httpx.TimeoutException as exc:
        raise NodeExecError("Таймаут соединения с нодой") from exc
    except httpx.RequestError as exc:
        raise NodeExecError(f"Ошибка соединения с нодой: {exc}") from exc

    if response.status_code != 200:
        await learn_from_denial(server.id, response.status_code, _maybe_json(response.text))
        raise NodeExecError(f"Нода ответила HTTP {response.status_code}: {response.text[:200]}")

    payload = response.json()
    stdout = str(payload.get("stdout") or "").strip()
    if payload.get("success"):
        return stdout

    # Без stderr и кода возврата ошибка выглядела бы пустой строкой
    stderr = str(payload.get("stderr") or "").strip()
    error = str(payload.get("error") or "").strip()
    detail = stderr or error or stdout or "команда не дала вывода"
    raise NodeExecError(f"код {payload.get('exit_code')}: {detail[:300]}")


def _extract_line(data: str) -> str:
    parsed = _maybe_json(data)
    if isinstance(parsed, dict) and "line" in parsed:
        return str(parsed["line"])
    return data


def _maybe_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _parse_results(chunk: list[TestCell], events: list[dict]) -> dict[int, CellResult]:
    """Разложить строки исполнителя по ячейкам пачки.

    Исполнитель гонит проверки параллельно, поэтому строки приходят вперемешку и
    сопоставляются по номеру ячейки. Ячейка без своей строки — не молчаливый
    провал, а отдельная ошибка: значит исполнитель до неё не дошёл.
    """
    by_index: dict[int, dict] = {}
    for event in events:
        if event.get("type") != "cell":
            continue
        try:
            by_index[int(event.get("index"))] = event
        except (TypeError, ValueError):
            continue

    logs = " ".join(
        str(event.get("line", "")) for event in events if event.get("type") == "log"
    )
    results: dict[int, CellResult] = {}
    for cell in chunk:
        payload = by_index.get(cell.index)
        if payload is None:
            results[cell.index] = _fail(
                cell, FailReason.NODE_ERROR, logs or "нода не вернула результат проверки"
            )
            continue
        results[cell.index] = _parse_result(cell, payload)
    return results


def _parse_result(cell: TestCell, payload: dict) -> CellResult:
    result = _empty(cell)
    result.verdict = Verdict(payload.get("verdict", "fail"))
    reason = payload.get("reason")
    result.reason = FailReason(reason) if reason else None
    raw_detail = str(payload.get("detail") or "")
    parsed_detail, hint = core_output.extract_reason(raw_detail)
    if not parsed_detail and core_output.looks_stalled(raw_detail):
        parsed_detail, hint = core_output.STALLED_DETAIL, core_output.STALLED_HINT
    result.detail = sanitize_output(parsed_detail or raw_detail)[:400]
    result.hint = hint or core_output.detect_hint(raw_detail)
    # У оговорок объяснение выводится из самой причины, лога ядра для них нет
    if result.reason in (FailReason.SLOW_RTT, FailReason.EXIT_IP_UNKNOWN):
        result.hint = result.reason.value
    result.http_status = payload.get("http_status")
    result.exit_ip = payload.get("exit_ip")
    result.exit_country = payload.get("exit_country")
    result.resolved_ip = payload.get("resolved_ip")
    result.timings.dns_ms = payload.get("dns_ms")
    result.timings.tcp_min_ms = payload.get("tcp_min_ms")
    result.timings.tcp_avg_ms = payload.get("tcp_avg_ms")
    result.timings.tcp_jitter_ms = payload.get("tcp_jitter_ms")
    result.timings.handshake_ms = payload.get("handshake_ms")
    result.timings.rtt_ms = payload.get("rtt_ms")
    result.timings.speed_mbps = payload.get("speed_mbps")
    return result


def _empty(cell: TestCell) -> CellResult:
    endpoint = cell.endpoint
    core = None
    try:
        core = core_manager.select_core(endpoint).value
    except XrayTestError:
        pass
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
        core=core,
        link=cell.link,
        location=cell.location,
        location_name=cell.location_name,
    )


def _fail(cell: TestCell, reason: FailReason, detail: str) -> CellResult:
    result = _empty(cell)
    result.verdict = Verdict.FAIL
    result.reason = reason
    result.detail = sanitize_output(detail)[:400]
    return result
