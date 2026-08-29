"""Общие примитивы разбора прокси-ссылок.

Реальные ссылки приходят из десятка клиентов, каждый со своими вольностями:
base64 в обоих алфавитах и без паддинга, IPv6 в скобках, percent-encoded пароли,
незнакомые query-параметры. Терпимость к этому здесь, чтобы протокольные
парсеры остались короткими.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

from app.services.xray_test.errors import LinkParseError
from app.services.xray_test.models import Security, TlsSettings, Transport, TransportSettings

# Псевдонимы транспортов из разных клиентов → канонический вид ядра
TRANSPORT_ALIASES = {
    "": Transport.TCP,
    "tcp": Transport.TCP,
    "raw": Transport.TCP,
    "ws": Transport.WS,
    "websocket": Transport.WS,
    "grpc": Transport.GRPC,
    "gun": Transport.GRPC,
    "httpupgrade": Transport.HTTPUPGRADE,
    "xhttp": Transport.XHTTP,
    "splithttp": Transport.XHTTP,
    "kcp": Transport.MKCP,
    "mkcp": Transport.MKCP,
    "http": Transport.H2,
    "h2": Transport.H2,
}

SECURITY_ALIASES = {
    "": Security.NONE,
    "none": Security.NONE,
    "tls": Security.TLS,
    "xtls": Security.TLS,
    "reality": Security.REALITY,
}

TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

# Ключи, уже разобранные в TlsSettings/TransportSettings — в extra не попадают
KNOWN_STREAM_KEYS = frozenset({
    "type", "security", "sni", "peer", "alpn", "fp", "allowinsecure", "insecure",
    "pbk", "sid", "spx", "path", "host", "servicename", "mode", "headertype",
    "seed", "flow", "encryption", "servername", "authority",
})


def decode_base64_text(raw: str) -> str:
    """base64 в любом алфавите и с любым паддингом → текст.

    Подписки отдают и стандартный алфавит, и url-safe, и часто без '='.
    """
    cleaned = "".join(raw.split())
    if not cleaned:
        return ""
    normalized = cleaned.replace("-", "+").replace("_", "/")
    padded = normalized + "=" * (-len(normalized) % 4)
    try:
        return base64.b64decode(padded, validate=False).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError) as exc:
        raise LinkParseError(f"Некорректный base64: {exc}") from exc


def split_host_port(netloc: str) -> tuple[str, int]:
    """«host:port» / «[ipv6]:port» → (host, port)."""
    target = netloc.strip()
    if not target:
        raise LinkParseError("Пустой адрес сервера")

    if target.startswith("["):
        closing = target.find("]")
        if closing == -1:
            raise LinkParseError("Незакрытая скобка в IPv6-адресе")
        host = target[1:closing]
        rest = target[closing + 1:]
        port_str = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port_str = target.rpartition(":")
        if not host:
            raise LinkParseError(f"Не указан порт: {target}")

    if not port_str.isdigit():
        raise LinkParseError(f"Порт не число: {target}")
    port = int(port_str)
    if not 1 <= port <= 65535:
        raise LinkParseError(f"Порт вне диапазона 1–65535: {port}")
    if not host:
        raise LinkParseError("Пустой хост")
    return host, port


def first(params: dict[str, list[str]], *keys: str) -> Optional[str]:
    """Первое непустое значение среди синонимичных ключей."""
    for key in keys:
        values = params.get(key)
        if values and values[0]:
            return values[0]
    return None


def as_bool(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in TRUE_VALUES


def split_alpn(value: Optional[str]) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in unquote(value).split(",") if item.strip())


def lowercase_keys(params: dict[str, list[str]]) -> dict[str, list[str]]:
    """Клиенты пишут serviceName и servicename вперемешку."""
    merged: dict[str, list[str]] = {}
    for key, values in params.items():
        merged.setdefault(key.lower(), []).extend(values)
    return merged


def build_tls(params: dict[str, list[str]], *, default_sni: Optional[str] = None) -> TlsSettings:
    security = SECURITY_ALIASES.get((first(params, "security") or "").lower(), Security.NONE)
    sni = first(params, "sni", "peer", "servername") or default_sni
    # REALITY без явного security, но с публичным ключом — так пишут некоторые панели
    if security is Security.NONE and first(params, "pbk"):
        security = Security.REALITY
    return TlsSettings(
        security=security,
        sni=unquote(sni) if sni else None,
        alpn=split_alpn(first(params, "alpn")),
        fingerprint=first(params, "fp"),
        allow_insecure=as_bool(first(params, "allowinsecure", "insecure")),
        reality_public_key=first(params, "pbk"),
        reality_short_id=first(params, "sid"),
        reality_spider_x=unquote(first(params, "spx") or "") or None,
    )


def build_transport(params: dict[str, list[str]]) -> TransportSettings:
    raw_kind = (first(params, "type") or "").lower()
    kind = TRANSPORT_ALIASES.get(raw_kind)
    if kind is None:
        raise LinkParseError(f"Неизвестный транспорт: {raw_kind}")
    path = first(params, "path")
    host = first(params, "host")
    return TransportSettings(
        kind=kind,
        path=unquote(path) if path else None,
        host=unquote(host) if host else None,
        service_name=unquote(first(params, "servicename") or "") or None,
        mode=first(params, "mode"),
        header_type=first(params, "headertype"),
        seed=first(params, "seed"),
        authority=unquote(first(params, "authority") or "") or None,
    )


def collect_extra(params: dict[str, list[str]]) -> tuple[tuple[str, str], ...]:
    """Незнакомые параметры сохраняем — они могут пригодиться в диагностике."""
    return tuple(
        (key, values[0])
        for key, values in sorted(params.items())
        if key not in KNOWN_STREAM_KEYS and values
    )


def decode_remark(fragment: str) -> str:
    return unquote(fragment).strip()


@dataclass(frozen=True)
class UserInfoUrl:
    """Разобранная ссылка вида «схема://креды@host:port?query#remark»."""

    userinfo: str
    host: str
    port: int
    params: dict[str, list[str]]
    remark: str


def split_userinfo_url(raw: str, *, require_userinfo: bool = True) -> UserInfoUrl:
    """Общая форма ссылок vless/trojan/hysteria2/tuic/anytls/socks.

    У socks/http креды необязательны, поэтому требование отключаемо.
    """
    try:
        parts = urlsplit(raw.strip())
    except ValueError as exc:
        raise LinkParseError(f"Ссылку не разобрать: {exc}") from exc
    netloc = parts.netloc
    if "@" not in netloc:
        if require_userinfo:
            raise LinkParseError("В ссылке нет учётных данных перед адресом", parts.scheme)
        netloc = f"@{netloc}"

    userinfo, _, hostport = netloc.rpartition("@")
    host, port = split_host_port(hostport)
    return UserInfoUrl(
        userinfo=unquote(userinfo),
        host=host,
        port=port,
        params=lowercase_keys(parse_qs(parts.query, keep_blank_values=True)),
        remark=decode_remark(parts.fragment),
    )
