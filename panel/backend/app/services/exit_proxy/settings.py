"""Настройки exit-прокси: singleton в БД, JSON-поля и снимок для фоновых задач."""

import json
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExitProxySettings

DEFAULT_PORT = 7590
DEFAULT_INTERVAL_MINUTES = 30
DEFAULT_ALERT_COOLDOWN_SECONDS = 1800
DEFAULT_BLOCKED_COUNTRIES = ["RU"]
DEFAULT_BUILTIN_CHECKS = {"google_country": True, "google_captcha": True, "gemini": True}
BUILTIN_CHECK_KEYS = tuple(DEFAULT_BUILTIN_CHECKS)
DEFAULT_CUSTOM_CHECKS = [
    {
        "id": "claude", "name": "Claude", "url": "https://claude.ai/login", "enabled": True,
        "block_status": [], "block_regex": "", "block_url_regex": "unavailable", "expect_status": None,
    },
    {
        "id": "chatgpt", "name": "ChatGPT", "url": "https://chatgpt.com/", "enabled": True,
        "block_status": [403], "block_regex": "", "block_url_regex": "", "expect_status": None,
    },
    {
        "id": "reddit", "name": "Reddit", "url": "https://www.reddit.com/", "enabled": True,
        "block_status": [], "block_regex": "blocked by network security", "block_url_regex": "", "expect_status": None,
    },
]
# Порты агента, проверок Xray-теста, SSH-релея Remnawave, WARP и mTLS-nginx — под socks не годятся
RESERVED_SERVICE_PORTS = frozenset({7500, 2222, 9091, 9100} | set(range(7501, 7565)))


def load_json(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class SettingsSnapshot:
    """Настройки, отвязанные от сессии БД: цикл работает с ними после закрытия сессии."""

    enabled: bool
    port: int
    check_interval_minutes: int
    blocked_countries: list[str]
    builtin_checks: dict[str, bool]
    custom_checks: list[dict]
    telegram_enabled: bool
    alert_cooldown_seconds: int

    @classmethod
    def from_row(cls, row: ExitProxySettings) -> "SettingsSnapshot":
        builtin = dict(DEFAULT_BUILTIN_CHECKS)
        builtin.update({key: bool(value) for key, value in load_json(row.builtin_checks, {}).items() if key in builtin})
        return cls(
            enabled=bool(row.enabled),
            port=row.port or DEFAULT_PORT,
            check_interval_minutes=row.check_interval_minutes or DEFAULT_INTERVAL_MINUTES,
            blocked_countries=list(load_json(row.blocked_countries, DEFAULT_BLOCKED_COUNTRIES)),
            builtin_checks=builtin,
            custom_checks=list(load_json(row.custom_checks, DEFAULT_CUSTOM_CHECKS)),
            telegram_enabled=bool(row.telegram_enabled),
            alert_cooldown_seconds=row.alert_cooldown_seconds or DEFAULT_ALERT_COOLDOWN_SECONDS,
        )


async def get_or_create_settings(db: AsyncSession) -> ExitProxySettings:
    row = (await db.execute(select(ExitProxySettings).limit(1))).scalar_one_or_none()
    if row is not None:
        return row
    row = ExitProxySettings(
        enabled=False,
        port=DEFAULT_PORT,
        check_interval_minutes=DEFAULT_INTERVAL_MINUTES,
        blocked_countries=json.dumps(DEFAULT_BLOCKED_COUNTRIES),
        builtin_checks=json.dumps(DEFAULT_BUILTIN_CHECKS),
        custom_checks=json.dumps(DEFAULT_CUSTOM_CHECKS),
        telegram_enabled=True,
        alert_cooldown_seconds=DEFAULT_ALERT_COOLDOWN_SECONDS,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def service_ports(db: AsyncSession) -> list[str]:
    """Порт socks для резервации от эфемерной выдачи — только пока фича включена."""
    row = (await db.execute(select(ExitProxySettings).limit(1))).scalar_one_or_none()
    if row is None or not row.enabled:
        return []
    return [str(row.port or DEFAULT_PORT)]
