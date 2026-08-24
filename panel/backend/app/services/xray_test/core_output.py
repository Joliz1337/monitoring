"""Разбор вывода прокси-ядра: из простыни лога — короткая причина отказа.

Ядро не говорит «не работает» — оно печатает цепочку обёрток вида
«failed to process outbound traffic > … > common/retry: [dial tcp …: connection
refused] > common/retry: all retry attempts failed». Суть здесь в скобках,
остальное — служебный контекст, который оператору ничего не даёт.

Кроме текста из вывода выводится код подсказки: по нему интерфейс показывает
человеческое объяснение, что именно проверить.
"""
from __future__ import annotations

import re
from typing import Optional

MAX_DETAIL = 300

# Служебные строки старта — в них никогда нет причины отказа
_NOISE = (
    "Reading config",
    "started",
    "A unified platform",
    "Xray ",
    "accepted ",
    "sing-box started",
)

_BRACKET_RE = re.compile(r"\[([^\[\]]{6,})\]")
_TIMESTAMP_RE = re.compile(r"^\d{4}/\d{2}/\d{2} [\d:.]+\s*")
_LEVEL_RE = re.compile(r"^\[(Info|Warning|Error|Debug)\]\s*", re.IGNORECASE)
_ID_RE = re.compile(r"^\[\d+\]\s*")

# Порядок важен: более частный шаблон должен стоять раньше общего
HINTS: tuple[tuple[str, str], ...] = (
    ("CERT_MISMATCH", r"certificate is valid for"),
    ("CERT_UNTRUSTED", r"x509:|certificate signed by unknown|certificate has expired"),
    ("CONN_REFUSED", r"connection refused|actively refused|econnrefused"),
    ("CONN_RESET", r"connection reset|forcibly closed|econnreset"),
    ("IO_TIMEOUT", r"i/o timeout|context deadline exceeded|timeout awaiting"),
    ("DNS_FAIL", r"no such host|server misbehaving|lookup .* failed"),
    ("AUTH_FAILED", r"invalid user|not authenticated|authentication failed|invalid request user"),
    ("REALITY_REJECTED", r"reality"),
    ("PROTOCOL_MISMATCH", r"first payload|invalid protocol|unknown protocol|wrong version"),
    ("NO_ROUTE", r"no route to host|network is unreachable"),
)


def clean_line(line: str) -> str:
    """Убрать отметку времени, уровень и идентификатор соединения."""
    text = _TIMESTAMP_RE.sub("", line.strip())
    text = _LEVEL_RE.sub("", text)
    return _ID_RE.sub("", text).strip()


def is_noise(line: str) -> bool:
    return any(marker in line for marker in _NOISE)


def extract_reason(output: str) -> tuple[str, Optional[str]]:
    """Вывод ядра → (короткая причина, код подсказки).

    Причина берётся из последних квадратных скобок: именно там ядро печатает
    исходную ошибку, а вокруг — цепочка «не смог обработать» разной глубины.
    """
    if not output:
        return "", None

    meaningful = [
        clean_line(line) for line in output.splitlines()
        if line.strip() and not is_noise(line)
    ]
    # Только строки об ошибке: обычный рабочий вывод («tunneling request to …»)
    # в роли причины выглядел бы объяснением, ничего не объясняя
    failures = [line for line in meaningful if "fail" in line.lower() or "error" in line.lower()]
    if not failures:
        return "", None
    source = failures[-1]

    brackets = _BRACKET_RE.findall(source)
    # Последние скобки — сама ошибка; «all retry attempts failed» это уже итог
    detail = brackets[-1].strip() if brackets else source
    return detail[:MAX_DETAIL], detect_hint(source)


def detect_hint(text: str) -> Optional[str]:
    lowered = text.lower()
    for code, pattern in HINTS:
        if re.search(pattern, lowered):
            return code
    return None
