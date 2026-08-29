"""Восстановление выбора версий ядер при старте панели.

Выбор живёт в настройках панели, а нужен фоновым проверкам, где сессии БД под
рукой нет, — поэтому читается один раз и кэшируется в памяти модуля.
"""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.xray_test import core_manager, storage

logger = logging.getLogger(__name__)


async def load_xray_test_versions(db: AsyncSession) -> None:
    try:
        values = await storage.load_core_versions(db, list(core_manager.SETTING_KEYS.values()))
    except Exception as exc:  # noqa: BLE001 — панель обязана подняться и без этой настройки
        logger.warning("xray-test: core versions not loaded: %s", exc)
        return

    core_manager.load_selected(values)
    if values:
        logger.info(
            "xray-test cores: %s",
            ", ".join(f"{core.value}={core_manager.selected_version(core)}"
                      for core in core_manager.SETTING_KEYS),
        )
