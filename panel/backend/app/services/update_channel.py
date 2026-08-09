"""Канал обновлений панели и нод: стабильная ветка (main) или dev.

Значение хранится в PanelSettings (ключ update_branch) и кэшируется в памяти:
GitHub-URL нужны и в фоновых циклах (antiddos_manager, deploy), где сессии БД
под рукой нет. Кэш загружается на старте и обновляется при смене настройки.
"""
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PanelSettings

logger = logging.getLogger(__name__)

GITHUB_REPO = "Joliz1337/monitoring"
STABLE_BRANCH = "main"
DEV_BRANCH = "dev"
ALLOWED_BRANCHES = (STABLE_BRANCH, DEV_BRANCH)
UPDATE_BRANCH_SETTING_KEY = "update_branch"

_current_branch = STABLE_BRANCH


class InvalidBranchError(ValueError):
    """Недопустимое имя ветки канала обновлений."""


def current_branch() -> str:
    return _current_branch


def set_current_branch(branch: str) -> None:
    global _current_branch
    if branch not in ALLOWED_BRANCHES:
        raise InvalidBranchError(branch)
    if branch != _current_branch:
        logger.info(f"Update channel switched: {_current_branch} -> {branch}")
    _current_branch = branch


def github_raw_base() -> str:
    return f"https://raw.githubusercontent.com/{GITHUB_REPO}/{_current_branch}"


def github_configs_base() -> str:
    return f"{github_raw_base()}/configs"


def installer_url() -> str:
    return f"{github_raw_base()}/install.sh"


async def load_branch_from_db(db: AsyncSession) -> str:
    """Прочитать сохранённый канал из БД в кэш (вызывается на старте панели)."""
    result = await db.execute(
        select(PanelSettings).where(PanelSettings.key == UPDATE_BRANCH_SETTING_KEY)
    )
    setting = result.scalar_one_or_none()
    if setting and setting.value in ALLOWED_BRANCHES:
        set_current_branch(setting.value)
    return _current_branch
