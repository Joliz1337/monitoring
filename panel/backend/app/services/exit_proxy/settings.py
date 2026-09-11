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
# Стоковые проверки ходят в API, а не на сайты: claude.ai и chatgpt.com стоят за
# bot-защитой Cloudflare и хостинговым IP отвечают 403-челленджем независимо от
# страны, а API без ключа называют отказ по региону словами
# («Request not allowed», «unsupported_country…») либо отвечают 401 — значит, регион обслуживается
DEFAULT_CUSTOM_CHECKS = [
    {
        "id": "claude", "name": "Claude", "url": "https://api.anthropic.com/v1/models", "enabled": True,
        "block_status": [], "block_regex": "request not allowed|unsupported_country", "block_url_regex": "",
        "expect_status": None,
    },
    {
        "id": "chatgpt", "name": "ChatGPT", "url": "https://api.openai.com/compliance/cookie_requirements",
        "enabled": True, "block_status": [], "block_regex": "unsupported_country", "block_url_regex": "",
        "expect_status": None,
    },
]
# Прежний сток: сайты за Cloudflare давали ложный «блок» по 403, а reddit.com режет
# хостинговые диапазоны независимо от страны — все IP ноды выглядели заблокированными
LEGACY_STOCK_CHECKS = {
    "claude": {
        "url": "https://claude.ai/login", "block_status": [], "block_regex": "",
        "block_url_regex": "unavailable", "expect_status": None,
    },
    "chatgpt": {
        "url": "https://chatgpt.com/", "block_status": [403], "block_regex": "",
        "block_url_regex": "", "expect_status": None,
    },
    "reddit": {
        "url": "https://www.reddit.com/", "block_status": [], "block_regex": "blocked by network security",
        "block_url_regex": "", "expect_status": None,
    },
}
CHECK_RULE_FIELDS = ("url", "block_status", "block_regex", "block_url_regex", "expect_status")
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


def upgrade_stock_checks(checks: list[dict]) -> Optional[list[dict]]:
    """Стоковые проверки старого образца, не тронутые пользователем, → новые.

    Сравнивается само правило (URL, коды, шаблоны): правленую запись не трогаем.
    Стоковая проверка, которой в новом наборе нет (Reddit), выключается, а не
    удаляется — имя и тумблер пользователя сохраняются. None — менять нечего.
    """
    fresh_by_id = {check["id"]: check for check in DEFAULT_CUSTOM_CHECKS}
    upgraded: list[dict] = []
    changed = False
    for check in checks:
        legacy = LEGACY_STOCK_CHECKS.get(check.get("id"))
        if legacy is None or any(check.get(field) != legacy[field] for field in CHECK_RULE_FIELDS):
            upgraded.append(check)
            continue
        changed = True
        fresh = fresh_by_id.get(check["id"])
        if fresh is None:
            upgraded.append({**check, "enabled": False})
            continue
        upgraded.append({**fresh, "name": check.get("name") or fresh["name"], "enabled": bool(check.get("enabled", True))})
    return upgraded if changed else None


async def upgrade_stored_stock_checks(db: AsyncSession) -> bool:
    """Одноразовая починка сохранённого набора при старте панели; нода получит новый конфиг по хэшу."""
    row = (await db.execute(select(ExitProxySettings).limit(1))).scalar_one_or_none()
    if row is None:
        return False
    upgraded = upgrade_stock_checks(load_json(row.custom_checks, []))
    if upgraded is None:
        return False
    row.custom_checks = json.dumps(upgraded, ensure_ascii=False)
    await db.commit()
    return True


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
