"""Белый список аномалий Remnawave: glob-правила по имени или ID пользователя.

Одна строка — одно правило. Синтаксис паттерна — глоб (fnmatch):
`*` — любая последовательность символов, `?` — один символ,
`[abc]`/`[0-9]` — символ из набора, `[!abc]` — любой символ кроме набора.

Правило можно ограничить одним типом проверки префиксом `тип:`
(`ip`, `hwid`, `ua`, `devdata`, `traffic`); без префикса действует на все типы.
Пустые строки и текст после `#` игнорируются.

    vip-*                # все проверки для vip-пользователей
    ip: reseller-[0-9]*  # только проверка IP-лимита
    traffic: 4521        # только трафик, по числовому ID
"""

import fnmatch
import re
from dataclasses import dataclass

ANOMALY_SCOPES = ("ip", "hwid", "ua", "devdata", "traffic")
_SCOPE_ALL = "*"


@dataclass(frozen=True)
class WhitelistRule:
    scope: str
    pattern: str
    regex: re.Pattern


def _parse_line(line: str) -> WhitelistRule | None:
    """Правило из одной строки. None — пустая строка или комментарий.

    ValueError — ошибка формата (неизвестный тип, пустой или некорректный паттерн).
    """
    text = line.split("#", 1)[0].strip()
    if not text:
        return None

    scope = _SCOPE_ALL
    if ":" in text:
        prefix, _, rest = text.partition(":")
        prefix = prefix.strip().lower()
        if prefix not in ANOMALY_SCOPES:
            raise ValueError(f"неизвестный тип «{prefix}» (допустимо: {', '.join(ANOMALY_SCOPES)})")
        scope = prefix
        text = rest.strip()
        if not text:
            raise ValueError("пустой паттерн после типа")

    try:
        # fnmatch.translate даёт полностью заякоренный regex — паттерн без
        # wildcard-ов совпадает только с точным именем, а не с подстрокой
        regex = re.compile(fnmatch.translate(text), re.IGNORECASE)
    except re.error as e:
        raise ValueError(f"некорректный паттерн «{text}»: {e}")
    return WhitelistRule(scope=scope, pattern=text, regex=regex)


def validate_rules(raw: str | None) -> list[str]:
    """Все ошибки формата с номерами строк; пустой список — текст корректен."""
    errors: list[str] = []
    for line_no, line in enumerate((raw or "").splitlines(), start=1):
        try:
            _parse_line(line)
        except ValueError as e:
            errors.append(f"строка {line_no}: {e}")
    return errors


class AnomalyWhitelist:
    """Скомпилированный набор правил.

    Некорректные строки молча пропускаются: формат проверяется при сохранении
    настроек, а рантайм обязан переживать любое содержимое БД.
    """

    def __init__(self, rules: list[WhitelistRule]):
        self._rules = rules

    @classmethod
    def parse(cls, raw: str | None) -> "AnomalyWhitelist":
        rules: list[WhitelistRule] = []
        for line in (raw or "").splitlines():
            try:
                rule = _parse_line(line)
            except ValueError:
                continue
            if rule is not None:
                rules.append(rule)
        return cls(rules)

    def matches(self, scope: str, username: str | None, user_id: int | None = None) -> bool:
        candidates = [c for c in (username, str(user_id) if user_id is not None else None) if c]
        if not candidates:
            return False
        for rule in self._rules:
            if rule.scope not in (_SCOPE_ALL, scope):
                continue
            if any(rule.regex.match(c) for c in candidates):
                return True
        return False
