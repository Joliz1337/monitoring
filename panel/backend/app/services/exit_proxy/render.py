"""Чистые функции: конфиг для ноды, его хэш и кусок конфига Remnawave для пользователя."""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional

from app.models import ExitProxyNode
from app.services.exit_proxy.settings import SettingsSnapshot, load_json

CHECK_TIMEOUT_SEC = 15
REMNAWAVE_OUTBOUND_TAG = "exit-proxy"
# Теги outbound'ов из конфига Remnawave по умолчанию
DIRECT_OUTBOUND_TAG = "DIRECT"
BLOCK_OUTBOUND_TAG = "BLOCK"
# Весь Google одним путём: половинчатая маршрутизация даёт «IP A ≠ IP B» в одной сессии
GOOGLE_DOMAINS = [
    "geosite:google",
    "geosite:google-gemini",
    "domain:googleapis.com",
    "domain:gstatic.com",
    "domain:googleusercontent.com",
]
# geosite:google тянет за собой include:youtube, include:googlefcm и include:google-play:
# видео, постоянное push-соединение каждого Android-устройства и загрузки Play гео не
# нужно, а через релей это тысячи соединений и гигабиты — они уходят прямым outbound'ом
GOOGLE_BYPASS_DOMAINS = ["geosite:youtube", "geosite:googlefcm", "geosite:google-play"]


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
        {"type": "field", "domain": list(GOOGLE_BYPASS_DOMAINS), "outboundTag": DIRECT_OUTBOUND_TAG},
        {"type": "field", "network": "udp", "port": 443, "outboundTag": BLOCK_OUTBOUND_TAG},
        {"type": "field", "domain": list(GOOGLE_DOMAINS), "outboundTag": REMNAWAVE_OUTBOUND_TAG},
    ]


def remnawave_snippet(port: int) -> dict:
    outbound = json.dumps(remnawave_outbound(port), indent=2, ensure_ascii=False)
    rules = json.dumps(remnawave_rules(), indent=2, ensure_ascii=False)
    text = (
        f"1. В конфиге Xray (Remnawave → Config Profiles) добавьте outbound в массив \"outbounds\":\n{outbound}\n\n"
        f"2. В \"routing\".\"rules\" добавьте правила выше остальных правил для Google "
        f"(первое выпускает YouTube, push-соединения Android и загрузки Play Store прямым outbound'ом — "
        f"им гео не нужно, а через прокси это тысячи соединений; второе глушит QUIC — socks не несёт UDP; "
        f"третье ведёт остальной Google в exit-прокси):\n{rules}\n\n"
        f"3. Убедитесь, что есть outbound'ы с тегами \"{DIRECT_OUTBOUND_TAG}\" (protocol \"freedom\") и "
        f"\"{BLOCK_OUTBOUND_TAG}\" (protocol \"blackhole\") — если у вас они называются иначе, подставьте свои теги — "
        "и что sniffing на inbound включён (destOverride http, tls) — иначе доменные правила не сработают.\n\n"
        f"Порт {port} одинаков на всех нодах, поэтому кусок конфига общий. "
        "Прокси рассчитан на Gemini, поиск и API; видео в него не отправляйте — YouTube уже выведен первым правилом."
    )
    return {"outbound_json": outbound, "rules_json": rules, "text": text}
