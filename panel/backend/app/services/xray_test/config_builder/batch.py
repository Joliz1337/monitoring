"""Одна проверка внутри пачки: свой порт, свой outbound, свои теги."""
from __future__ import annotations

from dataclasses import dataclass

from app.services.xray_test.models import ProxyEndpoint

INBOUND_TAG = "mon-test-in"
OUTBOUND_TAG = "mon-test-out"


@dataclass(frozen=True)
class BatchEntry:
    """`slot` — номер проверки в пачке, он же попадает в теги и в лог ядра.

    Пустой slot оставляет теги без номера: так выглядит конфиг одиночного
    прогона, к которому откатываемся, когда пачка не поднялась.
    """

    slot: str
    endpoint: ProxyEndpoint
    socks_port: int

    @property
    def inbound_tag(self) -> str:
        return f"{INBOUND_TAG}-{self.slot}" if self.slot else INBOUND_TAG

    @property
    def outbound_tag(self) -> str:
        return f"{OUTBOUND_TAG}-{self.slot}" if self.slot else OUTBOUND_TAG
