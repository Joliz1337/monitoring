"""Anti-DDoS emergency-mode + whitelist router.

No auth dependency: nginx terminates mTLS with the panel CA before requests
reach uvicorn (same model as every other node router).
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.antiddos_manager import get_antiddos_manager

router = APIRouter(prefix="/api/antiddos", tags=["antiddos"])


class EmergencyRequest(BaseModel):
    enabled: bool = Field(..., description="Turn emergency mode on/off")


class WatchdogRequest(BaseModel):
    enabled: bool = Field(..., description="Enable/disable the auto-detection watchdog")


class WhitelistSyncRequest(BaseModel):
    ips: list[str] = Field(default_factory=list, description="Full IP/CIDR whitelist (replaces current)")


class InstallRequest(BaseModel):
    script_content: str = Field(..., description="ddos-watchdog.sh contents")
    service_content: str = Field(..., description="ddos-watchdog.service contents")
    watchdog_enabled: bool = Field(True, description="Enable auto-detection by default")


def _status_dict(status) -> dict:
    return {
        "installed": status.installed,
        "mode": status.mode,
        "source": status.source,
        "since": status.since,
        "reason": status.reason,
        "watchdog": status.watchdog,
        "watchdog_active": status.watchdog_active,
        "client_ports": status.client_ports,
        "version": status.version,
    }


@router.get("/status")
async def get_status():
    status = await get_antiddos_manager().get_status()
    return _status_dict(status)


@router.post("/emergency")
async def set_emergency(request: EmergencyRequest):
    manager = get_antiddos_manager()
    if request.enabled:
        ok, msg = await manager.enable_emergency(source="manual")
    else:
        ok, msg = await manager.disable_emergency()
    status = await manager.get_status()
    return {"success": ok, "message": msg, "status": _status_dict(status)}


@router.post("/watchdog")
async def set_watchdog(request: WatchdogRequest):
    manager = get_antiddos_manager()
    ok, msg = await manager.set_watchdog(request.enabled)
    return {"success": ok, "message": msg}


@router.post("/whitelist/sync")
async def sync_whitelist(request: WhitelistSyncRequest):
    ok, msg, count = await get_antiddos_manager().sync_whitelist(request.ips)
    return {"success": ok, "message": msg, "count": count}


@router.post("/install")
async def install_watchdog(request: InstallRequest):
    ok, msg = await get_antiddos_manager().install(
        request.script_content, request.service_content, request.watchdog_enabled
    )
    status = await get_antiddos_manager().get_status()
    return {"success": ok, "message": msg, "status": _status_dict(status)}


@router.get("/client-ports")
async def get_client_ports():
    ports = await get_antiddos_manager().get_client_ports()
    return {"ports": ports}
