"""VMess в двух несовместимых формах.

Историческая (v2rayN) — vmess://<base64 JSON> с однобуквенными ключами; новая —
обычная ссылка vmess://uuid@host:port?…, как у vless. В подписках встречаются
обе, поэтому форма определяется по содержимому, а не по длине.
"""
from __future__ import annotations

import json
from typing import Any

from app.services.xray_test.errors import LinkParseError
from app.services.xray_test.models import (
    Protocol,
    ProxyEndpoint,
    Security,
    TlsSettings,
    TransportSettings,
)
from app.services.xray_test.parsers.common import (
    SECURITY_ALIASES,
    TRANSPORT_ALIASES,
    build_tls,
    build_transport,
    collect_extra,
    decode_base64_text,
    first,
    split_alpn,
    split_userinfo_url,
)

SCHEME_PREFIX = "vmess://"


def parse(raw: str) -> ProxyEndpoint:
    payload = raw.strip()[len(SCHEME_PREFIX):]
    if "@" in payload.split("?", 1)[0]:
        return _parse_uri_form(raw)
    return _parse_legacy_form(payload)


def _parse_uri_form(raw: str) -> ProxyEndpoint:
    url = split_userinfo_url(raw)
    if not url.userinfo:
        raise LinkParseError("Не указан UUID", "vmess")
    return ProxyEndpoint(
        protocol=Protocol.VMESS,
        address=url.host,
        port=url.port,
        remark=url.remark,
        uuid=url.userinfo,
        encryption=first(url.params, "encryption") or "auto",
        tls=build_tls(url.params),
        transport=build_transport(url.params),
        extra=collect_extra(url.params),
    )


def _parse_legacy_form(payload: str) -> ProxyEndpoint:
    try:
        data: dict[str, Any] = json.loads(decode_base64_text(payload))
    except json.JSONDecodeError as exc:
        raise LinkParseError(f"Тело vmess не является JSON: {exc}", "vmess") from exc
    if not isinstance(data, dict):
        raise LinkParseError("Тело vmess не объект JSON", "vmess")

    address = str(data.get("add") or "").strip()
    if not address:
        raise LinkParseError("Не указан адрес сервера", "vmess")

    port = _as_int(data.get("port"), field="port")
    net = str(data.get("net") or "tcp").lower()
    kind = TRANSPORT_ALIASES.get(net)
    if kind is None:
        raise LinkParseError(f"Неизвестный транспорт: {net}", "vmess")

    security = SECURITY_ALIASES.get(str(data.get("tls") or "").lower(), Security.NONE)
    host_header = str(data.get("host") or "").strip() or None
    header_type = str(data.get("type") or "").strip() or None

    return ProxyEndpoint(
        protocol=Protocol.VMESS,
        address=address,
        port=port,
        remark=str(data.get("ps") or "").strip(),
        uuid=str(data.get("id") or "").strip(),
        alter_id=_as_int(data.get("aid"), default=0),
        encryption=str(data.get("scy") or "auto").strip() or "auto",
        tls=TlsSettings(
            security=security,
            sni=str(data.get("sni") or "").strip() or host_header,
            alpn=split_alpn(str(data.get("alpn") or "")),
            fingerprint=str(data.get("fp") or "").strip() or None,
            allow_insecure=str(data.get("allowInsecure") or "").lower() in ("1", "true"),
        ),
        transport=TransportSettings(
            kind=kind,
            path=str(data.get("path") or "").strip() or None,
            host=host_header,
            # v2rayN кладёт имя grpc-сервиса в path, отдельного поля у него нет
            service_name=str(data.get("path") or "").strip() or None if net == "grpc" else None,
            header_type=header_type,
        ),
    )


def _as_int(value: Any, *, default: int | None = None, field: str = "") -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        if default is not None:
            return default
        raise LinkParseError(f"Поле {field} не число: {value!r}", "vmess") from None
