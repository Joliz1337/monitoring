"""Включение и выключение nginx Remnawave на ноде.

«Выключен» — желаемое состояние, которое панель хранит у привязанного сервера
(`Server.remnawave_nginx_disabled`). Нода останавливает контейнер так, что он
не поднимается после перезагрузки, но `docker start` из хука certbot или
`docker compose up -d` при обновлении remnanode запускают его снова. Поэтому
состояние держит панель: раскатки такой nginx не поднимают, а поднятый в обход
неё останавливается при следующей проверке.
"""

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_maker
from app.models import Server
from app.services.haproxy_profile_sync import is_server_online
from app.services.http_client import get_node_client, node_auth_headers
from app.services.node_capabilities import Capability, server_allows
from app.services.remnawave_nginx_sync import get_remnawave_nginx_path
from app.services.reserved_ports_sync import _version_tuple

logger = logging.getLogger(__name__)

MIN_NODE_VERSION_NGINX_POWER = "10.36.2"
NODE_TOO_OLD_MESSAGE = (
    "Нода не умеет включать и выключать nginx Remnawave — обновите агент ноды "
    f"(нужна версия ≥ {MIN_NODE_VERSION_NGINX_POWER})"
)

# Оба таймаута длиннее худшего случая ноды: иначе панель сочла бы исход
# неизвестным, пока нода ещё доделывает своё. Остановка на ноде — до 70 секунд
# (inspect, docker stop, docker update); запуск обрывает nginx самой ноды
# через 120 секунд
STOP_TIMEOUT_SECONDS = 90.0
START_TIMEOUT_SECONDS = 130.0
STATUS_TIMEOUT_SECONDS = 10.0
MAX_CONCURRENT_CHECKS = 10

KEPT_OFF_NOTE = "nginx помечен выключенным: панель остановит его, как только нода ответит"
KEPT_ON_NOTE = (
    "nginx помечен включённым: если контейнер не поднялся, запустите его кнопкой перезапуска"
)


@dataclass
class PowerResult:
    success: bool
    message: str
    # False — нода не дала ответа (обрыв связи, таймаут, 5xx): чем кончился
    # запрос, неизвестно. Отказ с ответом ноды — определённый
    definite: bool = True


class UnlinkStopSkip(str, Enum):
    """Почему nginx на отвязанной ноде остановить нельзя."""
    OFFLINE = "offline"
    DENIED = "denied"
    OLD_AGENT = "old_agent"


async def _post_power(
    server: Server, action: str, timeout: float, params: Optional[dict] = None,
) -> PowerResult:
    """Сбой связи возвращается результатом, а не исключением: у кнопки, отвязки
    и фоновой проверки на него разная реакция."""
    url = f"{server.url}/api/remnawave/nginx/{action}"
    try:
        response = await get_node_client(server).post(
            url, headers=node_auth_headers(server), params=params, timeout=timeout,
        )
    except httpx.TimeoutException:
        return PowerResult(False, "Нода не ответила вовремя", definite=False)
    except httpx.RequestError as e:
        return PowerResult(False, f"Нет связи с нодой: {e}", definite=False)

    # Старый агент этих путей не знает
    if response.status_code == 404:
        return PowerResult(False, NODE_TOO_OLD_MESSAGE)
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        data = {}
    if response.status_code != 200:
        # 5xx — агент упал или его nginx оборвал запрос на полпути
        return PowerResult(
            False, str(data.get("detail") or f"HTTP {response.status_code}"),
            definite=response.status_code < 500,
        )
    return PowerResult(bool(data.get("success")), str(data.get("message") or ""))


async def stop_nginx_on_node(server: Server) -> PowerResult:
    return await _post_power(server, "stop", STOP_TIMEOUT_SECONDS)


async def start_nginx_on_node(server: Server, install_path: str) -> PowerResult:
    return await _post_power(
        server, "start", START_TIMEOUT_SECONDS, params={"path": install_path},
    )


async def set_nginx_enabled(db: AsyncSession, server: Server, enabled: bool) -> PowerResult:
    """Меняет желаемое состояние nginx и приводит к нему ноду.

    Флаг пишется до обращения к ноде: иначе досинхронизация или фоновая проверка
    успели бы вернуть контейнер в прежнее состояние между ответом ноды и коммитом.
    Возвращается он только по определённому отказу ноды. Ответа нет (таймаут,
    обрыв) — нода могла довести дело до конца, и откат развёл бы панель с ней:
    «включён» в панели при остановленном контейнере либо «выключен» при
    запущенном, который фоновая проверка тут же погасила бы. Выбор оператора
    остаётся в силе, а к выключенному состоянию ноду приведёт фоновая проверка.
    """
    install_path = await get_remnawave_nginx_path(db)
    was_disabled = server.remnawave_nginx_disabled
    server.remnawave_nginx_disabled = not enabled
    await db.commit()

    if enabled:
        result = await start_nginx_on_node(server, install_path)
    else:
        result = await stop_nginx_on_node(server)

    if not result.success and result.definite:
        server.remnawave_nginx_disabled = was_disabled
        await db.commit()
    elif not result.success:
        note = KEPT_ON_NOTE if enabled else KEPT_OFF_NOTE
        result = PowerResult(False, f"{result.message}. {note}", definite=False)
    logger.info(
        "remnawave_nginx_power server=%s(%s) enabled=%s success=%s definite=%s message=%s",
        server.id, server.name, enabled, result.success, result.definite, result.message,
    )
    return result


def unlink_stop_skip(server: Server) -> Optional[UnlinkStopSkip]:
    """Причина, по которой отвязанной ноде nginx не остановить; None — можно."""
    if not is_server_online(server):
        return UnlinkStopSkip.OFFLINE
    if not server_allows(server, Capability.REMNAWAVE, write=True):
        return UnlinkStopSkip.DENIED
    # Версия ещё не известна — пробуем: старый агент ответит 404, и это уйдёт в лог
    version = server.node_version
    if version and _version_tuple(version) < _version_tuple(MIN_NODE_VERSION_NGINX_POWER):
        return UnlinkStopSkip.OLD_AGENT
    return None


async def stop_nginx_after_unlink(server: Server) -> bool:
    """Гасит nginx на ноде, отвязанной от профиля. Отвязка в БД уже выполнена,
    поэтому недоступная нода — не ошибка: остановка делается по возможности."""
    skip = unlink_stop_skip(server)
    if skip is not None:
        logger.info(
            "Remnawave nginx: остановка после отвязки пропущена на %s (%s)", server.name, skip.value,
        )
        return False
    result = await stop_nginx_on_node(server)
    if result.success:
        logger.info(
            "Remnawave nginx остановлен на сервере %s после отвязки: %s", server.name, result.message,
        )
    else:
        logger.warning(
            "Remnawave nginx: остановка на %s после отвязки не удалась: %s",
            server.name, result.message,
        )
    return result.success


async def _nginx_running(server: Server, install_path: str) -> bool:
    try:
        response = await get_node_client(server).get(
            f"{server.url}/api/remnawave/nginx/status",
            headers=node_auth_headers(server),
            params={"path": install_path},
            timeout=STATUS_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            return False
        return bool(response.json()["container"]["running"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return False


async def _still_disabled(server_id: int) -> bool:
    async with async_session_maker() as db:
        result = await db.execute(
            select(Server.remnawave_nginx_disabled).where(Server.id == server_id)
        )
        return bool(result.scalar_one_or_none())


async def stop_reappeared_nginx() -> int:
    """Останавливает nginx, выключенный в панели, но снова работающий на ноде.

    Опрашиваются только серверы с флагом, поэтому цена проверки не растёт
    вместе с числом нод.
    """
    async with async_session_maker() as db:
        result = await db.execute(
            select(Server).where(
                Server.active_remnawave_nginx_profile_id.isnot(None),
                Server.remnawave_nginx_disabled.is_(True),
                Server.is_active.is_(True),
            )
        )
        servers = [
            s for s in result.scalars().all()
            if is_server_online(s) and server_allows(s, Capability.REMNAWAVE, write=True)
        ]
        if not servers:
            return 0
        install_path = await get_remnawave_nginx_path(db)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

    async def settle(server: Server) -> bool:
        async with semaphore:
            if not await _nginx_running(server, install_path):
                return False
            # Пока шёл опрос, оператор мог включить nginx обратно: список
            # серверов снят раньше, а запущенный контейнер — уже его работа
            if not await _still_disabled(server.id):
                return False
            result = await stop_nginx_on_node(server)
        if result.success:
            logger.info(
                "Remnawave nginx: выключенный в панели nginx снова работал на %s — остановлен",
                server.name,
            )
        else:
            logger.warning(
                "Remnawave nginx: не удалось остановить выключенный nginx на %s: %s",
                server.name, result.message,
            )
        return result.success

    return sum(await asyncio.gather(*[settle(s) for s in servers]))
