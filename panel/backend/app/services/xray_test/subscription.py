"""Загрузка и разбор подписок.

URL сюда вводит оператор, а запрос выполняет сервер панели — у которого есть
доступ к базе, к сокету Docker и ко всем нодам. Поэтому адрес проверяется до
запроса (все A/AAAA-записи), на каждом редиректе отдельно, и ещё раз по факту
установленного соединения: DNS может «перевернуться» между проверкой и
коннектом.

Форматы ответа: base64, список ссылок текстом, JSON-конфиг Xray или sing-box.
Clash YAML осознанно не поддержан — это отдельный маппер и лишняя зависимость,
честное сообщение полезнее наполовину рабочего импорта.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from urllib.parse import urlsplit

import httpx

from app.services.net_utils import is_public_range, resolve_panel_ip
from app.services.xray_test.errors import (
    LinkParseError,
    SubscriptionFetchError,
    SubscriptionTooLargeError,
    UnknownSubscriptionFormatError,
    UnsafeTargetError,
)
from app.services.xray_test.models import ProxyEndpoint
from app.services.xray_test.parsers import json_config, looks_like_link, parse_link
from app.services.xray_test.parsers.common import decode_base64_text
from app.services.xray_test.sanitize import sanitize_link

logger = logging.getLogger(__name__)

MAX_SUBSCRIPTION_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 15.0
DEFAULT_USER_AGENT = "v2rayNG/1.9.24"

# Клиенты подписок отдают разный формат в зависимости от User-Agent
KNOWN_USER_AGENTS = {
    "v2rayng": "v2rayNG/1.9.24",
    "clash": "clash-verge/v1.7.7",
    "happ": "Happ/1.0",
    "singbox": "sing-box/1.13.19",
    "browser": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}


class SubscriptionFormat(str, Enum):
    BASE64 = "base64"
    PLAIN = "plain"
    XRAY_JSON = "xray_json"
    SINGBOX_JSON = "singbox_json"


@dataclass
class LineError:
    line: int
    preview: str
    reason: str


@dataclass
class SubscriptionContent:
    format: SubscriptionFormat
    endpoints: list[ProxyEndpoint]
    links: list[Optional[str]]
    errors: list[LineError]
    dropped_sections: list[str]


async def fetch_subscription(url: str, user_agent: Optional[str] = None) -> str:
    """Скачать тело подписки, не позволив увести запрос во внутреннюю сеть."""
    current = url.strip()
    headers = {"User-Agent": user_agent or DEFAULT_USER_AGENT, "Accept": "*/*"}

    async with httpx.AsyncClient(
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=10.0, pool=10.0),
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await assert_public_target(current)
            try:
                async with client.stream("GET", current, headers=headers) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise SubscriptionFetchError("Редирект без адреса назначения")
                        current = str(response.url.join(location))
                        continue

                    _assert_peer_is_public(response)
                    if response.status_code >= 400:
                        raise SubscriptionFetchError(
                            f"Сервер подписки ответил HTTP {response.status_code}"
                        )
                    return await _read_limited(response)
            except httpx.TimeoutException as exc:
                raise SubscriptionFetchError("Таймаут загрузки подписки") from exc
            except httpx.HTTPError as exc:
                raise SubscriptionFetchError(f"Ошибка загрузки подписки: {exc}") from exc

    raise SubscriptionFetchError(f"Слишком много редиректов (больше {MAX_REDIRECTS})")


async def assert_public_target(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeTargetError(f"Допустимы только http и https, получено: {parts.scheme or '—'}")

    host = parts.hostname
    if not host:
        raise UnsafeTargetError("В адресе подписки нет хоста")

    addresses = await _resolve_all(host)
    if not addresses:
        raise SubscriptionFetchError(f"Не удалось определить адрес хоста {host}")

    for address in addresses:
        _assert_public_ip(address, host)

    panel_ip = await resolve_panel_ip()
    if panel_ip and panel_ip in addresses:
        raise UnsafeTargetError("Адрес подписки указывает на саму панель")


def detect_format(text: str) -> SubscriptionFormat:
    stripped = text.strip()
    if not stripped:
        raise UnknownSubscriptionFormatError("Подписка пуста")

    if stripped[0] in "{[":
        return _detect_json_format(stripped)

    if _looks_like_plain_links(stripped):
        return SubscriptionFormat.PLAIN

    decoded = _try_base64(stripped)
    if decoded is not None:
        return (
            _detect_json_format(decoded.strip())
            if decoded.strip()[:1] in "{["
            else SubscriptionFormat.BASE64
        )

    raise UnknownSubscriptionFormatError(
        f"Формат ответа не распознан: {sanitize_link(stripped[:120])}"
    )


def parse_subscription(text: str) -> SubscriptionContent:
    """Тело подписки → конфигурации, с построчным списком ошибок.

    Частичный успех обязателен: одна битая строка из восьмидесяти не должна
    отменять импорт остальных.
    """
    detected = detect_format(text)

    if detected in (SubscriptionFormat.XRAY_JSON, SubscriptionFormat.SINGBOX_JSON):
        payload = text.strip()
        if payload[:1] not in "{[":
            payload = decode_base64_text(payload)
        endpoints, dropped = json_config.parse_config(payload)
        return SubscriptionContent(
            format=detected,
            endpoints=endpoints,
            links=[None] * len(endpoints),
            errors=[],
            dropped_sections=dropped,
        )

    body = text if detected is SubscriptionFormat.PLAIN else decode_base64_text(text)
    endpoints, links, errors = _parse_links_block(body)
    if not endpoints and errors:
        raise UnknownSubscriptionFormatError("Ни одна строка подписки не разобрана как ссылка")
    return SubscriptionContent(
        format=detected, endpoints=endpoints, links=links, errors=errors, dropped_sections=[]
    )


def parse_links_text(text: str) -> tuple[list[ProxyEndpoint], list[Optional[str]], list[LineError]]:
    """Разбор пачки ссылок, вставленных руками."""
    return _parse_links_block(text)


def _parse_links_block(
    body: str,
) -> tuple[list[ProxyEndpoint], list[Optional[str]], list[LineError]]:
    endpoints: list[ProxyEndpoint] = []
    links: list[Optional[str]] = []
    errors: list[LineError] = []

    for number, raw in enumerate(body.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        try:
            endpoints.append(parse_link(line))
            links.append(line)
        except LinkParseError as exc:
            errors.append(LineError(number, sanitize_link(line)[:120], str(exc)))
        except Exception as exc:  # noqa: BLE001 — одна строка не должна валить импорт
            errors.append(LineError(number, sanitize_link(line)[:120], str(exc)))
    return endpoints, links, errors


def _detect_json_format(text: str) -> SubscriptionFormat:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UnknownSubscriptionFormatError(f"Похоже на JSON, но не разбирается: {exc}") from exc

    if isinstance(data, dict):
        outbounds = data.get("outbounds")
        if isinstance(outbounds, list) and outbounds:
            first = outbounds[0] if isinstance(outbounds[0], dict) else {}
            if "type" in first or "route" in data or "experimental" in data:
                return SubscriptionFormat.SINGBOX_JSON
        return SubscriptionFormat.XRAY_JSON
    if isinstance(data, list):
        return SubscriptionFormat.XRAY_JSON
    raise UnknownSubscriptionFormatError("JSON не является объектом или массивом")


def _looks_like_plain_links(text: str) -> bool:
    for line in text.splitlines():
        candidate = line.strip()
        if candidate and not candidate.startswith("#"):
            return looks_like_link(candidate)
    return False


def _try_base64(text: str) -> Optional[str]:
    try:
        decoded = decode_base64_text(text)
    except LinkParseError:
        return None
    return decoded if "://" in decoded else None


async def _read_limited(response: httpx.Response) -> str:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_SUBSCRIPTION_BYTES:
            raise SubscriptionTooLargeError(
                f"Ответ больше {MAX_SUBSCRIPTION_BYTES // 1024} КБ"
            )
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


async def _resolve_all(host: str) -> list[str]:
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, type=socket.SOCK_STREAM
        )
    except (socket.gaierror, OSError) as exc:
        raise SubscriptionFetchError(f"Хост {host} не резолвится: {exc}") from exc
    return sorted({info[4][0] for info in infos})


def _assert_public_ip(address: str, host: str) -> None:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise UnsafeTargetError(f"Некорректный адрес {address}") from exc

    if parsed.version == 6:
        if (
            parsed.is_private or parsed.is_loopback or parsed.is_link_local
            or parsed.is_reserved or parsed.is_multicast or parsed.is_unspecified
        ):
            raise UnsafeTargetError(f"{host} указывает во внутреннюю сеть ({address})")
        return

    if not is_public_range(address):
        raise UnsafeTargetError(f"{host} указывает во внутреннюю сеть ({address})")


def _assert_peer_is_public(response: httpx.Response) -> None:
    """Последняя проверка — по фактически установленному соединению.

    Между резолвом и коннектом DNS может отдать другой адрес; здесь ответ
    отбрасывается до чтения тела.
    """
    stream = response.extensions.get("network_stream")
    peer = stream.get_extra_info("server_addr") if stream else None
    if not peer:
        return
    address = peer[0] if isinstance(peer, (tuple, list)) else str(peer)
    _assert_public_ip(str(address), str(response.url.host))
