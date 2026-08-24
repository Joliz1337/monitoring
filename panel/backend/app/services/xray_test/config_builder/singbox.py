"""Генерация клиентского конфига sing-box.

Формат сверен с живым бинарником sing-box 1.13.19. Сюда уходят QUIC-протоколы,
AnyTLS/ShadowTLS, HTTP/2-транспорт и всё, где просили не проверять сертификат.
"""
from __future__ import annotations

from typing import Any

from app.services.xray_test.config_builder.batch import BatchEntry
from app.services.xray_test.errors import UnsupportedConfigError
from app.services.xray_test.models import (
    Protocol,
    ProxyEndpoint,
    Security,
    Transport,
    TransportSettings,
)

INBOUND_TAG = "mon-test-in"
OUTBOUND_TAG = "mon-test-out"

TRANSPORT_NAMES = {
    Transport.WS: "ws",
    Transport.GRPC: "grpc",
    Transport.HTTPUPGRADE: "httpupgrade",
    Transport.H2: "http",
}


def build_config(endpoint: ProxyEndpoint, socks_port: int) -> dict[str, Any]:
    return build_batch([BatchEntry("", endpoint, socks_port)])


def build_batch(entries: list[BatchEntry]) -> dict[str, Any]:
    """Один процесс на пачку — см. `xray.build_batch`.

    `final` ставится только у одиночной проверки. В пачке маршрут по умолчанию
    опасен: не сработай правило, трафик ушёл бы через чужую конфигурацию и
    проверка показала бы «работает» для не того сервера. Каждый порт и так
    связан со своим outbound явным правилом.
    """
    route: dict[str, Any] = {
        "rules": [
            {"inbound": [entry.inbound_tag], "outbound": entry.outbound_tag}
            for entry in entries
        ],
    }
    if len(entries) == 1:
        route["final"] = entries[0].outbound_tag

    return {
        # info, а не warn: причина отказа печатается только на этом уровне
        "log": {"level": "info"},
        "inbounds": [{
            "type": "socks",
            "tag": entry.inbound_tag,
            "listen": "127.0.0.1",
            "listen_port": entry.socks_port,
        } for entry in entries],
        "outbounds": [_outbound(entry.endpoint, entry.outbound_tag) for entry in entries],
        "route": route,
    }


def _outbound(endpoint: ProxyEndpoint, tag: str = OUTBOUND_TAG) -> dict[str, Any]:
    outbound: dict[str, Any] = {
        "type": endpoint.protocol.value,
        "tag": tag,
        "server": endpoint.address,
        "server_port": endpoint.port,
    }

    if endpoint.protocol is Protocol.VLESS:
        outbound["uuid"] = endpoint.uuid or ""
        if endpoint.flow:
            outbound["flow"] = endpoint.flow
    elif endpoint.protocol is Protocol.VMESS:
        outbound["uuid"] = endpoint.uuid or ""
        outbound["alter_id"] = endpoint.alter_id
        outbound["security"] = endpoint.encryption or "auto"
    elif endpoint.protocol in (Protocol.TROJAN, Protocol.ANYTLS):
        outbound["password"] = endpoint.password or ""
    elif endpoint.protocol is Protocol.SHADOWSOCKS:
        outbound["method"] = endpoint.method or ""
        outbound["password"] = endpoint.password or ""
    elif endpoint.protocol is Protocol.HYSTERIA2:
        outbound["password"] = endpoint.password or ""
        if endpoint.obfs:
            obfs_type, obfs_password = endpoint.obfs
            outbound["obfs"] = {"type": obfs_type, "password": obfs_password}
    elif endpoint.protocol is Protocol.TUIC:
        outbound["uuid"] = endpoint.uuid or ""
        outbound["password"] = endpoint.password or ""
    elif endpoint.protocol is Protocol.SHADOWTLS:
        outbound["version"] = 3
        outbound["password"] = endpoint.password or ""
    elif endpoint.protocol in (Protocol.SOCKS, Protocol.HTTP):
        if endpoint.uuid:
            outbound["username"] = endpoint.uuid
            outbound["password"] = endpoint.password or ""
    else:
        raise UnsupportedConfigError(f"sing-box не поддерживает протокол {endpoint.protocol.value}")

    tls = _tls(endpoint)
    if tls:
        outbound["tls"] = tls

    transport = _transport(endpoint.transport)
    if transport:
        outbound["transport"] = transport
    return outbound


def _tls(endpoint: ProxyEndpoint) -> dict[str, Any]:
    if endpoint.tls.security is Security.NONE:
        return {}

    tls: dict[str, Any] = {"enabled": True, "server_name": endpoint.effective_sni}
    if endpoint.tls.allow_insecure:
        tls["insecure"] = True
    if endpoint.tls.alpn:
        tls["alpn"] = list(endpoint.tls.alpn)
    if endpoint.tls.fingerprint:
        tls["utls"] = {"enabled": True, "fingerprint": endpoint.tls.fingerprint}

    if endpoint.tls.security is Security.REALITY:
        reality: dict[str, Any] = {
            "enabled": True,
            "public_key": endpoint.tls.reality_public_key or "",
        }
        if endpoint.tls.reality_short_id:
            reality["short_id"] = endpoint.tls.reality_short_id
        tls["reality"] = reality
        # REALITY у sing-box требует utls: без него ядро откажется стартовать
        tls.setdefault("utls", {"enabled": True, "fingerprint": "chrome"})
    return tls


def _transport(transport: TransportSettings) -> dict[str, Any]:
    name = TRANSPORT_NAMES.get(transport.kind)
    if name is None:
        if transport.kind is Transport.TCP:
            return {}
        raise UnsupportedConfigError(f"sing-box не поддерживает транспорт {transport.kind.value}")

    settings: dict[str, Any] = {"type": name}
    if transport.kind is Transport.GRPC:
        settings["service_name"] = transport.service_name or ""
        return settings

    if transport.path:
        settings["path"] = transport.path
    if transport.kind is Transport.WS and transport.host:
        settings["headers"] = {"Host": transport.host}
    elif transport.host:
        settings["host"] = [transport.host] if transport.kind is Transport.H2 else transport.host
    return settings
