"""Разбор вставленного JSON-конфига (Xray или sing-box) в единую модель.

Чужой конфиг никогда не запускается как есть: из него извлекаются только
исходящие подключения, а конфиг для запуска панель собирает заново. Пастнутый
файл может содержать inbound на 0.0.0.0, dokodemo-door, api/stats или
clash_api — на ноде с host-сетью это открытый порт наружу, а не косметика.

Побочная выгода: раз конфиг сведён к ProxyEndpoint, вставленный JSON тоже
участвует в мульти-SNI.
"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Optional

from app.services.xray_test.errors import LinkParseError
from app.services.xray_test.models import (
    Protocol,
    ProxyEndpoint,
    Security,
    TlsSettings,
    Transport,
    TransportSettings,
)
from app.services.xray_test.parsers.common import TRANSPORT_ALIASES

# Служебные исходящие: трафик наружу через них не идёт, тестировать нечего
SERVICE_OUTBOUNDS = frozenset({
    "freedom", "blackhole", "dns", "direct", "block", "selector", "urltest", "loopback",
})

# Секции, которые отбрасываются вместе со всем содержимым
DROPPABLE_SECTIONS = (
    "inbounds", "api", "stats", "policy", "experimental", "dns", "route",
    "routing", "reverse", "services", "metrics", "log", "fakedns", "burstObservatory",
    "observatory", "endpoints", "certificate", "ntp",
)


def parse_config(text: str) -> tuple[list[ProxyEndpoint], list[str]]:
    """JSON-конфиг → (конфигурации, названия отброшенных секций).

    Массив на верхнем уровне бывает двух видов: список исходящих подключений и
    список целых конфигов (так подписки отдают набор профилей — каждый со своим
    именем в `remarks`). Различаются по наличию ключа outbounds внутри.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LinkParseError(f"Не разобрать JSON: {exc}") from exc

    if isinstance(data, dict):
        configs = [data]
    elif isinstance(data, list):
        configs = data if _is_config_list(data) else [{"outbounds": data}]
    else:
        raise LinkParseError("Конфиг должен быть объектом или массивом JSON")

    endpoints: list[ProxyEndpoint] = []
    dropped: list[str] = []
    for config in configs:
        if not isinstance(config, dict):
            continue
        found, sections = _endpoints_from_config(config)
        endpoints.extend(found)
        dropped.extend(section for section in sections if section not in dropped)

    if not endpoints:
        raise LinkParseError("В конфиге нет исходящих подключений, которые можно проверить")
    return endpoints, dropped


def _is_config_list(items: list[Any]) -> bool:
    return any(isinstance(item, dict) and "outbounds" in item for item in items)


def _endpoints_from_config(config: dict[str, Any]) -> tuple[list[ProxyEndpoint], list[str]]:
    raw = config.get("outbounds")
    outbounds = raw if isinstance(raw, list) else []
    dropped = [name for name in DROPPABLE_SECTIONS if name in config]
    # Имя профиля из подписки: у отдельных outbounds его нет, а различать
    # «Германия» и «Нидерланды» в списке из сотни серверов необходимо
    profile_name = str(config.get("remarks") or "").strip()

    endpoints: list[ProxyEndpoint] = []
    for item in outbounds:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("protocol") or item.get("type") or "").lower()
        if not kind or kind in SERVICE_OUTBOUNDS:
            continue
        endpoint = _outbound_to_endpoint(item, kind)
        if endpoint is None:
            continue
        if profile_name:
            tag = endpoint.remark
            endpoint = replace(
                endpoint,
                remark=f"{profile_name} · {tag}" if tag else profile_name,
            )
        endpoints.append(endpoint)
    return endpoints, dropped


def _outbound_to_endpoint(item: dict[str, Any], kind: str) -> Optional[ProxyEndpoint]:
    try:
        protocol = Protocol(kind)
    except ValueError:
        return None

    # У Xray адрес спрятан в settings.vnext/servers, у sing-box лежит плоско
    if "server" in item:
        return _from_singbox(item, protocol)
    return _from_xray(item, protocol)


def _from_singbox(item: dict[str, Any], protocol: Protocol) -> Optional[ProxyEndpoint]:
    address = str(item.get("server") or "").strip()
    port = _as_int(item.get("server_port"))
    if not address or port is None:
        return None

    tls_raw = item.get("tls") if isinstance(item.get("tls"), dict) else {}
    reality_raw = tls_raw.get("reality") if isinstance(tls_raw.get("reality"), dict) else {}
    utls_raw = tls_raw.get("utls") if isinstance(tls_raw.get("utls"), dict) else {}

    security = Security.NONE
    if tls_raw.get("enabled"):
        security = Security.REALITY if reality_raw.get("enabled") else Security.TLS

    transport_raw = item.get("transport") if isinstance(item.get("transport"), dict) else {}
    obfs_raw = item.get("obfs") if isinstance(item.get("obfs"), dict) else {}

    return ProxyEndpoint(
        protocol=protocol,
        address=address,
        port=port,
        remark=str(item.get("tag") or ""),
        uuid=_str_or_none(item.get("uuid")) or _str_or_none(item.get("username")),
        password=_str_or_none(item.get("password")),
        method=_str_or_none(item.get("method")),
        alter_id=_as_int(item.get("alter_id")) or 0,
        flow=_str_or_none(item.get("flow")),
        encryption=_str_or_none(item.get("security")),
        obfs=(
            (str(obfs_raw.get("type")), str(obfs_raw.get("password") or ""))
            if obfs_raw.get("type") else None
        ),
        tls=TlsSettings(
            security=security,
            sni=_str_or_none(tls_raw.get("server_name")),
            alpn=tuple(tls_raw.get("alpn") or ()),
            fingerprint=_str_or_none(utls_raw.get("fingerprint")),
            allow_insecure=bool(tls_raw.get("insecure")),
            reality_public_key=_str_or_none(reality_raw.get("public_key")),
            reality_short_id=_str_or_none(reality_raw.get("short_id")),
        ),
        transport=_singbox_transport(transport_raw),
    )


def _singbox_transport(raw: dict[str, Any]) -> TransportSettings:
    kind = TRANSPORT_ALIASES.get(str(raw.get("type") or "").lower(), Transport.TCP)
    host = raw.get("host")
    if isinstance(host, list):
        host = host[0] if host else None
    headers = raw.get("headers") if isinstance(raw.get("headers"), dict) else {}
    return TransportSettings(
        kind=kind,
        path=_str_or_none(raw.get("path")),
        host=_str_or_none(host) or _str_or_none(headers.get("Host")),
        service_name=_str_or_none(raw.get("service_name")),
    )


def _from_xray(item: dict[str, Any], protocol: Protocol) -> Optional[ProxyEndpoint]:
    settings = item.get("settings") if isinstance(item.get("settings"), dict) else {}
    vnext = settings.get("vnext")
    servers = settings.get("servers")

    if isinstance(vnext, list) and vnext:
        peer = vnext[0]
        users = peer.get("users") or [{}]
        user = users[0] if isinstance(users, list) and users else {}
        credentials = {
            "uuid": _str_or_none(user.get("id")),
            "alter_id": _as_int(user.get("alterId")) or 0,
            "flow": _str_or_none(user.get("flow")),
            "encryption": _str_or_none(user.get("encryption")) or _str_or_none(user.get("security")),
        }
    elif isinstance(servers, list) and servers:
        peer = servers[0]
        users = peer.get("users") or []
        user = users[0] if isinstance(users, list) and users else {}
        credentials = {
            "password": _str_or_none(peer.get("password")) or _str_or_none(user.get("pass")),
            "method": _str_or_none(peer.get("method")),
            "uuid": _str_or_none(user.get("user")),
        }
    else:
        return None

    address = str(peer.get("address") or "").strip()
    port = _as_int(peer.get("port"))
    if not address or port is None:
        return None

    stream = item.get("streamSettings") if isinstance(item.get("streamSettings"), dict) else {}
    return ProxyEndpoint(
        protocol=protocol,
        address=address,
        port=port,
        remark=str(item.get("tag") or ""),
        tls=_xray_tls(stream),
        transport=_xray_transport(stream),
        **credentials,
    )


def _xray_tls(stream: dict[str, Any]) -> TlsSettings:
    security_name = str(stream.get("security") or "none").lower()
    security = Security.REALITY if security_name == "reality" else (
        Security.TLS if security_name in ("tls", "xtls") else Security.NONE
    )
    raw = stream.get("realitySettings") if security is Security.REALITY else stream.get("tlsSettings")
    raw = raw if isinstance(raw, dict) else {}

    return TlsSettings(
        security=security,
        sni=_str_or_none(raw.get("serverName")),
        alpn=tuple(raw.get("alpn") or ()),
        fingerprint=_str_or_none(raw.get("fingerprint")),
        allow_insecure=bool(raw.get("allowInsecure")),
        reality_public_key=_str_or_none(raw.get("publicKey")) or _str_or_none(raw.get("password")),
        reality_short_id=_str_or_none(raw.get("shortId")),
        reality_spider_x=_str_or_none(raw.get("spiderX")),
    )


def _xray_transport(stream: dict[str, Any]) -> TransportSettings:
    network = str(stream.get("network") or "tcp").lower()
    kind = TRANSPORT_ALIASES.get(network, Transport.TCP)
    raw = stream.get(f"{network}Settings")
    raw = raw if isinstance(raw, dict) else {}

    headers = raw.get("headers") if isinstance(raw.get("headers"), dict) else {}
    host = raw.get("host") or headers.get("Host")
    if isinstance(host, list):
        host = host[0] if host else None

    header = raw.get("header") if isinstance(raw.get("header"), dict) else {}
    return TransportSettings(
        kind=kind,
        path=_str_or_none(raw.get("path")),
        host=_str_or_none(host),
        service_name=_str_or_none(raw.get("serviceName")),
        mode=_grpc_mode(raw),
        header_type=_str_or_none(header.get("type")),
        seed=_str_or_none(raw.get("seed")),
        authority=_str_or_none(raw.get("authority")),
        xhttp_extra=_xhttp_extra(kind, raw),
    )


def _xhttp_extra(kind: Transport, raw: dict[str, Any]) -> Optional[str]:
    """xhttpSettings.extra — часть протокола, а не тюнинг.

    Кастомные ключи query, аплоад GET-ом, паддинг: сервер с зеркальной
    настройкой отвечает 403 клиенту без такого же extra, поэтому блок едет в
    тестовый конфиг без изменений.
    """
    if kind is not Transport.XHTTP:
        return None
    extra = raw.get("extra")
    if not isinstance(extra, dict) or not extra:
        return None
    return json.dumps(extra, ensure_ascii=False, sort_keys=True)


def _grpc_mode(raw: dict[str, Any]) -> Optional[str]:
    """Режим gRPC пишут и строкой, и булевым — второе означает multiMode.

    Без этого `"mode": true` превратился бы в строку «True», не совпал бы ни с
    одним известным режимом и мультиплексирование молча потерялось бы.
    """
    mode = raw.get("mode")
    if isinstance(mode, bool):
        return "multi" if mode else None
    text = _str_or_none(mode)
    if text is not None:
        return text
    return "multi" if raw.get("multiMode") is True else None


def _str_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
