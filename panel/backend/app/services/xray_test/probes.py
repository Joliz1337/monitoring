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
SPEED_URL = "https://speed.cloudflare.com/__down?bytes={size}"

TCP_ATTEMPTS = 3
TCP_TIMEOUT = 3.0
TLS_TIMEOUT = 5.0
HTTP_TIMEOUT = 10.0
SPEED_TIMEOUT = 20.0
SPEED_BYTES = 10_000_000
DEGRADED_RTT_MS = 1500.0


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


async def download_speed(socks_port: int, size: int = SPEED_BYTES) -> Optional[float]:
    """Мбит/с на скачивании тестового файла через прокси."""
    proxy = f"socks5://127.0.0.1:{socks_port}"
    received = 0
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=SPEED_TIMEOUT, trust_env=False) as client:
            async with client.stream("GET", SPEED_URL.format(size=size)) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
    except httpx.HTTPError:
        return None

    elapsed = time.perf_counter() - started
    if elapsed <= 0 or received == 0:
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
