"""Pydantic-схемы DNAT-маршрутизации (проброс портов через iptables nat)."""

import ipaddress
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

Protocol = Literal["tcp", "udp", "both"]
# per_server — панель уже выбрала этой ноде один адрес; остальные режимы —
# нода сама раскидывает новые соединения по всем адресам списка
Distribution = Literal["per_server", "random", "round_robin", "client_hash"]

RULE_NAME_PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"
MAX_TARGETS = 32

# Дефолтный порт mTLS-nginx ноды: DNAT на порт API отрезал бы панель от
# сервера. Фактический порт — settings.node_api_port, сюда идёт как параметр
DEFAULT_NODE_API_PORT = 9100


class DnatRule(BaseModel):
    name: str = Field(..., pattern=RULE_NAME_PATTERN)
    protocol: Protocol = "tcp"
    listen_port: int = Field(..., ge=1, le=65535)
    # Конец диапазона; None — одиночный порт
    listen_port_end: Optional[int] = Field(None, ge=1, le=65535)
    # Один IPv4 или несколько через запятую (хранится как «a,b,c»)
    target_ip: str
    distribution: Distribution = "per_server"
    # 0 — порт назначения равен входящему (для диапазона порты сохраняются)
    target_port: int = Field(0, ge=0, le=65535)
    # Подмена адреса источника (MASQUERADE): без неё цель должна маршрутизировать
    # ответы клиенту через эту ноду
    masquerade: bool = True
    # Маскировка транзита: TTL=64 и MSS clamp на потоках правила — пакеты не выдают
    # чужие стеки за IP ноды (см. dnat_manager)
    mask_ttl: bool = False
    enabled: bool = True
    comment: Optional[str] = Field("", max_length=200)

    @field_validator("target_ip")
    @classmethod
    def _ipv4_list(cls, value: str) -> str:
        targets: list[str] = []
        for part in (value or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                address = ipaddress.IPv4Address(part)
            except ValueError:
                raise ValueError(f"target_ip '{part}' must be an IPv4 address")
            if address.is_unspecified or address.is_multicast:
                raise ValueError(f"target_ip '{part}' must be a unicast IPv4 address")
            if str(address) not in targets:
                targets.append(str(address))
        if not targets:
            raise ValueError("target_ip must contain at least one IPv4 address")
        if len(targets) > MAX_TARGETS:
            raise ValueError(f"target_ip must contain at most {MAX_TARGETS} addresses")
        return ",".join(targets)

    @field_validator("comment", mode="before")
    @classmethod
    def _none_comment(cls, value):
        return "" if value is None else value

    @model_validator(mode="after")
    def _check_range(self) -> "DnatRule":
        if self.listen_port_end is not None:
            if self.listen_port_end == self.listen_port:
                self.listen_port_end = None
            elif self.listen_port_end < self.listen_port:
                raise ValueError("listen_port_end must be greater than listen_port")
        return self

    @property
    def port_range(self) -> tuple[int, int]:
        return self.listen_port, self.listen_port_end or self.listen_port

    def covers_port(self, port: int) -> bool:
        low, high = self.port_range
        return low <= port <= high

    def protocols(self) -> tuple[str, ...]:
        return ("tcp", "udp") if self.protocol == "both" else (self.protocol,)

    def targets(self) -> list[str]:
        return self.target_ip.split(",")


class DnatApplyRequest(BaseModel):
    rules: list[DnatRule]


class DnatApplyResponse(BaseModel):
    success: bool
    message: str
    rules_hash: Optional[str] = None
    error_log: Optional[str] = None


class DnatTargetCounters(BaseModel):
    ip: str
    present: bool
    conns: int = 0
    packets_in: int = 0
    bytes_in: int = 0
    packets_out: int = 0
    bytes_out: int = 0


class DnatRuleCounters(BaseModel):
    """Счётчики ядра по одному правилу: nat-правило видит только первый пакет
    соединения (conns), байты — из ACCEPT-правил FORWARD. Суммы по целям плюс
    разбивка по каждому адресу назначения."""
    name: str
    present: bool
    conns: int = 0
    packets_in: int = 0
    bytes_in: int = 0
    packets_out: int = 0
    bytes_out: int = 0
    targets: list[DnatTargetCounters] = []


class DnatStateResponse(BaseModel):
    available: bool
    ip_forward: bool
    rules: list[DnatRule]
    rules_hash: str
    healthy: bool
    missing: list[str] = []
    counters: list[DnatRuleCounters] = []
    applied_at: Optional[str] = None
    message: Optional[str] = None


class DnatActionResponse(BaseModel):
    success: bool
    message: str
