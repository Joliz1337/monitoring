"""Представление для API панели: туннели из базы + состояние с ноды → карточка
сервера. Без БД и сети."""

from datetime import datetime, timezone
from typing import Any, Optional

from app.models import GreNode, Server
from app.services.exit_proxy.settings import load_json
from app.services.gre.activity import SILENT_AFTER_SECONDS, activity_status
from app.services.gre.node_client import MIN_NODE_VERSION_GRE, node_supports_gre
from app.services.gre.render import DEFAULT_HEALTH_NET, DEFAULT_REMOTE, MAX_TUNNELS, interface_name, load_tunnels
from app.services.node_capabilities import Capability, server_allows

STATUS_OFF = "off"
STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_DRIFT = "drift"
STATUS_FAILED = "failed"
STATUS_DENIED = "denied"
STATUS_UNSUPPORTED = "unsupported"


def install_status(row: Optional[GreNode], has_tunnels: bool, node_state: dict) -> str:
    # Без туннелей и снимать нечего: снятие доехало или агент GRE вовсе не знает
    if row is None or (not has_tunnels and row.sync_status in ("synced", STATUS_UNSUPPORTED)):
        return STATUS_OFF
    if row.sync_status in (STATUS_DENIED, STATUS_UNSUPPORTED, STATUS_FAILED):
        return row.sync_status
    if row.sync_status != "synced" or not node_state:
        return STATUS_PENDING
    if not node_state.get("supported", True):
        return STATUS_FAILED
    # Заданное в ядре не всё — служба на ноде вернёт его на ближайшем событии
    return STATUS_ACTIVE if node_state.get("in_sync") else STATUS_DRIFT


def node_view(server: Server, row: Optional[GreNode], online: bool, now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    tunnels = load_tunnels(row)
    node_state = load_json(row.node_state, {}) if row is not None else {}
    activity = load_json(row.activity, {}) if row is not None else {}
    live = {item.get("slot"): item for item in node_state.get("tunnels") or [] if isinstance(item, dict)}

    status = install_status(row, bool(tunnels), node_state)
    if tunnels and status != STATUS_OFF:
        if not node_supports_gre(server.node_version):
            status = STATUS_UNSUPPORTED
        elif not server_allows(server, Capability.SYSTEM, write=True):
            status = STATUS_DENIED

    views = []
    for tunnel in tunnels:
        name = interface_name(tunnel.slot)
        state = live.get(tunnel.slot) or {}
        # Нода ещё не получила этот вариант туннеля — её цифры относятся к прежнему
        applied = state.get("ce") == tunnel.ce and state.get("local") == tunnel.local
        activity_state, last_rx_at = activity_status(activity.get(name) if applied else None, now)
        views.append({
            **tunnel.model_dump(),
            "name": name,
            "link": state.get("link") if applied else None,
            "mtu": state.get("mtu") if applied else None,
            "conflict_with": state.get("conflict_with") if applied else None,
            "rx_bytes": state.get("rx_bytes", 0) if applied else 0,
            "tx_bytes": state.get("tx_bytes", 0) if applied else 0,
            "rx_packets": state.get("rx_packets", 0) if applied else 0,
            "tx_packets": state.get("tx_packets", 0) if applied else 0,
            "activity": activity_state.value,
            "last_rx_at": last_rx_at,
        })

    return {
        "server_id": server.id,
        "name": server.name,
        "online": online,
        "node_version": server.node_version,
        "min_node_version": MIN_NODE_VERSION_GRE,
        "supported_by_node": node_supports_gre(server.node_version),
        "install_status": status,
        "sync_error": row.sync_error if row is not None else None,
        "node_error": node_state.get("last_error") or node_state.get("message"),
        "notify": bool(row.notify) if row is not None else True,
        "tunnels": views,
        "max_tunnels": MAX_TUNNELS,
        "default_remote": DEFAULT_REMOTE,
        "default_health_net": DEFAULT_HEALTH_NET,
        "silent_after_seconds": SILENT_AFTER_SECONDS,
        "last_state_at": row.last_state_at.isoformat() if row is not None and row.last_state_at else None,
    }
