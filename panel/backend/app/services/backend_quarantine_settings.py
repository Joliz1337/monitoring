"""Настройки автоотключения адресов — блок «Автоотключение» на странице «Потери».

Лежат одной JSON-строкой в panel_settings. Разбор терпимый: запись от другой
версии панели, битая или с выходом за границы не роняет запуск и не включает
автоотключение с порогами, которых никто не выставлял. Строгая проверка ввода
— на границе, в роутере.
"""

import json
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.backend_quarantine import Exemptions, Mode, QuarantinePolicy
from app.services.loss_overview import TargetParseError, format_endpoint, parse_endpoint

QUARANTINE_SETTINGS_KEY = "loss_quarantine"

# Меньше половины теряет и рабочий сервер: по таким потерям его от заблокированного не отличить
LOSS_THRESHOLD_BOUNDS = (50.0, 90.0)
SUSTAINED_SECONDS_BOUNDS = (60, 1800)
DOWNS_THRESHOLD_BOUNDS = (2, 20)
CALM_SECONDS_BOUNDS = (120, 3600)
CHRONIC_STRIKES_BOUNDS = (2, 20)
MAX_SHARE_PCT_BOUNDS = (10, 90)

# ip без порта — все порты адреса
ExemptTarget = tuple[str, Optional[int]]


@dataclass(frozen=True)
class QuarantineSettings:
    mode: Mode = Mode.OFF
    policy: QuarantinePolicy = QuarantinePolicy()
    notify: bool = True
    exempt_server_ids: frozenset[int] = frozenset()
    exempt_targets: tuple[ExemptTarget, ...] = ()

    def exemptions(self) -> Exemptions:
        return Exemptions(
            server_ids=self.exempt_server_ids,
            ips=frozenset(ip for ip, port in self.exempt_targets if port is None),
            endpoints=frozenset((ip, port) for ip, port in self.exempt_targets if port is not None),
        )

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "loss_threshold": self.policy.loss_threshold,
            "sustained_seconds": int(self.policy.sustained_sec),
            "downs_threshold": self.policy.downs_threshold,
            "calm_seconds": int(self.policy.calm_sec),
            "chronic_strikes": self.policy.chronic_strikes,
            "max_share_pct": self.policy.max_share_pct,
            "drop_sessions": self.policy.drop_sessions,
            "notify": self.notify,
            "exempt_server_ids": sorted(self.exempt_server_ids),
            "exempt_targets": [format_endpoint(ip, port) for ip, port in self.exempt_targets],
        }


def _number(value: Any, bounds: tuple[float, float], default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    low, high = bounds
    return min(max(value, low), high)


def _flag(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _mode(value: Any) -> Mode:
    try:
        return Mode(value)
    except ValueError:
        return Mode.OFF


def _server_ids(value: Any) -> frozenset[int]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, int) and not isinstance(item, bool))


def _targets(value: Any) -> tuple[ExemptTarget, ...]:
    if not isinstance(value, list):
        return ()
    targets: dict[ExemptTarget, None] = {}
    for item in value:
        if not isinstance(item, str):
            continue
        try:
            targets[parse_endpoint(item)] = None
        except TargetParseError:
            continue
    return tuple(targets)


def settings_from_dict(data: dict) -> QuarantineSettings:
    defaults = QuarantinePolicy()
    return QuarantineSettings(
        mode=_mode(data.get("mode")),
        policy=QuarantinePolicy(
            loss_threshold=float(_number(data.get("loss_threshold"), LOSS_THRESHOLD_BOUNDS, defaults.loss_threshold)),
            sustained_sec=int(_number(data.get("sustained_seconds"), SUSTAINED_SECONDS_BOUNDS, defaults.sustained_sec)),
            downs_threshold=int(_number(data.get("downs_threshold"), DOWNS_THRESHOLD_BOUNDS, defaults.downs_threshold)),
            calm_sec=int(_number(data.get("calm_seconds"), CALM_SECONDS_BOUNDS, defaults.calm_sec)),
            chronic_strikes=int(_number(data.get("chronic_strikes"), CHRONIC_STRIKES_BOUNDS, defaults.chronic_strikes)),
            max_share_pct=int(_number(data.get("max_share_pct"), MAX_SHARE_PCT_BOUNDS, defaults.max_share_pct)),
            drop_sessions=_flag(data.get("drop_sessions"), defaults.drop_sessions),
        ),
        notify=_flag(data.get("notify"), True),
        exempt_server_ids=_server_ids(data.get("exempt_server_ids")),
        exempt_targets=_targets(data.get("exempt_targets")),
    )


def parse_settings(raw: Optional[str]) -> QuarantineSettings:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return QuarantineSettings()
    return settings_from_dict(data) if isinstance(data, dict) else QuarantineSettings()


async def load_quarantine_settings(db: AsyncSession) -> QuarantineSettings:
    from app.routers.settings import get_setting

    return parse_settings(await get_setting(QUARANTINE_SETTINGS_KEY, db))


async def save_quarantine_settings(db: AsyncSession, settings: QuarantineSettings) -> None:
    from app.routers.settings import set_setting

    await set_setting(QUARANTINE_SETTINGS_KEY, json.dumps(settings.to_dict()), db)
