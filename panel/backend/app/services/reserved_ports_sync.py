"""Резервация портов от эфемерной выдачи: хранение списков и доставка на ноды.

Ядро ноды не выдаёт зарезервированный порт исходящим соединениям как source-порт
(net.ipv4.ip_local_reserved_ports), поэтому рестарт сервиса на таком порту не
проигрывает гонку эфемерной выдаче. Базовые порты (7500, порт API ноды, 2222)
нода резервирует сама; панель управляет только дополнительными: общий список на
весь парк (panel_settings) плюс список конкретного сервера (Server.reserved_ports).

Доставка — лёгкой ручкой ноды POST /api/system/reserved-ports с объединённым
списком; офлайн-ноды и сбои закрывает очередь node_pending_sync.
"""

import asyncio
import logging
from typing import Optional

from sqlalchemy import select

from app.database import async_session
from app.models import Server
from app.services.haproxy_profile_sync import is_server_online
from app.services.http_client import get_node_client, node_auth_headers
from app.services.node_capabilities import Capability, server_allows

logger = logging.getLogger(__name__)

MIN_NODE_VERSION_RESERVED_PORTS = "10.28.0"

# Тот же потолок конкурентности, что у остальных массовых рассылок; таймаут
# больше обычного — POST на ноде запускает ре-рендер sysctl.
CONCURRENCY = 50
NODE_TIMEOUT_SEC = 60.0

# Зеркало ограничений агента (node/app/services/reserved_ports.py): валидируем
# на входе в панель, чтобы оператор получил понятную ошибку сразу, а не отказ
# ноды при рассылке.
MAX_ENTRIES = 64
MAX_TOTAL_PORTS = 4096


def _parse_entry(token: str) -> tuple[int, int]:
    start_str, sep, end_str = token.partition("-")
    if not start_str.strip().isdigit() or (sep and not end_str.strip().isdigit()):
        raise ValueError(f"Не порт и не диапазон: {token!r}")
    start = int(start_str)
    end = int(end_str) if sep else start
    if not (1 <= start <= 65535 and 1 <= end <= 65535):
        raise ValueError(f"Порт вне диапазона 1–65535: {token!r}")
    if start > end:
        raise ValueError(f"Начало диапазона больше конца: {token!r}")
    return start, end


def parse_ports_value(value: Optional[str]) -> list[str]:
    """Строка оператора/БД ("5201, 8443-8450") → нормализованный список записей.

    Пустая строка и None — пустой список; мусор — ValueError с внятным текстом.
    """
    tokens = [
        t for t in (value or "").replace(",", " ").replace(";", " ").split() if t
    ]
    if len(tokens) > MAX_ENTRIES:
        raise ValueError(f"Слишком много записей: {len(tokens)} > {MAX_ENTRIES}")

    parsed = sorted({_parse_entry(t) for t in tokens})
    total = sum(end - start + 1 for start, end in parsed)
    if total > MAX_TOTAL_PORTS:
        raise ValueError(
            f"Резервируется {total} портов — больше потолка {MAX_TOTAL_PORTS}, "
            "эфемерному диапазону ничего не останется"
        )
    return [str(s) if s == e else f"{s}-{e}" for s, e in parsed]


def merged_entries(global_value: Optional[str], server_value: Optional[str]) -> list[str]:
    """Объединённый список для отправки на ноду: общий + серверный, без дублей."""
    return parse_ports_value(f"{global_value or ''} {server_value or ''}")


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for chunk in (value or "").strip().split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def node_supports_reserved_ports(node_version: Optional[str]) -> bool:
    if not node_version:
        return False
    return _version_tuple(node_version) >= _version_tuple(MIN_NODE_VERSION_RESERVED_PORTS)


async def _push_to_node(server: Server, entries: list[str]) -> Optional[str]:
    """None — успех, иначе текст ошибки. Ответ ноды с applied=false — тоже успех:
    файл на ноде сохранён, рендерер подхватит его при применении оптимизаций."""
    if not server_allows(server, Capability.SYSTEM, write=True):
        return "нода закрыла домен system для записи (NODE_CAPABILITIES)"
    if not node_supports_reserved_ports(server.node_version):
        return (
            f"агент {server.node_version or 'unknown'} старше "
            f"{MIN_NODE_VERSION_RESERVED_PORTS} — обновите ноду"
        )
    try:
        client = get_node_client(server)
        response = await client.post(
            f"{server.url}/api/system/reserved-ports",
            json={"extra_ports": entries},
            headers=node_auth_headers(server),
            timeout=NODE_TIMEOUT_SEC,
        )
        if response.status_code == 200:
            return None
        return f"HTTP {response.status_code}: {response.text[:200]}"
    except Exception as e:
        return str(e)


async def push_reserved_ports_to_servers(server_ids: list[int]) -> dict[int, Optional[str]]:
    """Исполнитель вида долга KIND_RESERVED_PORTS: по каждой ноде None или ошибка.

    Желаемое состояние собирается в момент отправки (общий список из настроек +
    серверный из БД) — за время простоя ноды устаревшие команды не копятся.
    """
    from app.routers.settings import RESERVED_PORTS_KEY, get_setting

    async with async_session() as db:
        global_value = await get_setting(RESERVED_PORTS_KEY, db)
        result = await db.execute(select(Server).where(Server.id.in_(server_ids)))
        servers = list(result.scalars().all())

    results: dict[int, Optional[str]] = {}
    sem = asyncio.Semaphore(CONCURRENCY)

    async def _one(server: Server) -> None:
        try:
            entries = merged_entries(global_value, server.reserved_ports)
        except ValueError as e:
            # Битое значение в БД (правка руками) — внятная ошибка вместо 400 от ноды
            results[server.id] = str(e)
            return
        async with sem:
            results[server.id] = await _push_to_node(server, entries)

    await asyncio.gather(*(_one(s) for s in servers))
    return results


async def apply_reserved_ports(
    server_ids: Optional[list[int]] = None, reason: str = "reserved ports changed"
) -> dict:
    """Разослать актуальные списки: живым нодам — сразу, офлайн и сбоям — долг.

    server_ids=None — все активные ноды (смена общего списка).
    """
    from app.services import node_sync_queue

    async with async_session() as db:
        query = select(Server).where(Server.is_active == True)  # noqa: E712
        if server_ids is not None:
            query = query.where(Server.id.in_(server_ids))
        servers = list((await db.execute(query)).scalars().all())

    online = [s.id for s in servers if is_server_online(s)]
    offline = [s.id for s in servers if not is_server_online(s)]

    if offline:
        await node_sync_queue.enqueue(offline, node_sync_queue.KIND_RESERVED_PORTS, reason)

    results = await push_reserved_ports_to_servers(online) if online else {}
    failed = {sid: err for sid, err in results.items() if err is not None}
    if failed:
        await node_sync_queue.enqueue(
            list(failed), node_sync_queue.KIND_RESERVED_PORTS, next(iter(failed.values()))
        )

    updated = len(results) - len(failed)
    logger.info(
        "reserved ports pushed: %d ok, %d failed, %d queued (%s)",
        updated, len(failed), len(offline), reason,
    )
    return {
        "total": len(servers),
        "updated": updated,
        "queued": len(offline) + len(failed),
        "errors": {sid: err for sid, err in failed.items()},
    }
