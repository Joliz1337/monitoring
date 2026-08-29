"""Профили клиентов подписки: заголовки устройства и HWID.

Панели выдачи ключей смотрят, чем пришёл запрос. Если клиент не передал
идентификатор устройства, а в панели включена привязка по HWID, вместо ключей
приходит текст-инструкция («у вас выключена передача hwid») — формально
валидная подписка, из которой нечего проверять.

HWID выводится из адреса подписки детерминированно (uuid5). Случайный
идентификатор на каждый запрос регистрировал бы в чужой панели новое
устройство и съедал лимит владельца ключа — проверка не должна иметь таких
побочных эффектов.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

# Пространство имён фиксировано: от него зависит стабильность HWID между
# перезапусками панели и между разными панелями с тем же адресом подписки
HWID_NAMESPACE = uuid.UUID("6f5c1d5a-2b0f-4a3e-9c7d-51f0c2b8a4e1")

HAPP_MOBILE = "3.13.0"
HAPP_DESKTOP = "1.9.0"


@dataclass(frozen=True)
class DeviceProfile:
    id: str
    title: str
    user_agent: str
    os: Optional[str] = None
    os_version: Optional[str] = None
    model: Optional[str] = None
    locale: str = "ru-RU"
    sends_hwid: bool = False


PROFILES: tuple[DeviceProfile, ...] = (
    DeviceProfile(
        id="happ-ios", title="Happ · iPhone", user_agent=f"Happ/{HAPP_MOBILE}",
        os="iOS", os_version="18.3", model="iPhone 15 Pro", sends_hwid=True,
    ),
    DeviceProfile(
        id="happ-android", title="Happ · Android", user_agent=f"Happ/{HAPP_MOBILE}",
        os="Android", os_version="14", model="Samsung SM-S918B", sends_hwid=True,
    ),
    DeviceProfile(
        id="happ-windows", title="Happ · Windows", user_agent=f"Happ/{HAPP_DESKTOP}",
        os="Windows", os_version="10.0.19045", model="PC", sends_hwid=True,
    ),
    DeviceProfile(
        id="happ-macos", title="Happ · macOS", user_agent=f"Happ/{HAPP_DESKTOP}",
        os="macOS", os_version="14.5", model="MacBook Pro", locale="en-US", sends_hwid=True,
    ),
    DeviceProfile(id="v2rayng", title="v2rayNG", user_agent="v2rayNG/1.9.24"),
    DeviceProfile(id="clash", title="Clash", user_agent="clash-verge/v1.7.7"),
    DeviceProfile(id="singbox", title="sing-box", user_agent="sing-box/1.13.19"),
    DeviceProfile(
        id="browser", title="Браузер",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    ),
)

DEFAULT_PROFILE_ID = PROFILES[0].id
_BY_ID = {profile.id: profile for profile in PROFILES}


def get_profile(profile_id: Optional[str]) -> DeviceProfile:
    return _BY_ID.get((profile_id or "").strip(), _BY_ID[DEFAULT_PROFILE_ID])


def hwid_for(url: str) -> str:
    """Стабильный идентификатор устройства для конкретной подписки."""
    return str(uuid.uuid5(HWID_NAMESPACE, url.strip()))


def build_headers(profile_id: Optional[str], url: str) -> dict[str, str]:
    profile = get_profile(profile_id)
    headers = {
        "User-Agent": profile.user_agent,
        "Accept": "*/*",
        "Accept-Language": profile.locale,
    }
    if not profile.sends_hwid:
        return headers

    headers["x-hwid"] = hwid_for(url)
    if profile.os:
        headers["x-device-os"] = profile.os
    if profile.os_version:
        headers["x-ver-os"] = profile.os_version
    if profile.model:
        headers["x-device-model"] = profile.model
    headers["x-device-locale"] = profile.locale
    return headers


def describe() -> list[dict]:
    return [
        {
            "id": profile.id,
            "title": profile.title,
            "user_agent": profile.user_agent,
            "sends_hwid": profile.sends_hwid,
        }
        for profile in PROFILES
    ]
