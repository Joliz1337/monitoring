"""QUIC-протоколы: Hysteria2 и TUIC.

Оба живут поверх UDP и умеет их только sing-box. TLS у них обязателен всегда,
поэтому security в ссылке не пишут — выставляем сами.
"""
from __future__ import annotations

from app.services.xray_test.errors import LinkParseError
from app.services.xray_test.models import Protocol, ProxyEndpoint, Security, TlsSettings
from app.services.xray_test.parsers.common import (
    as_bool,
    collect_extra,
    first,
    split_alpn,
    split_userinfo_url,
)


def parse_hysteria2(raw: str) -> ProxyEndpoint:
    url = split_userinfo_url(raw)
    if not url.userinfo:
        raise LinkParseError("Не указан пароль", "hysteria2")

    obfs_type = first(url.params, "obfs")
    obfs_password = first(url.params, "obfs-password", "obfs_password")

    return ProxyEndpoint(
        protocol=Protocol.HYSTERIA2,
        address=url.host,
        port=url.port,
        remark=url.remark,
        password=url.userinfo,
        obfs=(obfs_type, obfs_password or "") if obfs_type else None,
        tls=_quic_tls(url.params),
        extra=collect_extra(url.params),
    )


def parse_tuic(raw: str) -> ProxyEndpoint:
    url = split_userinfo_url(raw)
    uuid, _, password = url.userinfo.partition(":")
    if not uuid:
        raise LinkParseError("Не указан UUID", "tuic")

    return ProxyEndpoint(
        protocol=Protocol.TUIC,
        address=url.host,
        port=url.port,
        remark=url.remark,
        uuid=uuid,
        password=password,
        tls=_quic_tls(url.params),
        extra=collect_extra(url.params),
    )


def _quic_tls(params: dict[str, list[str]]) -> TlsSettings:
    return TlsSettings(
        security=Security.TLS,
        sni=first(params, "sni", "peer"),
        alpn=split_alpn(first(params, "alpn")),
        allow_insecure=as_bool(first(params, "insecure", "allowinsecure", "skip-cert-verify")),
    )
