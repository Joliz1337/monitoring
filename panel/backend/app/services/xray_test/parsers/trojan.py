"""Trojan: trojan://password@host:port?type=&security=&…#remark

У trojan TLS включён всегда, даже если security в ссылке не указан: без него
протокол не работает, а клиенты этот параметр часто опускают.
"""
from __future__ import annotations

from dataclasses import replace

from app.services.xray_test.errors import LinkParseError
from app.services.xray_test.models import Protocol, ProxyEndpoint, Security
from app.services.xray_test.parsers.common import (
    build_tls,
    build_transport,
    collect_extra,
    split_userinfo_url,
)


def parse(raw: str) -> ProxyEndpoint:
    url = split_userinfo_url(raw)
    if not url.userinfo:
        raise LinkParseError("Не указан пароль", "trojan")

    tls = build_tls(url.params)
    if tls.security is Security.NONE:
        tls = replace(tls, security=Security.TLS)

    return ProxyEndpoint(
        protocol=Protocol.TROJAN,
        address=url.host,
        port=url.port,
        remark=url.remark,
        password=url.userinfo,
        tls=tls,
        transport=build_transport(url.params),
        extra=collect_extra(url.params),
    )
