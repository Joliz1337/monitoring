"""VLESS: vless://uuid@host:port?type=&security=&…#remark"""
from __future__ import annotations

from app.services.xray_test.errors import LinkParseError
from app.services.xray_test.models import Protocol, ProxyEndpoint
from app.services.xray_test.parsers.common import (
    build_tls,
    build_transport,
    collect_extra,
    first,
    split_userinfo_url,
)


def parse(raw: str) -> ProxyEndpoint:
    url = split_userinfo_url(raw)
    if not url.userinfo:
        raise LinkParseError("Не указан UUID", "vless")

    return ProxyEndpoint(
        protocol=Protocol.VLESS,
        address=url.host,
        port=url.port,
        remark=url.remark,
        uuid=url.userinfo,
        flow=first(url.params, "flow"),
        encryption=first(url.params, "encryption") or "none",
        tls=build_tls(url.params),
        transport=build_transport(url.params),
        extra=collect_extra(url.params),
    )
