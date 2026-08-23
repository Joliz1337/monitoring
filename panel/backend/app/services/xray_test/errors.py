"""Доменные исключения проверки прокси-конфигураций.

Все несут машинный код: причина отказа доезжает до фронта кодом, а текст
подставляет i18n — иначе английская локаль показывала бы русские строки.
"""
from __future__ import annotations


class XrayTestError(Exception):
    """Базовое исключение раздела."""

    code = "UNKNOWN"


class LinkParseError(XrayTestError):
    """Ссылку не удалось разобрать."""

    code = "LINK_PARSE_FAILED"

    def __init__(self, reason: str, scheme: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.scheme = scheme


class UnsupportedProtocolError(XrayTestError):
    """Протокол ссылки не поддерживается ни одним из ядер."""

    code = "UNSUPPORTED_PROTOCOL"

    def __init__(self, scheme: str) -> None:
        super().__init__(f"Протокол не поддерживается: {scheme}")
        self.scheme = scheme


class UnsupportedConfigError(XrayTestError):
    """Комбинация протокола и транспорта не реализуема ни одним ядром."""

    code = "UNSUPPORTED_CONFIG"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SubscriptionFetchError(XrayTestError):
    """Подписку не удалось загрузить."""

    code = "SUBSCRIPTION_FETCH_FAILED"


class SubscriptionTooLargeError(SubscriptionFetchError):
    """Тело подписки превысило допустимый размер."""

    code = "SUBSCRIPTION_TOO_LARGE"


class UnsafeTargetError(SubscriptionFetchError):
    """Адрес подписки указывает во внутреннюю сеть — запрос не выполняется."""

    code = "UNSAFE_TARGET"


class UnknownSubscriptionFormatError(SubscriptionFetchError):
    """Формат ответа подписки не распознан."""

    code = "UNKNOWN_SUBSCRIPTION_FORMAT"


class CoreDownloadError(XrayTestError):
    """Не удалось получить бинарник ядра."""

    code = "CORE_DOWNLOAD_FAILED"


class LimitExceededError(XrayTestError):
    """Превышен лимит на объём задачи."""

    code = "LIMIT_EXCEEDED"
