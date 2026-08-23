"""Разбор прокси-ссылок: схема → ProxyEndpoint.

Схемы http/https сознательно не поддержаны как прокси-ссылки: пользователь
вставляет в это же поле URL подписки, и трактовать его как HTTP-прокси было бы
хуже, чем честно сказать «не ссылка».
"""
from __future__ import annotations

from typing import Callable

from app.services.xray_test.errors import LinkParseError, UnsupportedProtocolError
from app.services.xray_test.models import ProxyEndpoint
from app.services.xray_test.parsers import quic, shadowsocks, simple, trojan, vless, vmess

PARSERS: dict[str, Callable[[str], ProxyEndpoint]] = {
    "vless": vless.parse,
    "vmess": vmess.parse,
    "trojan": trojan.parse,
    "ss": shadowsocks.parse,
    "shadowsocks": shadowsocks.parse,
    "hysteria2": quic.parse_hysteria2,
    "hy2": quic.parse_hysteria2,
    "tuic": quic.parse_tuic,
    "anytls": simple.parse_anytls,
    "shadowtls": simple.parse_shadowtls,
    "socks": simple.parse_socks,
    "socks5": simple.parse_socks,
}

SUPPORTED_SCHEMES = frozenset(PARSERS)


def looks_like_link(text: str) -> bool:
    scheme, sep, _ = text.strip().partition("://")
    return bool(sep) and scheme.lower() in PARSERS


def parse_link(raw: str) -> ProxyEndpoint:
    """Одна ссылка → конфигурация. Кидает LinkParseError с внятной причиной."""
    text = raw.strip()
    scheme, sep, rest = text.partition("://")
    if not sep or not rest:
        raise LinkParseError("Строка не похожа на ссылку прокси")

    parser = PARSERS.get(scheme.lower())
    if parser is None:
        raise UnsupportedProtocolError(scheme.lower())
    return parser(text)
