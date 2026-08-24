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
MAX_ENDPOINTS = 500
MAX_LOCATIONS = 20


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
    sync_transport_host: bool = False,
    include_original_sni: bool = True,
    links: Optional[list[Optional[str]]] = None,
    locations: Optional[list[tuple[str, str]]] = None,
) -> list[TestCell]:
    """Произведение «конфигурация × SNI × место запуска».

    Локации — пары (код, отображаемое имя). Пустой список означает прогон с
    самой панели: один и тот же ключ из разных точек ведёт себя по-разному,
    поэтому каждая точка даёт свою ячейку, а не усредняется с остальными.

    `include_original_sni` добавляет к списку оператора родное имя из ключа —
    первой проверкой и без пометки SNI. Без него подстановку не с чем сравнить:
    все домены отвалились — и непонятно, режут их или лёг сам сервер. Имя,
    которое оператор перечислил сам, второй раз не проверяется.

    Размер матрицы не ограничен: ячейки исполняются очередью с постоянным
    числом рабочих, поэтому большой прогон занимает время, а не память.
    """
    if not endpoints:
        raise LimitExceededError("Нечего проверять: не разобрано ни одной конфигурации")
    if len(endpoints) > MAX_ENDPOINTS:
        raise LimitExceededError(
            f"Конфигураций больше допустимых {MAX_ENDPOINTS}: {len(endpoints)}"
        )

    names = normalize_sni_list(sni_list or [])
    if len(names) > MAX_SNI:
        raise LimitExceededError(f"SNI больше допустимых {MAX_SNI}: {len(names)}")

    places = locations or [("panel", "")]
    if len(places) > MAX_LOCATIONS:
        raise LimitExceededError(f"Мест запуска больше допустимых {MAX_LOCATIONS}: {len(places)}")

    cells: list[TestCell] = []
    for position, endpoint in enumerate(endpoints):
        link = links[position] if links and position < len(links) else None
        if names:
            variants = [
                (apply_sni(endpoint, name, sync_transport_host=sync_transport_host), name)
                for name in names
            ]
            if include_original_sni and endpoint.effective_sni.lower() not in names:
                variants.insert(0, (endpoint, None))
        else:
            variants = [(endpoint, None)]
        for variant, sni_label in variants:
            for code, title in places:
                cells.append(TestCell(
                    index=len(cells),
                    endpoint=variant,
                    sni_label=sni_label,
                    link=link,
                    location=code,
                    location_name=title,
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
