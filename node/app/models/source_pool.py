"""Схемы пула исходящих адресов: конфиг от панели и раскладка меток по адресам."""

import ipaddress
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# Число меток фиксировано и одинаково для всех нод: конфиг Xray один на весь парк,
# а сколько у ноды адресов — знает только она сама. Метки раскладываются по
# адресам по кругу, поэтому парк с разным числом адресов делит трафик ровно.
MARK_COUNT = 30
MARK_BASE = 101
TABLE_BASE = 101
RULE_PRIORITY_BASE = 101
MAX_EXCLUDED = 64


def mark_range() -> range:
    return range(MARK_BASE, MARK_BASE + MARK_COUNT)


class SourcePoolMode(str, Enum):
    # Метки по кругу по всем участвующим адресам
    AUTO = "auto"
    # Только назначенные оператором метки; остальные идут по основной таблице
    MANUAL = "manual"


def _ipv4(value) -> str:
    try:
        return str(ipaddress.IPv4Address(str(value).strip()))
    except ValueError:
        raise ValueError(f"not an IPv4 address: {value}")


class SourcePoolConfig(BaseModel):
    enabled: bool = False
    excluded: list[str] = Field(default_factory=list, max_length=MAX_EXCLUDED)
    mode: SourcePoolMode = SourcePoolMode.AUTO
    # Ручная раскладка: метка → адрес. Действует только в режиме manual
    assignments: dict[int, str] = Field(default_factory=dict)

    @field_validator("excluded")
    @classmethod
    def _valid_ipv4(cls, value: list[str]) -> list[str]:
        return sorted({_ipv4(item) for item in value})

    @field_validator("assignments")
    @classmethod
    def _valid_assignments(cls, value: dict[int, str]) -> dict[int, str]:
        marks = mark_range()
        for mark in value:
            if mark not in marks:
                raise ValueError(f"mark {mark} is outside {marks.start}-{marks.stop - 1}")
        return {mark: _ipv4(address) for mark, address in sorted(value.items())}


class MarkBinding(BaseModel):
    mark: int
    address: str
    table: int


class SourcePoolState(BaseModel):
    enabled: bool = False
    # Панель по этому полю видит, что агент понял ручную раскладку, а не тихо остался в авто
    mode: SourcePoolMode = SourcePoolMode.AUTO
    supported: bool = True
    reason: Optional[str] = None
    interface: Optional[str] = None
    gateway: Optional[str] = None
    # Всё, что нашлось на интерфейсе, — панель показывает это списком с галочками
    addresses: list[str] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)
    # Что реально участвует в раскладке после исключений
    active_addresses: list[str] = Field(default_factory=list)
    mark_base: int = MARK_BASE
    mark_count: int = MARK_COUNT
    bindings: list[MarkBinding] = Field(default_factory=list)
    missing_marks: list[int] = Field(default_factory=list)
    # Ручные метки, чей адрес пропал с интерфейса: правило не ставится, трафик идёт с основного IP
    unavailable_marks: list[int] = Field(default_factory=list)
    in_sync: bool = False
    last_error: Optional[str] = None
    # Причина, по которой пул не может быть включён (сейчас — включённый exit-прокси)
    conflict: Optional[str] = None
