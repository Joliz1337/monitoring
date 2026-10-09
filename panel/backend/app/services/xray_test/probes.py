"""Проверки: от TCP-рукопожатия до реального запроса через поднятый прокси.

Разделены сознательно. TCP-проба стоит копейки и мгновенно отсеивает мёртвые
серверы, TLS-проба показывает, жив ли домен-маскировка REALITY, а вердикт
«работает» даёт только сквозной HTTP-запрос через socks: сервер может держать
порт открытым и не пропускать трафик.
"""
from __future__ import annotations

import asyncio
import os
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from app.services.xray_test.models import FailReason, TlsInfo

# Эндпоинты без тела ответа: 204 и разрыв — минимум трафика на проверку
GENERATE_204_URLS = (
    "https://cp.cloudflare.com/generate_204",
    "https://www.gstatic.com/generate_204",
)
TRACE_URLS = (
    "https://cloudflare.com/cdn-cgi/trace",
    "https://ipinfo.io/json",
)

TCP_ATTEMPTS = 3
TCP_TIMEOUT = 3.0
TLS_TIMEOUT = 5.0
HTTP_TIMEOUT = 10.0
DEGRADED_RTT_MS = 1500.0

# Замер скорости — как у спидтестов: несколько потоков разом и фиксированное
# окно без лимита объёма, чтобы мерить до 10 Гбит/с. Один поток через VPN
# упирается в окно TCP при задержке пути (через ключ: один поток ~400 Мбит/с,
# четыре ~700), а закачка фиксированного объёма на быстром канале кончается
# раньше, чем соединение успевает разогнаться.
SPEED_STREAMS = 8
SPEED_MAX_TIME = 8.0
# Столько запрашивается за раз; докачав, поток запрашивает файл снова
SPEED_REQUEST_BYTES = 1_000_000_000
# Cloudflare за запрос отдаёт меньше 100 000 000 байт: на ровно 100 млн — 403
CLOUDFLARE_MAX_BYTES = 99_000_000
# Новая закачка короче секунды мерила бы только установку соединения
SPEED_MIN_TRANSFER_TIME = 1.0
SPEED_CONNECT_TIMEOUT = 5.0
# Поток медленнее порога столько секунд подряд встал — так выглядит Cloudflare
# под ТСПУ: первые 16 КБ приходят, потом соединение замирает. Порог не ниже
# 16 КБ/с, потому что curl усредняет скорость за последние секунды: при пороге
# в килобайт всплеск первых 16 КБ держал бы среднее выше него, и зависший
# сервер съедал бы всё окно.
SPEED_STALL_RATE = 16384
SPEED_STALL_SECONDS = 4
SPEED_MIN_BYTES = 256 * 1024
# Замер идёт по одной проверке на точку: две многопоточные закачки разом
# делили бы канал точки, и каждая мерила бы свою долю
SPEED_CONCURRENCY = 1
SPEED_ATTEMPTS = 3
# Худший случай замера: каждый сервер дотянул до жёсткого потолка попытки
SPEED_BUDGET = SPEED_ATTEMPTS * (SPEED_MAX_TIME + SPEED_CONNECT_TIMEOUT)
RU_EXIT_COUNTRIES = frozenset({"RU"})


@dataclass(frozen=True)
class SpeedServer:
    name: str
    url: str


# Файлы отдаются частями (Range) и имеют постоянное имя. Сервер нужен рядом с
# выходом ключа: замер до другого конца света упирается в задержку пути, а не
# в канал прокси. И он обязан отдавать несколько потоков одному IP: Hetzner на
# часть параллельных отвечает 429, Vultr — 503. OVH держит четыре потока из
# восьми, остальным отвечает 429 — замер считается по тем, что качали. Cloudflare
# и OVH после пары гигабайт с одного IP отказывают целиком — тогда замер уходит
# к следующему серверу.
SPEED_SERVERS_RU = (
    SpeedServer("Selectel", "https://speedtest.selectel.ru/1GB"),
    # Образ Arch в latest/ меняется раз в месяц, имя у него постоянное
    SpeedServer("Yandex", "https://mirror.yandex.ru/archlinux/iso/latest/archlinux-x86_64.iso"),
)
SPEED_SERVERS_WORLD = (
    # Anycast: файл отдаёт ближайшая к выходу точка присутствия
    SpeedServer("Cloudflare", f"https://speed.cloudflare.com/__down?bytes={CLOUDFLARE_MAX_BYTES}"),
    SpeedServer("OVH", "https://proof.ovh.net/files/1Gb.dat"),
)


@dataclass
class DnsResult:
    ip: Optional[str] = None
    elapsed_ms: Optional[float] = None
    error: Optional[str] = None


@dataclass
class TcpResult:
    min_ms: Optional[float] = None
    avg_ms: Optional[float] = None
    jitter_ms: Optional[float] = None
    reason: Optional[FailReason] = None
    error: Optional[str] = None

    @property
    def alive(self) -> bool:
        return self.min_ms is not None


@dataclass
class HttpResult:
    status: Optional[int] = None
    handshake_ms: Optional[float] = None
    rtt_ms: Optional[float] = None
    reason: Optional[FailReason] = None
    error: Optional[str] = None
    # Была вторая попытка: значит первая упала по таймауту, и результат стоит
    # читать с поправкой — канал как минимум нестабилен
    retried: bool = False


@dataclass
class ExitIdentity:
    ip: Optional[str] = None
    country: Optional[str] = None
    asn: Optional[str] = None


@dataclass
class SpeedResult:
    mbps: Optional[float] = None
    server: Optional[str] = None


@dataclass
class _StreamProgress:
    received: int = 0
    first_byte_at: float = 0.0
    last_byte_at: float = 0.0


@dataclass
class ProbeOptions:
    """Что именно гонять — быстрый режим экономит минуты на больших подписках."""

    tcp: bool = True
    tls_inspect: bool = False
    http: bool = True
    exit_identity: bool = True
    speed: bool = False
    attempts: int = TCP_ATTEMPTS
    extra_headers: dict[str, str] = field(default_factory=dict)


async def resolve_address(host: str) -> DnsResult:
    if _is_ip_literal(host):
        return DnsResult(ip=host, elapsed_ms=0.0)

    started = time.perf_counter()
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, type=socket.SOCK_STREAM
        )
    except (socket.gaierror, OSError) as exc:
        return DnsResult(error=str(exc))
    elapsed = (time.perf_counter() - started) * 1000
    return DnsResult(ip=infos[0][4][0] if infos else None, elapsed_ms=round(elapsed, 1))


async def tcp_ping(host: str, port: int, attempts: int = TCP_ATTEMPTS) -> TcpResult:
    samples: list[float] = []
    last_error: Optional[BaseException] = None

    for _ in range(max(1, attempts)):
        started = time.perf_counter()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=TCP_TIMEOUT
            )
        except asyncio.TimeoutError as exc:
            last_error = exc
            continue
        except OSError as exc:
            last_error = exc
            continue

        samples.append((time.perf_counter() - started) * 1000)
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    if not samples:
        reason = (
            FailReason.TCP_TIMEOUT
            if isinstance(last_error, asyncio.TimeoutError)
            else FailReason.TCP_REFUSED
        )
        return TcpResult(reason=reason, error=str(last_error) if last_error else "нет ответа")

    return TcpResult(
        min_ms=round(min(samples), 1),
        avg_ms=round(sum(samples) / len(samples), 1),
        jitter_ms=round(max(samples) - min(samples), 1) if len(samples) > 1 else 0.0,
    )


async def inspect_tls(host: str, port: int, sni: str) -> TlsInfo:
    """Прямое рукопожатие с целевым SNI — мимо прокси.

    Для REALITY показывает, отвечает ли домен-маскировка и чей у него
    сертификат: неожиданный издатель — признак перехвата у провайдера.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_alpn_protocols(["h2", "http/1.1"])

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=context, server_hostname=sni),
            timeout=TLS_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return TlsInfo(error="таймаут TLS-рукопожатия")
    except (ssl.SSLError, OSError) as exc:
        return TlsInfo(error=_describe(exc, "соединение не установилось"))

    try:
        ssl_object = writer.get_extra_info("ssl_object")
        # При verify_mode=CERT_NONE Python не разбирает сертификат и getpeercert()
        # возвращает пустой словарь — берём DER и читаем поля сами
        der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
        issuer, subject, not_after = _read_certificate(der)
        return TlsInfo(
            reachable=True,
            issuer=issuer,
            subject=subject,
            not_after=not_after,
            version=ssl_object.version() if ssl_object else None,
            alpn=ssl_object.selected_alpn_protocol() if ssl_object else None,
            self_signed=bool(issuer and subject and issuer == subject),
        )
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ssl.SSLError):
            pass


RETRY_PAUSE = 1.5
RETRY_REASONS = (FailReason.HTTP_TIMEOUT, FailReason.PROXY_HANDSHAKE_FAILED)


async def http_through_proxy(socks_port: int, headers: Optional[dict] = None) -> HttpResult:
    """Проба через прокси с одной повторной попыткой по таймауту.

    Проверки идут пачками, и на загруженной ноде запрос может не уложиться в
    таймаут при живом канале — вердикт «не работает» по одной такой попытке
    оказывается ложным. Явные отказы (плохой статус, отказ цели) не повторяем:
    они воспроизводимы, и вторая попытка только тянет время.
    """
    first = await _http_attempt(socks_port, headers)
    if first.reason not in RETRY_REASONS:
        return first
    await asyncio.sleep(RETRY_PAUSE)
    second = await _http_attempt(socks_port, headers)
    second.retried = True
    return second


async def _http_attempt(socks_port: int, headers: Optional[dict] = None) -> HttpResult:
    """Два запроса: первый меряет установку соединения, второй — чистый RTT.

    Без второго замера медленное рукопожатие и медленный канал сливались бы в
    одно число, а это разные диагнозы.
    """
    proxy = f"socks5://127.0.0.1:{socks_port}"
    last: HttpResult = HttpResult(reason=FailReason.PROXY_HANDSHAKE_FAILED, error="нет попыток")

    for url in GENERATE_204_URLS:
        try:
            async with httpx.AsyncClient(
                proxy=proxy, timeout=HTTP_TIMEOUT, verify=True, trust_env=False,
                headers=headers or {},
            ) as client:
                started = time.perf_counter()
                first = await client.get(url)
                handshake = (time.perf_counter() - started) * 1000

                started = time.perf_counter()
                second = await client.get(url)
                rtt = (time.perf_counter() - started) * 1000

            status = second.status_code or first.status_code
            result = HttpResult(
                status=status,
                handshake_ms=round(handshake, 1),
                rtt_ms=round(rtt, 1),
            )
            if status not in (200, 204):
                result.reason = FailReason.HTTP_BAD_STATUS
                result.error = f"HTTP {status}"
            return result
        except httpx.TimeoutException as exc:
            last = HttpResult(reason=FailReason.HTTP_TIMEOUT, error=_describe(exc, "таймаут запроса"))
        except httpx.ProxyError as exc:
            last = HttpResult(
                reason=FailReason.PROXY_HANDSHAKE_FAILED,
                error=_describe(exc, "локальный socks не принял соединение"),
            )
        except httpx.ConnectError as exc:
            last = HttpResult(
                reason=FailReason.PROXY_HANDSHAKE_FAILED,
                error=_describe(exc, "через прокси не удалось дойти до цели"),
            )
        except httpx.HTTPError as exc:
            last = HttpResult(reason=FailReason.PROXY_HANDSHAKE_FAILED, error=_describe(exc))
    return last


def _describe(exc: BaseException, fallback: str = "") -> str:
    """Текст исключения, а если он пуст — хотя бы его тип.

    httpx часто поднимает ConnectError с пустым сообщением: в интерфейсе это
    выглядело прочерком вместо причины.
    """
    text = str(exc).strip()
    if text:
        return text
    return fallback or type(exc).__name__


async def exit_identity(socks_port: int) -> ExitIdentity:
    proxy = f"socks5://127.0.0.1:{socks_port}"
    for url in TRACE_URLS:
        try:
            async with httpx.AsyncClient(
                proxy=proxy, timeout=HTTP_TIMEOUT, trust_env=False
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                identity = _parse_identity(url, response.text)
                if identity.ip:
                    return identity
        except (httpx.HTTPError, ValueError):
            continue
    return ExitIdentity()


def speed_servers_for(exit_country: Optional[str]) -> tuple[SpeedServer, ...]:
    """Порядок попыток замера: серверы своей группы, затем другой.

    Другая группа — на случай, когда страна выхода не определилась: ключу с
    выходом в РФ и неизвестной страной иначе достались бы одни зависания.
    Исполнитель на ноде получает уже готовые списки и сам их не составляет.
    """
    if (exit_country or "").upper() in RU_EXIT_COUNTRIES:
        ordered = SPEED_SERVERS_RU + SPEED_SERVERS_WORLD
    else:
        ordered = SPEED_SERVERS_WORLD + SPEED_SERVERS_RU
    return ordered[:SPEED_ATTEMPTS]


async def download_speed(socks_port: int, exit_country: Optional[str]) -> SpeedResult:
    """Скорость через прокси до первого сервера, отдавшего данные."""
    # socks5h: адрес цели резолвит выход ключа, а не панель — иначе anycast
    # Cloudflare отдал бы узел рядом с панелью
    proxy = f"socks5h://127.0.0.1:{socks_port}"
    for server in speed_servers_for(exit_country):
        mbps = await _download_mbps(proxy, server.url)
        if mbps is not None:
            return SpeedResult(mbps=mbps, server=server.name)
    return SpeedResult()


async def _download_mbps(proxy: Optional[str], url: str) -> Optional[float]:
    """Общая скорость `SPEED_STREAMS` потоков за окно `SPEED_MAX_TIME`; None — данных нет.

    Качает curl, а не httpx: Python в одном потоке через ключ давал вдвое меньше
    curl (366 против 842 Мбит/с) и до гигабит не дотянул бы, а процессы curl
    раскладываются по ядрам.
    """
    deadline = time.monotonic() + SPEED_MAX_TIME
    streams = await asyncio.gather(*(_pull(proxy, url, deadline) for _ in range(SPEED_STREAMS)))
    return _aggregate_mbps([transfer for stream in streams for transfer in stream])


async def _pull(proxy: Optional[str], url: str, deadline: float) -> list[_StreamProgress]:
    """Один поток: докачав файл, запрашивает его снова, пока не кончится окно.

    Отказ, зависание или закачка, оборванная окном, поток заканчивают: повтор
    после 429 только продлил бы ограничение со стороны сервера.
    """
    transfers: list[_StreamProgress] = []
    while (left := deadline - time.monotonic()) >= SPEED_MIN_TRANSFER_TIME:
        transfer, finished = await _curl_transfer(proxy, url, left)
        if transfer is not None:
            transfers.append(transfer)
        if not finished:
            break
    return transfers


async def _curl_transfer(
    proxy: Optional[str], url: str, budget: float,
) -> tuple[Optional[_StreamProgress], bool]:
    """Одна закачка: что пришло и докачан ли файл целиком."""
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *_curl_command(proxy, url, budget),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        # Потолок поверх --max-time: повисший curl держал бы замер, а с ним и прогон
        output, _ = await asyncio.wait_for(process.communicate(), budget + SPEED_CONNECT_TIMEOUT)
    except TimeoutError:
        return None, False
    finally:
        # И при отмене проверки: иначе curl качал бы дальше уже без хозяина
        if process.returncode is None:
            process.kill()
            await process.wait()

    fields = output.decode(errors="replace").split()
    if len(fields) != 4 or fields[0] not in ("200", "206") or not float(fields[1]):
        return None, False
    transfer = _StreamProgress(
        received=int(float(fields[1])),
        first_byte_at=started + float(fields[2]),
        last_byte_at=started + float(fields[3]),
    )
    return transfer, process.returncode == 0


def _curl_command(proxy: Optional[str], url: str, budget: float) -> list[str]:
    command = [
        "curl", "-s", "-o", os.devnull,
        "-r", f"0-{SPEED_REQUEST_BYTES - 1}",
        "--connect-timeout", f"{SPEED_CONNECT_TIMEOUT:g}",
        "--max-time", f"{budget:.2f}",
        "--speed-limit", str(SPEED_STALL_RATE),
        "--speed-time", str(SPEED_STALL_SECONDS),
        "-w", "%{http_code} %{size_download} %{time_starttransfer} %{time_total}",
    ]
    if proxy:
        command += ["--proxy", proxy]
    return command + [url]


def _aggregate_mbps(streams: list[_StreamProgress]) -> Optional[float]:
    """Скорость всех закачек по общему окну — от первого байта до последнего.

    Время идёт от первого байта: установка соединения через прокси на быстром
    канале занимала бы большую часть замера. Сумма скоростей отдельных потоков
    завышала бы цифру — потоки, кончившие в разное время, вместе не шли.
    """
    active = [stream for stream in streams if stream.received]
    if not active:
        return None
    elapsed = max(s.last_byte_at for s in active) - min(s.first_byte_at for s in active)
    return _mbps(sum(s.received for s in active), elapsed)


def _mbps(received: int, elapsed: float) -> Optional[float]:
    if received < SPEED_MIN_BYTES or elapsed <= 0:
        return None
    return round(received * 8 / elapsed / 1_000_000, 2)


def _parse_identity(url: str, body: str) -> ExitIdentity:
    if url.endswith("/trace"):
        values = dict(
            line.split("=", 1) for line in body.splitlines() if "=" in line
        )
        return ExitIdentity(ip=values.get("ip"), country=values.get("loc"))

    import json

    data = json.loads(body)
    org = str(data.get("org") or "")
    return ExitIdentity(
        ip=data.get("ip"),
        country=data.get("country"),
        asn=org.split()[0] if org.startswith("AS") else None,
    )


def _read_certificate(der: Optional[bytes]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """DER-сертификат → (издатель, владелец, срок действия)."""
    if not der:
        return None, None, None
    try:
        from cryptography import x509

        cert = x509.load_der_x509_certificate(der)
        return (
            _common_name(cert.issuer),
            _common_name(cert.subject),
            cert.not_valid_after_utc.strftime("%Y-%m-%d"),
        )
    except Exception:  # noqa: BLE001 — диагностика не должна ронять проверку
        return None, None, None


def _common_name(name) -> Optional[str]:
    from cryptography.x509.oid import NameOID

    for oid in (NameOID.COMMON_NAME, NameOID.ORGANIZATION_NAME):
        values = name.get_attributes_for_oid(oid)
        if values:
            return str(values[0].value)
    return None


def _is_ip_literal(host: str) -> bool:
    try:
        socket.inet_pton(socket.AF_INET, host)
        return True
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, host)
        return True
    except OSError:
        return False
