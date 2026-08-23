"""Матрица проверок: каждая конфигурация против каждого SNI.

Смена SNI без смены Host-заголовка транспорта — главный источник ложных
отказов: у ws/httpupgrade/xhttp/grpc сервер маршрутизирует запрос по Host и на
чужой ответит 404. Ячейка тогда покажет «не работает», хотя SNI не заблокирован
— поэтому Host по умолчанию едет вместе с SNI.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Iterable, Optional

from app.services.xray_test.errors import LimitExceededError
from app.services.xray_test.models import (
    HOST_BOUND_TRANSPORTS,
    ProxyEndpoint,
    TestCell,
)

MAX_SNI = 50
MAX_CELLS_PER_JOB = 200
MAX_ENDPOINTS = 500


def normalize_sni_list(raw: Iterable[str]) -> list[str]:
    """Строки оператора → список доменов без дублей и мусора."""
    seen: set[str] = set()
    result: list[str] = []
    for item in raw:
        name = (item or "").strip().strip(",").lower()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def build_matrix(
    endpoints: list[ProxyEndpoint],
    sni_list: Optional[list[str]] = None,
    *,
    sync_transport_host: bool = True,
    links: Optional[list[Optional[str]]] = None,
) -> list[TestCell]:
    if not endpoints:
        raise LimitExceededError("Нечего проверять: не разобрано ни одной конфигурации")
    if len(endpoints) > MAX_ENDPOINTS:
        raise LimitExceededError(
            f"Конфигураций больше допустимых {MAX_ENDPOINTS}: {len(endpoints)}"
        )

    names = normalize_sni_list(sni_list or [])
    if len(names) > MAX_SNI:
        raise LimitExceededError(f"SNI больше допустимых {MAX_SNI}: {len(names)}")

    total = len(endpoints) * max(1, len(names))
    if total > MAX_CELLS_PER_JOB:
        raise LimitExceededError(
            f"Проверок в задаче больше допустимых {MAX_CELLS_PER_JOB}: {total}. "
            f"Уменьшите список конфигураций или SNI"
        )

    cells: list[TestCell] = []
    for position, endpoint in enumerate(endpoints):
        link = links[position] if links and position < len(links) else None
        if not names:
            cells.append(TestCell(index=len(cells), endpoint=endpoint, sni_label=None, link=link))
            continue
        for name in names:
            cells.append(TestCell(
                index=len(cells),
                endpoint=apply_sni(endpoint, name, sync_transport_host=sync_transport_host),
                sni_label=name,
                link=link,
            ))
    return cells


def apply_sni(
    endpoint: ProxyEndpoint, sni: str, *, sync_transport_host: bool = True
) -> ProxyEndpoint:
    tls = replace(endpoint.tls, sni=sni)
    transport = endpoint.transport
    if sync_transport_host and transport.kind in HOST_BOUND_TRANSPORTS and transport.host:
        transport = replace(transport, host=sni)
    return replace(endpoint, tls=tls, transport=transport)
