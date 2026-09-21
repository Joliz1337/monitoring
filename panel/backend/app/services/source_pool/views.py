"""Представления для API панели: состояние ноды агента → карточка сервера. Без БД и сети."""

from typing import Any, Optional

from app.models import Server, SourcePoolNode
from app.services.exit_proxy.settings import load_json
from app.services.node_capabilities import Capability, server_allows
from app.services.source_pool.node_client import MIN_NODE_VERSION_SOURCE_POOL, node_supports_source_pool

STATUS_OFF = "off"
STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_DRIFT = "drift"
STATUS_FAILED = "failed"
STATUS_DENIED = "denied"
STATUS_UNSUPPORTED = "unsupported"


def install_status(row: Optional[SourcePoolNode], node_state: dict) -> str:
    if row is None or not row.enabled:
        return STATUS_OFF
    if row.sync_status in (STATUS_DENIED, STATUS_UNSUPPORTED, STATUS_FAILED):
        return row.sync_status
    if row.sync_status != "synced" or not node_state:
        return STATUS_PENDING
    if not node_state.get("supported", True):
        return STATUS_FAILED
    # Раскладка задана, но в ядре не вся — нода вернёт её сама в течение цикла самолечения
    return STATUS_ACTIVE if node_state.get("in_sync") else STATUS_DRIFT


def marks_per_address(node_state: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for binding in node_state.get("bindings") or []:
        address = binding.get("address")
        if address:
            counts[address] = counts.get(address, 0) + 1
    return counts


def node_view(server: Server, row: Optional[SourcePoolNode], online: bool) -> dict[str, Any]:
    node_state = load_json(row.node_state, {}) if row is not None else {}
    status = install_status(row, node_state)
    if row is not None and row.enabled and status != STATUS_OFF:
        if not node_supports_source_pool(server.node_version):
            status = STATUS_UNSUPPORTED
        elif not server_allows(server, Capability.SYSTEM, write=True):
            status = STATUS_DENIED
    excluded = set(load_json(row.excluded, [])) if row is not None else set()
    counts = marks_per_address(node_state)
    return {
        "server_id": server.id,
        "name": server.name,
        "online": online,
        "node_version": server.node_version,
        "min_node_version": MIN_NODE_VERSION_SOURCE_POOL,
        "supported_by_node": node_supports_source_pool(server.node_version),
        "enabled": bool(row.enabled) if row is not None else False,
        "install_status": status,
        "sync_error": row.sync_error if row is not None else None,
        "interface": node_state.get("interface"),
        "addresses": [
            {"address": address, "excluded": address in excluded, "marks": counts.get(address, 0)}
            for address in node_state.get("addresses") or []
        ],
        "excluded": sorted(excluded),
        "active_count": len(node_state.get("active_addresses") or []),
        "mark_count": node_state.get("mark_count"),
        "mark_base": node_state.get("mark_base"),
        "in_sync": bool(node_state.get("in_sync")),
        "missing_marks": node_state.get("missing_marks") or [],
        "conflict": node_state.get("conflict"),
        "node_error": node_state.get("last_error") or node_state.get("reason"),
        "last_state_at": row.last_state_at.isoformat() if row is not None and row.last_state_at else None,
    }
