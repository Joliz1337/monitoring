"""Где нода не отвечает на ping: нигде, везде или на выбранных папках и серверах.

Папка выбирается «живой»: сервер, перенесённый в неё позже, закрывает ping
сам, поэтому храним имена папок, а не их состав на момент выбора. Переименование
и удаление папки правят сохранённую настройку вслед за серверами.
"""

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PanelSettings, Server
from app.services.reserved_ports_sync import _version_tuple

logger = logging.getLogger(__name__)

PING_BLOCK_SETTING = "blocklist_ping_block"
MIN_NODE_VERSION_PING_BLOCK = "10.31.0"


class PingBlockMode(str, Enum):
    OFF = "off"
    ALL = "all"
    SELECTED = "selected"


@dataclass(frozen=True)
class PingBlockScope:
    mode: PingBlockMode = PingBlockMode.OFF
    folders: frozenset[str] = field(default_factory=frozenset)
    server_ids: frozenset[int] = field(default_factory=frozenset)

    def covers(self, server_id: int, folder: Optional[str]) -> bool:
        if self.mode == PingBlockMode.ALL:
            return True
        if self.mode == PingBlockMode.OFF:
            return False
        return server_id in self.server_ids or (folder is not None and folder in self.folders)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "folders": sorted(self.folders),
            "server_ids": sorted(self.server_ids),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PingBlockScope":
        return cls(
            mode=PingBlockMode(data.get("mode", PingBlockMode.OFF.value)),
            folders=frozenset(data.get("folders") or ()),
            server_ids=frozenset(data.get("server_ids") or ()),
        )


def node_supports_ping_block(node_version: Optional[str]) -> bool:
    if not node_version:
        return False
    return _version_tuple(node_version) >= _version_tuple(MIN_NODE_VERSION_PING_BLOCK)


async def _setting_row(db: AsyncSession) -> Optional[PanelSettings]:
    return (await db.execute(
        select(PanelSettings).where(PanelSettings.key == PING_BLOCK_SETTING)
    )).scalar_one_or_none()


async def load_scope(db: AsyncSession) -> PingBlockScope:
    row = await _setting_row(db)
    if not row or not row.value:
        return PingBlockScope()
    try:
        return PingBlockScope.from_dict(json.loads(row.value))
    except (ValueError, TypeError) as e:
        # Битая запись не должна ронять синк блок-листа — ping просто открыт
        logger.error(f"Invalid {PING_BLOCK_SETTING} value, treating as off: {e}")
        return PingBlockScope()


async def _store(db: AsyncSession, scope: PingBlockScope) -> None:
    value = json.dumps(scope.to_dict())
    row = await _setting_row(db)
    if row:
        row.value = value
    else:
        db.add(PanelSettings(key=PING_BLOCK_SETTING, value=value))
    await db.commit()


async def save_scope(db: AsyncSession, scope: PingBlockScope) -> PingBlockScope:
    """Сохранить, оставив только существующие папки и серверы: выбор относится
    к тому, что есть сейчас, а не к устаревшему списку в открытой вкладке."""
    rows = (await db.execute(select(Server.id, Server.folder))).all()
    known_ids = {server_id for server_id, _ in rows}
    known_folders = {folder for _, folder in rows if folder}
    cleaned = PingBlockScope(
        mode=scope.mode,
        folders=scope.folders & known_folders,
        server_ids=scope.server_ids & known_ids,
    )
    await _store(db, cleaned)
    return cleaned


async def describe(db: AsyncSession) -> dict:
    """Настройка и серверы из её области, которые не выполнят её до обновления агента."""
    scope = await load_scope(db)
    servers = (await db.execute(
        select(Server.id, Server.name, Server.folder, Server.node_version)
        .where(Server.is_active == True)  # noqa: E712
    )).all()
    outdated = sorted(
        name for server_id, name, folder, version in servers
        if scope.covers(server_id, folder) and not node_supports_ping_block(version)
    )
    return {
        "ping_block": scope.to_dict(),
        "min_node_version": MIN_NODE_VERSION_PING_BLOCK,
        "outdated_servers": outdated,
    }


async def rename_folder(db: AsyncSession, old_name: str, new_name: str) -> None:
    scope = await load_scope(db)
    if old_name not in scope.folders:
        return
    await _store(db, PingBlockScope(
        mode=scope.mode,
        folders=(scope.folders - {old_name}) | {new_name},
        server_ids=scope.server_ids,
    ))


async def forget_folder(db: AsyncSession, name: str) -> None:
    """Папки больше нет — иначе новая папка с тем же именем закрыла бы ping сама."""
    scope = await load_scope(db)
    if name not in scope.folders:
        return
    await _store(db, PingBlockScope(
        mode=scope.mode,
        folders=scope.folders - {name},
        server_ids=scope.server_ids,
    ))
