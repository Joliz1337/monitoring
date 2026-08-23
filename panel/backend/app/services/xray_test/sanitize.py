"""Маскирование секретов конфигураций.

UUID, пароли и reality-ключи не должны попасть ни в лог задачи, ни в лог
контейнера, ни в ответы API. Отдельно фильтруется вывод самого ядра: xray при
ошибке разбора печатает куски конфига вместе с кредами.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

VISIBLE_HEAD = 4
VISIBLE_TAIL = 4

SECRET_QUERY_KEYS = frozenset({"pbk", "sid", "password", "id", "key", "secret", "spx"})

# UUID и длинные base64-подобные строки в свободном тексте (вывод ядра).
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")
_JSON_SECRET_RE = re.compile(
    r'"(id|password|publicKey|shortId|privateKey|psk|uuid|auth_str|auth)"\s*:\s*"([^"]+)"'
)


def mask_secret(value: str) -> str:
    """Секрет → «abcd…7f21». Короткие значения скрываются целиком."""
    if not value:
        return ""
    if len(value) <= VISIBLE_HEAD + VISIBLE_TAIL:
        return "•" * len(value)
    return f"{value[:VISIBLE_HEAD]}…{value[-VISIBLE_TAIL:]}"


def sanitize_link(raw: str) -> str:
    """Ссылка с замаскированными userinfo и секретными query-параметрами."""
    if not raw:
        return ""
    try:
        parts = urlsplit(raw.strip())
    except ValueError:
        return "<ссылка>"

    netloc = parts.netloc
    if "@" in netloc:
        userinfo, _, hostport = netloc.rpartition("@")
        netloc = f"{mask_secret(userinfo)}@{hostport}"

    query = parts.query
    if query:
        pairs = [
            (key, mask_secret(value) if key.lower() in SECRET_QUERY_KEYS else value)
            for key, value in parse_qsl(query, keep_blank_values=True)
        ]
        query = urlencode(pairs)

    # vmess://<base64 JSON> — весь payload секретный, разбирать нечего
    if parts.scheme == "vmess" and not netloc:
        return f"vmess://{mask_secret(parts.path)}"

    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def sanitize_output(text: str) -> str:
    """Вывод ядра без UUID, паролей и длинных токенов."""
    if not text:
        return ""
    cleaned = _JSON_SECRET_RE.sub(lambda m: f'"{m.group(1)}": "{mask_secret(m.group(2))}"', text)
    cleaned = _UUID_RE.sub(lambda m: mask_secret(m.group(0)), cleaned)
    return _LONG_TOKEN_RE.sub(lambda m: mask_secret(m.group(0)), cleaned)
