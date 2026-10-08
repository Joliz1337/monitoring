"""Проверки: от TCP-рукопожатия до реального запроса через поднятый прокси.

Разделены сознательно. TCP-проба стоит копейки и мгновенно отсеивает мёртвые
серверы, TLS-проба показывает, жив ли домен-маскировка REALITY, а вердикт
«работает» даёт только сквозной HTTP-запрос через socks: сервер может держать
порт открытым и не пропускать трафик.
"""
from __future__ import annotations

import asyncio
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

# Замер скорости — как у спидтестов: несколько потоков разом, до 99 МБ на поток,
# но не дольше окна. Один поток через VPN упирается в окно TCP при задержке
# пути (замер через ключ: один поток ~400 Мбит/с, четыре ~700), а закачка
# в 25 МБ кончалась раньше, чем соединение успевало разогнаться, — ключи
# показывали ~150 Мбит/с при канале в разы шире.
SPEED_STREAMS = 4
# Cloudflare за запрос отдаёт меньше 100 000 000 байт: на ровно 100 млн — 403
SPEED_STREAM_BYTES = 99_000_000
SPEED_MAX_TIME = 8.0
SPEED_CONNECT_TIMEOUT = 5.0
# Столько секунд без единого байта — поток встал. Так выглядит Cloudflare под
# ТСПУ: первые 16 КБ приходят, потом соединение замирает.
SPEED_STALL_SECONDS = 4.0
SPEED_MIN_BYTES = 256 * 1024
# Замер идёт по одной проверке на точку: две многопоточные закачки разом
# делили бы канал точки, и каждая мерила бы свою долю
SPEED_CONCURRENCY = 1
SPEED_ATTEMPTS = 3
# Худший случай замера: каждый сервер дотянул до жёсткого потолка попытки
SPEED_BUDGET = SPEED_ATTEMPTS * (SPEED_MAX_TIME + SPEED_STALL_SECONDS)
RU_EXIT_COUNTRIES = frozenset({"RU"})


@dataclass(frozen=True)
class SpeedServer:
    name: str
    url: str


# Файлы отдаются частями (Range), поэтому подходит любой файл не меньше потока
# с постоянным именем. Сервер нужен рядом с выходом ключа: замер до другого
# конца света упирается в задержку пути, а не в канал прокси. И он обязан
# отдавать несколько потоков одному IP: Hetzner на второй-третий отвечает 429,
# Vultr — 503.
SPEED_SERVERS_RU = (
    SpeedServer("Selectel", "https://speedtest.selectel.ru/100MB"),
    # Образ Arch в latest/ меняется раз в месяц, имя у него постоянное
    SpeedServer("Yandex", "https://mirror.yandex.ru/archlinux/iso/latest/archlinux-x86_64.iso"),
)
SPEED_SERVERS_WORLD = (
    # Anycast: файл отдаёт ближайшая к выходу точка присутствия
    SpeedServer("Cloudflare", f"https://speed.cloudflare.com/__down?bytes={SPEED_STREAM_BYTES}"),
    SpeedServer("OVH", "https://proof.ovh.net/files/100Mb.dat"),
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
    proxy = f"socks5://127.0.0.1:{socks_port}"
    for server in speed_servers_for(exit_country):
        mbps = await _download_mbps(proxy, server.url)
        if mbps is not None:
            return SpeedResult(mbps=mbps, server=server.name)
    return SpeedResult()


async def _download_mbps(proxy: Optional[str], url: str) -> Optional[float]:
    """Общая скорость `SPEED_STREAMS` параллельных закачек; None — данных нет."""
    timeout = httpx.Timeout(SPEED_STALL_SECONDS, connect=SPEED_CONNECT_TIMEOUT)
    deadline = time.perf_counter() + SPEED_MAX_TIME
    streams = [_StreamProgress() for _ in range(SPEED_STREAMS)]
    try:
        # Потолок поверх таймаутов httpx: SOCKS-рукопожатие ими не покрыто, и
        # повисший туннель держал бы замер, а с ним и прогон, бесконечно
        async with asyncio.timeout(SPEED_MAX_TIME + SPEED_STALL_SECONDS):
            async with httpx.AsyncClient(proxy=proxy, timeout=timeout, trust_env=False) as client:
                await asyncio.gather(*(
                    _pull(client, url, deadline, progress) for progress in streams
                ))
    except TimeoutError:
        # Окно вышло на зависшем рукопожатии — годится то, что успело прийти
        pass
    return _aggregate_mbps(streams)


async def _pull(
    client: httpx.AsyncClient, url: str, deadline: float, progress: _StreamProgress,
) -> None:
    headers = {"Range": f"bytes=0-{SPEED_STREAM_BYTES - 1}"}
    try:
        async with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                now = time.perf_counter()
                if not progress.received:
                    progress.first_byte_at = now
                progress.received += len(chunk)
                progress.last_byte_at = now
                if progress.received >= SPEED_STREAM_BYTES or now >= deadline:
                    return
    except httpx.HTTPError:
        # Упавший поток не обрывает соседей, а пришедшее до сбоя — тоже данные
        return


def _aggregate_mbps(streams: list[_StreamProgress]) -> Optional[float]:
    """Скорость всех потоков по общему окну — от первого байта до последнего.

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
