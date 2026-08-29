"""Протоколы без собственной обвязки: AnyTLS, ShadowTLS, SOCKS, HTTP."""
from __future__ import annotations

from dataclasses import replace

from app.services.xray_test.errors import LinkParseError
from app.services.xray_test.models import Protocol, ProxyEndpoint, Security
from app.services.xray_test.parsers.common import (
    build_tls,
    collect_extra,
    first,
    split_userinfo_url,
)


def parse_anytls(raw: str) -> ProxyEndpoint:
    url = split_userinfo_url(raw)
    if not url.userinfo:
        raise LinkParseError("Не указан пароль", "anytls")

    tls = build_tls(url.params)
    if tls.security is Security.NONE:
        tls = replace(tls, security=Security.TLS)

    return ProxyEndpoint(
        protocol=Protocol.ANYTLS,
        address=url.host,
        port=url.port,
        remark=url.remark,
        password=url.userinfo,
        tls=tls,
        extra=collect_extra(url.params),
    )


def parse_shadowtls(raw: str) -> ProxyEndpoint:
    url = split_userinfo_url(raw, require_userinfo=False)
    tls = build_tls(url.params)
    if tls.security is Security.NONE:
        tls = replace(tls, security=Security.TLS)

    return ProxyEndpoint(
        protocol=Protocol.SHADOWTLS,
        address=url.host,
        port=url.port,
        remark=url.remark,
        password=url.userinfo or first(url.params, "password"),
        tls=tls,
        extra=collect_extra(url.params),
    )


def parse_socks(raw: str) -> ProxyEndpoint:
    return _plain(raw, Protocol.SOCKS)


def parse_http(raw: str) -> ProxyEndpoint:
    return _plain(raw, Protocol.HTTP)


def _plain(raw: str, protocol: Protocol) -> ProxyEndpoint:
    """socks://user:pass@host:port — пара может отсутствовать целиком."""
    url = split_userinfo_url(raw, require_userinfo=False)
    login, _, password = url.userinfo.partition(":")
    return ProxyEndpoint(
        protocol=protocol,
        address=url.host,
        port=url.port,
        remark=url.remark,
        uuid=login or None,
        password=password or None,
        tls=build_tls(url.params),
        extra=collect_extra(url.params),
    )
