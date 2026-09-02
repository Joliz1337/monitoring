"""Представления для API панели: статус ноды агента → строка раздела. Без БД и сети."""

from typing import Any, Optional

from app.models import ExitProxyNode, Server
from app.services.exit_proxy.node_client import node_supports_exit_proxy
from app.services.exit_proxy.settings import load_json
from app.services.node_capabilities import Capability, server_allows

STATUS_OFF = "off"
STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_FAILED = "failed"
STATUS_DENIED = "denied"
STATUS_UNSUPPORTED = "unsupported"


def install_status(row: Optional[ExitProxyNode], node_status: dict) -> str:
    if row is None or not row.enabled:
        return STATUS_OFF
    if row.sync_status in (STATUS_DENIED, STATUS_UNSUPPORTED, STATUS_FAILED):
        return row.sync_status
    if row.sync_status != "synced":
        return STATUS_PENDING
    if not node_status:
        return STATUS_PENDING
    return STATUS_ACTIVE if node_status.get("listening") else STATUS_FAILED


def candidate_view(candidate: dict) -> dict:
    last = candidate.get("last_check") or {}
    kind = candidate.get("kind")
    return {
        "tag": candidate.get("id"),
        "kind": kind,
        "label": "WARP" if kind == "warp" else candidate.get("address"),
        "ip": last.get("ip") or (candidate.get("address") if kind == "ip" else None),
        "primary": bool(candidate.get("primary")),
        "managed": bool(candidate.get("managed")),
        "priority": candidate.get("priority", 0),
        "enabled": bool(candidate.get("enabled", True)),
        "healthy": candidate.get("healthy"),
        "country": last.get("country"),
        "country_confirm": last.get("country_confirm"),
        "captcha": bool(last.get("captcha")),
        "gemini": last.get("gemini"),
        "checks": {
            item.get("name", "?"): {"ok": bool(item.get("ok")), "status": item.get("status"), "detail": item.get("detail", "")}
            for item in last.get("checks", [])
        },
        "checked_at": last.get("checked_at"),
        "error": last.get("error"),
    }


def current_exit_view(node_status: dict) -> Optional[dict]:
    current = node_status.get("current")
    if not current:
        return None
    for candidate in node_status.get("candidates", []):
        if candidate.get("id") == current:
            view = candidate_view(candidate)
            return {"tag": view["tag"], "label": view["label"], "ip": view["ip"], "country": view["country"], "healthy": view["healthy"]}
    return {"tag": current, "label": current.removeprefix("ip:"), "ip": None, "country": None, "healthy": None}


def node_view(server: Server, row: Optional[ExitProxyNode], online: bool) -> dict[str, Any]:
    node_status = load_json(row.node_status, {}) if row is not None else {}
    self_test = node_status.get("self_test")
    stats = node_status.get("stats") or {}
    denied_now = not server_allows(server, Capability.SYSTEM, write=True)
    status = install_status(row, node_status)
    if row is not None and row.enabled and status not in (STATUS_OFF,):
        if not node_supports_exit_proxy(server.node_version):
            status = STATUS_UNSUPPORTED
        elif denied_now:
            status = STATUS_DENIED
    return {
        "server_id": server.id,
        "name": server.name,
        "folder": server.folder,
        "online": online,
        "node_version": server.node_version,
        "enabled": bool(row.enabled) if row is not None else False,
        "install_status": status,
        "sync_error": row.sync_error if row is not None else None,
        "listening": bool(node_status.get("listening")),
        "listen_error": node_status.get("listen_error"),
        "select_mode": (row.select_mode if row is not None else None) or "auto",
        "pinned_candidate": row.pinned_candidate if row is not None else None,
        "current_exit": current_exit_view(node_status),
        "candidates": [candidate_view(candidate) for candidate in node_status.get("candidates", [])],
        "warp": {"present": bool(node_status.get("warp_present"))},
        "check_in_progress": bool(node_status.get("check_in_progress")),
        "last_check_at": node_status.get("last_check_at"),
        "last_check_error": node_status.get("last_check_error"),
        "self_test": {
            "ok": bool(self_test.get("ok")),
            "ip": self_test.get("ip"),
            "expected": self_test.get("expected"),
            "at": self_test.get("at"),
            "error": self_test.get("error"),
        } if self_test else None,
        "stats": {
            "active_connections": stats.get("active_connections", 0),
            "total_connections": stats.get("total_connections", 0),
            "failed_connections": stats.get("failed_connections", 0),
        },
        "last_status_at": row.last_status_at.isoformat() if row is not None and row.last_status_at else None,
    }


def new_node_events(events: list[dict], last_seen_at: Optional[str]) -> list[dict]:
    """События ноды, которых панель ещё не видела, по возрастанию времени.

    Метки — ISO-8601 в UTC одного формата, поэтому сравниваются как строки.
    """
    fresh = [event for event in events if event.get("at") and (last_seen_at is None or event["at"] > last_seen_at)]
    return sorted(fresh, key=lambda event: event["at"])
