"""Обновление нод после обновления самой панели («Обновить всё»).

Панель обновляется первой: доставку образа и слежку за итогом ведёт её код, и
нодам, которые не смогли обновиться сами, должен помогать уже новый. Но, запустив
своё обновление, бэкенд уходит в перезапуск — рассылать команды нодам некому.
Поэтому список нод пишется в базу до обновления, а рассылку делает бэкенд,
поднявшийся после него. Сорвалось обновление, не дойдя до перезапуска, — рассылку
делает тот же процесс: ноды не должны ждать панель, которая не перезапустится.

Список убывает по мере рассылки и переживает перезапуск посреди неё: оставшиеся
ноды получат команду при следующем запуске бэкенда. Пачка, на которой бэкенд
оборвался, уйдёт повторно — нода на повтор отвечает «обновление уже идёт».
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Optional

from sqlalchemy import select

from app.database import async_session
from app.models import PanelSettings

logger = logging.getLogger(__name__)

SETTING_KEY = "pending_node_rollout"
# Столько нод получают команду одновременно; после каждой пачки остаток пишется в базу
ROLLOUT_BATCH = 20
MAX_ROLLOUT_SERVERS = 10000
# Бэкенд только что поднялся: коллектор и клиенты нод ещё прогреваются
STARTUP_DELAY_SEC = 15

# (id сервера, ветка) → запустить обновление ноды; отказ ноды — исключением
LaunchNodeUpdate = Callable[[int, str], Awaitable[None]]


@dataclass(frozen=True)
class PendingRollout:
    target_ref: str
    server_ids: tuple[int, ...]


_task: Optional[asyncio.Task] = None
_rolling_out = False


def _parse(value: Optional[str]) -> Optional[PendingRollout]:
    if not value:
        return None
    try:
        raw = json.loads(value)
        return PendingRollout(str(raw["target_ref"]), tuple(int(server_id) for server_id in raw["server_ids"]))
    except (ValueError, TypeError, KeyError) as e:
        logger.error(f"Invalid {SETTING_KEY} value, dropped: {e}")
        return None


async def _read_pending() -> Optional[PendingRollout]:
    async with async_session() as db:
        value = (await db.execute(
            select(PanelSettings.value).where(PanelSettings.key == SETTING_KEY)
        )).scalar_one_or_none()
    return _parse(value)


async def _write_pending(rollout: Optional[PendingRollout]) -> None:
    async with async_session() as db:
        row = (await db.execute(
            select(PanelSettings).where(PanelSettings.key == SETTING_KEY)
        )).scalar_one_or_none()
        if rollout is None:
            if row is not None:
                await db.delete(row)
        else:
            value = json.dumps({"target_ref": rollout.target_ref, "server_ids": list(rollout.server_ids)})
            if row is not None:
                row.value = value
            else:
                db.add(PanelSettings(key=SETTING_KEY, value=value))
        await db.commit()


async def schedule(server_ids: Iterable[int], target_ref: str) -> int:
    """Запомнить ноды, которые обновятся после панели. Возвращает, сколько их."""
    unique_ids = tuple(dict.fromkeys(server_ids))
    # Пустой список тоже записывается: он стирает остаток прошлой рассылки
    await _write_pending(PendingRollout(target_ref, unique_ids) if unique_ids else None)
    return len(unique_ids)


async def pending_count() -> int:
    rollout = await _read_pending()
    return len(rollout.server_ids) if rollout else 0


async def _launch_one(launch: LaunchNodeUpdate, server_id: int, target_ref: str) -> None:
    try:
        await launch(server_id, target_ref)
    except Exception as e:  # noqa: BLE001 — одна нода не должна срывать рассылку остальным
        reason = getattr(e, "detail", None) or str(e) or type(e).__name__
        logger.warning(f"Node rollout: server {server_id} did not start updating: {reason}")


async def run_pending(launch: LaunchNodeUpdate) -> None:
    """Разослать обновление нодам из отложенного списка. Пока рассылка идёт,
    повторный вызов ничего не делает."""
    global _rolling_out
    if _rolling_out:
        return
    _rolling_out = True
    try:
        rollout = await _read_pending()
        if rollout is None:
            return
        logger.info(f"Node rollout after panel update: {len(rollout.server_ids)} node(s), target {rollout.target_ref}")
        remaining = rollout.server_ids
        while remaining:
            batch, remaining = remaining[:ROLLOUT_BATCH], remaining[ROLLOUT_BATCH:]
            await asyncio.gather(*(_launch_one(launch, server_id, rollout.target_ref) for server_id in batch))
            await _write_pending(PendingRollout(rollout.target_ref, remaining) if remaining else None)
        logger.info("Node rollout after panel update: all nodes were asked to update")
    finally:
        _rolling_out = False


async def _run_after(delay: float, launch: LaunchNodeUpdate) -> None:
    await asyncio.sleep(delay)
    try:
        await run_pending(launch)
    except Exception as e:  # noqa: BLE001 — верхняя граница фоновой задачи
        logger.error(f"Node rollout failed, the rest will be retried on the next backend start: {e}")


def start(launch: LaunchNodeUpdate, delay: float = STARTUP_DELAY_SEC) -> None:
    """Запустить рассылку фоновой задачей, если есть отложенный список."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_run_after(delay, launch))


async def stop() -> None:
    if _task is None or _task.done():
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
