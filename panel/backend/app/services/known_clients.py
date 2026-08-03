"""Реестр известных VPN-клиентов для проверки User-Agent аномалий.

Каждая строка реестра — маска, сравниваемая с началом User-Agent (регистронезависимо):
`*` — любое количество любых символов, `?` — ровно один символ, остальное — буквально.
`Happ/` покрывает любые версии Happ, `*https*` ловит «https» в любом месте строки.
UA, не подошедший ни под одну маску, считается аномалией.

Оператор может заменить реестр своим списком в настройках аномалий
(`remnawave_settings.anomaly_ua_patterns`); пустое значение возвращает встроенный список.
"""

import re

DEFAULT_UA_PATTERNS = [
    "v2raytun/ios",
    "v2raytun/android",
    "v2raytun/windows",
    "Clash-Meta/Prizrak-Box",
    "Happ/",
    "FlClashX/",
    "FlClash X/",
    "INCY/",
    "HiddifyNext/",
    "Hiddify/",
    "Flowvy/",
    "prizrak-box/",
    "koala-clash/",
]


def _parse_lines(raw: str | None) -> list[str]:
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def _mask_to_regex(mask: str) -> str:
    return re.escape(mask).replace(r"\*", ".*").replace(r"\?", ".")


def default_ua_text() -> str:
    return "\n".join(DEFAULT_UA_PATTERNS)


def build_ua_pattern(raw: str | None) -> re.Pattern:
    """Скомпилированный реестр: пользовательский список или встроенный, если пусто.

    Маски экранируются целиком, поэтому скомпилироваться не могут только
    в теории — но детектор в любом случае не должен падать из-за содержимого БД.
    """
    lines = _parse_lines(raw) or DEFAULT_UA_PATTERNS
    try:
        return re.compile("^(?:" + "|".join(_mask_to_regex(line) for line in lines) + ")", re.IGNORECASE)
    except re.error:
        return re.compile("^(?:" + "|".join(_mask_to_regex(line) for line in DEFAULT_UA_PATTERNS) + ")", re.IGNORECASE)
