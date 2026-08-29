"""Генерация клиентского конфига Xray.

Формат сверен с живым бинарником Xray 26.3.27: legacy-форма vnext/servers и
имена транспортов tcp/ws/grpc/httpupgrade/xhttp/kcp приняты ядром. Поля,
удалённые в 26 (allowInsecure, mKCP seed/header, network=http), сюда не
попадают — конфигурации с ними уводятся к sing-box ещё на выборе ядра.
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

NETWORK_NAMES = {
    Transport.TCP: "tcp",
    Transport.WS: "ws",
    Transport.GRPC: "grpc",
    Transport.HTTPUPGRADE: "httpupgrade",
    Transport.XHTTP: "xhttp",
    Transport.MKCP: "kcp",
}


def build_config(endpoint: ProxyEndpoint, socks_port: int) -> dict[str, Any]:
    return build_batch([BatchEntry("", endpoint, socks_port)])


def build_batch(entries: list[BatchEntry]) -> dict[str, Any]:
    """Один процесс на пачку: свой socks-порт и свой outbound на каждую проверку.

    Ядро держит сколько угодно inbound'ов, а маршрут `inboundTag → outboundTag`
    жёстко связывает порт с конфигурацией — поэтому вместо процесса на ячейку
    хватает одного на всю пачку. Теги содержат номер ячейки: в логе ядра строки
    соединения помечены `[in → out]`, и только по ним ошибку можно вернуть той
    проверке, которой она принадлежит.
    """
    return {
        # info, а не warning: на warning ядро молчит о причинах отказа,
        # а именно они и нужны в результате проверки
        "log": {"loglevel": "info"},
        "inbounds": [{
            "tag": entry.inbound_tag,
            "listen": "127.0.0.1",
            "port": entry.socks_port,
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": False},
        } for entry in entries],
        "outbounds": [{
            "tag": entry.outbound_tag,
            "protocol": _protocol_name(entry.endpoint.protocol),
            "settings": _settings(entry.endpoint),
            "streamSettings": _stream_settings(entry.endpoint),
        } for entry in entries],
        "routing": {"rules": [{
            "type": "field",
            "inboundTag": [entry.inbound_tag],
            "outboundTag": entry.outbound_tag,
        } for entry in entries]},
    }


def _protocol_name(protocol: Protocol) -> str:
    if protocol is Protocol.SHADOWSOCKS:
        return "shadowsocks"
    return protocol.value


def _settings(endpoint: ProxyEndpoint) -> dict[str, Any]:
    if endpoint.protocol is Protocol.VLESS:
        user: dict[str, Any] = {
            "id": endpoint.uuid or "",
            "encryption": endpoint.encryption or "none",
        }
        if endpoint.flow:
            user["flow"] = endpoint.flow
        return {"vnext": [{"address": endpoint.address, "port": endpoint.port, "users": [user]}]}

    if endpoint.protocol is Protocol.VMESS:
        return {"vnext": [{
            "address": endpoint.address,
            "port": endpoint.port,
            "users": [{
                "id": endpoint.uuid or "",
                "alterId": endpoint.alter_id,
                "security": endpoint.encryption or "auto",
            }],
        }]}

    if endpoint.protocol is Protocol.TROJAN:
        return {"servers": [{
            "address": endpoint.address,
            "port": endpoint.port,
            "password": endpoint.password or "",
        }]}

    if endpoint.protocol is Protocol.SHADOWSOCKS:
        return {"servers": [{
            "address": endpoint.address,
            "port": endpoint.port,
            "method": endpoint.method or "",
            "password": endpoint.password or "",
        }]}

    if endpoint.protocol in (Protocol.SOCKS, Protocol.HTTP):
        server: dict[str, Any] = {"address": endpoint.address, "port": endpoint.port}
        if endpoint.uuid:
            server["users"] = [{"user": endpoint.uuid, "pass": endpoint.password or ""}]
        return {"servers": [server]}

    raise UnsupportedConfigError(f"Xray не поддерживает протокол {endpoint.protocol.value}")


def _stream_settings(endpoint: ProxyEndpoint) -> dict[str, Any]:
    transport = endpoint.transport
    network = NETWORK_NAMES.get(transport.kind)
    if network is None:
        raise UnsupportedConfigError(f"Xray не поддерживает транспорт {transport.kind.value}")

    stream: dict[str, Any] = {"network": network, "security": endpoint.tls.security.value}

    if endpoint.tls.security is Security.TLS:
        stream["tlsSettings"] = _tls_settings(endpoint)
    elif endpoint.tls.security is Security.REALITY:
        stream["realitySettings"] = _reality_settings(endpoint)

    transport_settings = _transport_settings(transport, endpoint.effective_sni)
    if transport_settings:
        stream[f"{network}Settings"] = transport_settings
    return stream


def _tls_settings(endpoint: ProxyEndpoint) -> dict[str, Any]:
    settings: dict[str, Any] = {"serverName": endpoint.effective_sni}
    if endpoint.tls.alpn:
        settings["alpn"] = list(endpoint.tls.alpn)
    if endpoint.tls.fingerprint:
        settings["fingerprint"] = endpoint.tls.fingerprint
    return settings


def _reality_settings(endpoint: ProxyEndpoint) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "serverName": endpoint.effective_sni,
        "publicKey": endpoint.tls.reality_public_key or "",
        "fingerprint": endpoint.tls.fingerprint or "chrome",
    }
    if endpoint.tls.reality_short_id:
        settings["shortId"] = endpoint.tls.reality_short_id
    if endpoint.tls.reality_spider_x:
        settings["spiderX"] = endpoint.tls.reality_spider_x
    return settings


def _transport_settings(transport: TransportSettings, sni: str) -> dict[str, Any]:
    if transport.kind is Transport.WS:
        settings: dict[str, Any] = {"path": transport.path or "/"}
        if transport.host:
            settings["headers"] = {"Host": transport.host}
        return settings

    if transport.kind is Transport.HTTPUPGRADE:
        settings = {"path": transport.path or "/"}
        if transport.host:
            settings["host"] = transport.host
        return settings

    if transport.kind is Transport.XHTTP:
        settings = {"path": transport.path or "/"}
        if transport.host:
            settings["host"] = transport.host
        if transport.mode:
            settings["mode"] = transport.mode
        return settings

    if transport.kind is Transport.GRPC:
        settings = {
            "serviceName": transport.service_name or "",
            "multiMode": (transport.mode or "").lower() in ("multi", "gun-multi"),
        }
        if transport.authority:
            settings["authority"] = transport.authority
        return settings

    if transport.kind is Transport.TCP and transport.header_type == "http":
        # HTTP-маскировка: Host берётся из host транспорта, иначе из SNI
        return {"header": {
            "type": "http",
            "request": {
                "headers": {"Host": [transport.host or sni]},
                "path": [transport.path or "/"],
            },
        }}

    return {}
