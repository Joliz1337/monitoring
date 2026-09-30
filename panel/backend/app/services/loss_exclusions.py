"""Серверы вне учёта потерь — настройка внизу страницы «Потери».

Исключённый сервер выпадает целиком: его проверки не попадают ни в таблицу, ни
в алерт, ни в подсказки, а адреса, которые стоят на нём, не показываются и не
алертятся. Нода сама проверять не перестаёт — данные просто не учитываются.
Список живёт в panel_settings и держится в памяти реестра (loss_registry).
"""

from typing import Iterable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.loss_registry import get_loss_registry

LOSS_EXCLUDED_KEY = "loss_excluded_server_ids"


def parse_ids(raw: Optional[str]) -> set[int]:
    return {int(part) for part in (raw or "").split(",") if part.strip().isdigit()}


def format_ids(ids: Iterable[int]) -> str:
    return ",".join(str(i) for i in sorted(set(ids)))


async def load_loss_exclusions(db: AsyncSession) -> set[int]:
    from app.routers.settings import get_setting

    ids = parse_ids(await get_setting(LOSS_EXCLUDED_KEY, db))
    get_loss_registry().set_excluded(ids)
    return ids


async def save_loss_exclusions(db: AsyncSession, ids: Iterable[int]) -> set[int]:
    from app.routers.settings import set_setting

    excluded = set(ids)
    await set_setting(LOSS_EXCLUDED_KEY, format_ids(excluded), db)
    get_loss_registry().set_excluded(excluded)
    return excluded
