"""Что не учитывается на странице «Потери» — настройка внизу страницы.

Исключённый сервер выпадает целиком: его проверки не попадают ни в таблицу, ни
в алерт, ни в подсказки, а адреса, которые стоят на нём, не показываются и не
алертятся. Исключённый адрес назначения — IP целиком или один бэкенд IP:порт —
не показывается и не алертится, сколько бы релеев его ни проверяло, а с галочкой
«только полные потери» молчит лишь до TOTAL_LOSS_PCT. Ноды сами проверять не
перестают — данные просто не учитываются. Оба списка живут в panel_settings и
держатся в памяти реестра (loss_registry).
"""

import json
from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.loss_overview import TargetParseError, format_endpoint, parse_endpoint
from app.services.loss_registry import ExcludedTarget, get_loss_registry

LOSS_EXCLUDED_KEY = "loss_excluded_server_ids"
LOSS_EXCLUDED_TARGETS_KEY = "loss_excluded_targets"


@dataclass(frozen=True)
class LossExclusions:
    server_ids: frozenset[int]
    targets: tuple[ExcludedTarget, ...]


def parse_ids(raw: Optional[str]) -> set[int]:
    return {int(part) for part in (raw or "").split(",") if part.strip().isdigit()}


def format_ids(ids: Iterable[int]) -> str:
    return ",".join(str(i) for i in sorted(set(ids)))


def unique_targets(targets: Iterable[ExcludedTarget]) -> tuple[ExcludedTarget, ...]:
    """Один адрес — одна запись; у повтора побеждает последняя."""
    return tuple({(t.ip, t.port): t for t in targets}.values())


def target_entry(target: ExcludedTarget) -> dict:
    return {"target": format_endpoint(target.ip, target.port), "total_only": target.total_only}


def parse_targets(raw: Optional[str]) -> tuple[ExcludedTarget, ...]:
    """Битая запись в базе пропускается, а не роняет загрузку остальных."""
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        return ()
    if not isinstance(items, list):
        return ()
    targets = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            ip, port = parse_endpoint(str(item.get("target", "")))
        except TargetParseError:
            continue
        targets.append(ExcludedTarget(ip, port, bool(item.get("total_only"))))
    return unique_targets(targets)


def format_targets(targets: Iterable[ExcludedTarget]) -> str:
    return json.dumps([target_entry(t) for t in targets])


def _publish(exclusions: LossExclusions) -> LossExclusions:
    registry = get_loss_registry()
    registry.set_excluded(exclusions.server_ids)
    registry.set_excluded_targets(exclusions.targets)
    return exclusions


async def load_loss_exclusions(db: AsyncSession) -> LossExclusions:
    from app.routers.settings import get_setting

    return _publish(LossExclusions(
        server_ids=frozenset(parse_ids(await get_setting(LOSS_EXCLUDED_KEY, db))),
        targets=parse_targets(await get_setting(LOSS_EXCLUDED_TARGETS_KEY, db)),
    ))


async def save_loss_exclusions(db: AsyncSession, server_ids: Iterable[int],
                               targets: Iterable[ExcludedTarget]) -> LossExclusions:
    from app.routers.settings import set_setting

    exclusions = LossExclusions(server_ids=frozenset(server_ids), targets=unique_targets(targets))
    await set_setting(LOSS_EXCLUDED_KEY, format_ids(exclusions.server_ids), db)
    await set_setting(LOSS_EXCLUDED_TARGETS_KEY, format_targets(exclusions.targets), db)
    return _publish(exclusions)
