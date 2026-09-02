"""Чистые функции: конфиг для ноды, его хэш и кусок конфига Remnawave для пользователя."""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional

from app.models import ExitProxyNode
from app.services.exit_proxy.settings import SettingsSnapshot, load_json

CHECK_TIMEOUT_SEC = 15
REMNAWAVE_OUTBOUND_TAG = "exit-proxy"
# Весь Google одним путём: половинчатая маршрутизация даёт «IP A ≠ IP B» в одной сессии
GOOGLE_DOMAINS = [
    "geosite:google",
    "geosite:google-gemini",
    "domain:googleapis.com",
    "domain:gstatic.com",
    "domain:googleusercontent.com",
]


@dataclass(frozen=True)
class NodePrefs:
    enabled: bool = True
    select_mode: str = "auto"
    pinned_candidate: Optional[str] = None
    candidates_order: list[str] = field(default_factory=list)
    candidates_disabled: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: ExitProxyNode) -> "NodePrefs":
        return cls(
            enabled=bool(row.enabled),
            select_mode=row.select_mode or "auto",
            pinned_candidate=row.pinned_candidate,
            candidates_order=list(load_json(row.candidates_order, [])),
            candidates_disabled=list(load_json(row.candidates_disabled, [])),
        )


def build_node_config(settings: SettingsSnapshot, prefs: NodePrefs) -> dict:
    """Полный конфиг агента (схема ExitProxyConfig ноды): глобальные настройки + правила ноды."""
    return {
        "enabled": prefs.enabled,
        "port": settings.port,
        "interval_minutes": settings.check_interval_minutes,
        "blocked_countries": list(settings.blocked_countries),
        "builtin_checks": dict(settings.builtin_checks),
        "custom_checks": [dict(check) for check in settings.custom_checks],
        "candidates_order": list(prefs.candidates_order),
        "candidates_disabled": list(prefs.candidates_disabled),
        "select_mode": prefs.select_mode,
        "pinned_candidate": prefs.pinned_candidate,
        "check_timeout": CHECK_TIMEOUT_SEC,
    }


def config_hash(config: dict) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def remnawave_outbound(port: int) -> dict:
    return {
        "tag": REMNAWAVE_OUTBOUND_TAG,
        "protocol": "socks",
        "settings": {"servers": [{"address": "127.0.0.1", "port": port}]},
    }


def remnawave_rules() -> list[dict]:
    return [
        {"type": "field", "network": "udp", "port": 443, "outboundTag": "block"},
        {"type": "field", "domain": list(GOOGLE_DOMAINS), "outboundTag": REMNAWAVE_OUTBOUND_TAG},
    ]


def remnawave_snippet(port: int) -> dict:
    outbound = json.dumps(remnawave_outbound(port), indent=2, ensure_ascii=False)
    rules = json.dumps(remnawave_rules(), indent=2, ensure_ascii=False)
    text = (
        f"1. В конфиге Xray (Remnawave → Config Profiles) добавьте outbound в массив \"outbounds\":\n{outbound}\n\n"
        f"2. В \"routing\".\"rules\" добавьте правила выше остальных правил для Google "
        f"(первое глушит QUIC — socks не несёт UDP, второе ведёт весь Google в exit-прокси):\n{rules}\n\n"
        "3. Убедитесь, что есть outbound с тегом \"block\" (protocol \"blackhole\") "
        "и что sniffing на inbound включён (destOverride http, tls) — иначе доменные правила не сработают.\n\n"
        f"Порт {port} одинаков на всех нодах, поэтому кусок конфига общий. "
        "YouTube и видео в этот outbound не отправляйте: прокси рассчитан на Gemini, поиск и API."
    )
    return {"outbound_json": outbound, "rules_json": rules, "text": text}
