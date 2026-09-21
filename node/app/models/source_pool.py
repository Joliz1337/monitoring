"""Схемы пула исходящих адресов: конфиг от панели и раскладка меток по адресам."""

import ipaddress
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


class SourcePoolConfig(BaseModel):
    enabled: bool = False
    excluded: list[str] = Field(default_factory=list, max_length=MAX_EXCLUDED)

    @field_validator("excluded")
    @classmethod
    def _valid_ipv4(cls, value: list[str]) -> list[str]:
        cleaned = set()
        for item in value:
            try:
                cleaned.add(str(ipaddress.IPv4Address(str(item).strip())))
            except ValueError:
                raise ValueError(f"not an IPv4 address: {item}")
        return sorted(cleaned)


class MarkBinding(BaseModel):
    mark: int
    address: str
    table: int


class SourcePoolState(BaseModel):
    enabled: bool = False
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
    in_sync: bool = False
    last_error: Optional[str] = None
    # Причина, по которой пул не может быть включён (сейчас — включённый exit-прокси)
    conflict: Optional[str] = None
