"""Shadowsocks в трёх формах.

SIP002 — ss://base64(method:password)@host:port#tag; историческая — весь адрес
внутри base64; SS2022 (шифры 2022-blake3-*) оставляет method:password открытым
текстом, потому что пароль там сам по себе base64 и повторное кодирование
ломало бы совместимость.
"""
from __future__ import annotations

from urllib.parse import parse_qs, unquote

from app.services.xray_test.errors import LinkParseError
from app.services.xray_test.models import Protocol, ProxyEndpoint
from app.services.xray_test.parsers.common import (
    collect_extra,
    decode_base64_text,
    decode_remark,
    lowercase_keys,
    split_host_port,
)

SCHEME_PREFIX = "ss://"


def parse(raw: str) -> ProxyEndpoint:
    body = raw.strip()[len(SCHEME_PREFIX):]

    body, _, fragment = body.partition("#")
    body, _, query = body.partition("?")
    remark = decode_remark(fragment)
    params = lowercase_keys(parse_qs(query, keep_blank_values=True))

    if "@" in body:
        userinfo, _, hostport = body.rpartition("@")
        method, password = _split_credentials(unquote(userinfo))
        host, port = split_host_port(hostport)
    else:
        decoded = decode_base64_text(body)
        if "@" not in decoded:
            raise LinkParseError("В теле ss нет адреса сервера", "ss")
        userinfo, _, hostport = decoded.rpartition("@")
        method, password = _split_credentials(userinfo)
        host, port = split_host_port(hostport)

    if not method:
        raise LinkParseError("Не указан метод шифрования", "ss")

    return ProxyEndpoint(
        protocol=Protocol.SHADOWSOCKS,
        address=host,
        port=port,
        remark=remark,
        method=method,
        password=password,
        extra=collect_extra(params),
    )


def _split_credentials(userinfo: str) -> tuple[str, str]:
    """«method:password» — как есть или под base64."""
    if ":" not in userinfo:
        userinfo = decode_base64_text(userinfo)
    method, sep, password = userinfo.partition(":")
    if not sep:
        raise LinkParseError("Учётные данные не в формате method:password", "ss")
    return method.strip(), password
