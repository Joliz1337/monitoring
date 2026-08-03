"""Реестр известных VPN-клиентов для проверки User-Agent аномалий.

Каждая строка реестра — regex-фрагмент, проверяемый с начала User-Agent
(регистронезависимо). UA, не подошедший ни под один фрагмент, считается аномалией.
Оператор может заменить реестр своим списком в настройках аномалий
(`remnawave_settings.anomaly_ua_patterns`); пустое значение возвращает встроенный список.
"""

import re

DEFAULT_UA_PATTERNS = [
    "v2raytun/(ios|android|windows)",
    "Clash-Meta/Prizrak-Box",
    "Happ/",
    "FlClash ?X/",
    "INCY/",
    "HiddifyNext/",
    "Hiddify/",
    "Flowvy/",
    "prizrak-box/",
    "koala-clash/",
]


def _parse_lines(raw: str | None) -> list[str]:
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def default_ua_text() -> str:
    return "\n".join(DEFAULT_UA_PATTERNS)


def validate_ua_patterns(raw: str | None) -> list[str]:
    """Все ошибки компиляции regex с номерами строк; пустой список — текст корректен."""
    errors: list[str] = []
    for line_no, line in enumerate((raw or "").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            re.compile(line, re.IGNORECASE)
        except re.error as e:
            errors.append(f"строка {line_no}: некорректный паттерн «{line}»: {e}")
    return errors


def build_ua_pattern(raw: str | None) -> re.Pattern:
    """Скомпилированный реестр: пользовательский список или встроенный, если пусто.

    Некорректное содержимое БД не роняет детектор — откат на встроенный список
    (формат проверяется при сохранении настроек).
    """
    lines = _parse_lines(raw) or DEFAULT_UA_PATTERNS
    try:
        return re.compile("^(?:" + "|".join(lines) + ")", re.IGNORECASE)
    except re.error:
        return re.compile("^(?:" + "|".join(DEFAULT_UA_PATTERNS) + ")", re.IGNORECASE)
