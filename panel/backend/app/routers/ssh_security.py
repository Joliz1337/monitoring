import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import Server, PanelSettings
from app.services.ssh_manager import proxy_to_node, RECOMMENDED_PRESET, MAXIMUM_PRESET

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ssh-security", tags=["ssh-security"])


class BulkSSHConfigRequest(BaseModel):
    server_ids: list[int]
    config: dict


class BulkFail2banRequest(BaseModel):
    server_ids: list[int]
    config: dict


class BulkKeyRequest(BaseModel):
    server_ids: list[int]
    public_key: str
    user: str = "root"


class ChangePasswordRequest(BaseModel):
    user: str = "root"
    password: str = Field(..., min_length=8)


class BulkPasswordRequest(BaseModel):
    server_ids: list[int]
    user: str = "root"
    password: str = Field(..., min_length=8)


class CustomPresetSave(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    ssh: dict
    fail2ban: dict


class CustomPresetDelete(BaseModel):
    name: str


async def _get_server(server_id: int, db: AsyncSession) -> Server:
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return server


async def _safe_proxy(server, method: str, path: str, json_data: dict | None = None) -> dict:
    try:
        return await proxy_to_node(server, method, path, json_data)
    except LookupError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except (ConnectionError, TimeoutError, RuntimeError) as e:
        raise HTTPException(status_code=503, detail=str(e))


# Пути с долгой установкой (apt-get install fail2ban может занять 120с)
_LONG_TIMEOUT_PATHS = frozenset({"/api/ssh/fail2ban/config", "/api/ssh/password"})


async def _bulk_apply(server, method: str, path: str, json_data: dict | None = None) -> dict:
    timeout = 120.0 if path in _LONG_TIMEOUT_PATHS else 30.0
    try:
        result = await proxy_to_node(server, method, path, json_data, timeout=timeout)
        logger.debug("ssh_bulk_ok", extra={"server_id": server.id, "server": server.name, "path": path})
        return {"server_id": server.id, "server_name": server.name, "success": True, **result}
    except (ConnectionError, TimeoutError, LookupError, RuntimeError) as e:
        logger.warning("ssh_bulk_fail", extra={"server_id": server.id, "server": server.name, "path": path, "error": str(e)})
        return {"server_id": server.id, "server_name": server.name, "success": False, "error": str(e)}


async def _get_servers_by_ids(server_ids: list[int], db: AsyncSession) -> list[Server]:
    result = await db.execute(select(Server).where(Server.id.in_(server_ids)))
    servers = result.scalars().all()
    if not servers:
        raise HTTPException(status_code=404, detail="No servers found")
    return list(servers)


def _log_bulk_summary(action: str, results: list[dict]) -> None:
    total = len(results)
    ok = sum(1 for r in results if r.get("success"))
    failed = total - ok
    failed_names = [r["server_name"] for r in results if not r.get("success")]

    if failed == 0:
        logger.info(
            "ssh_bulk_summary",
            extra={"action": action, "total": total, "ok": ok, "failed": 0},
        )
    else:
        logger.warning(
            "ssh_bulk_summary",
            extra={
                "action": action,
                "total": total,
                "ok": ok,
                "failed": failed,
                "failed_servers": failed_names,
            },
        )


# === SSH Config ===

@router.get("/server/{server_id}/config")
async def get_ssh_config(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    server = await _get_server(server_id, db)
    return await _safe_proxy(server, "GET", "/api/ssh/config")


@router.post("/server/{server_id}/config")
async def update_ssh_config(
    server_id: int,
    config: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    server = await _get_server(server_id, db)
    return await _safe_proxy(server, "POST", "/api/ssh/config", config)


@router.post("/server/{server_id}/config/test")
async def test_ssh_config(
    server_id: int,
    config: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    server = await _get_server(server_id, db)
    return await _safe_proxy(server, "POST", "/api/ssh/config/test", config)


# === Fail2ban ===

@router.get("/server/{server_id}/fail2ban/status")
async def get_fail2ban_status(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    server = await _get_server(server_id, db)
    return await _safe_proxy(server, "GET", "/api/ssh/fail2ban/status")


@router.post("/server/{server_id}/fail2ban/config")
async def update_fail2ban_config(
    server_id: int,
    config: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    server = await _get_server(server_id, db)
    return await _safe_proxy(server, "POST", "/api/ssh/fail2ban/config", config)


@router.get("/server/{server_id}/fail2ban/banned")
async def get_fail2ban_banned(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    server = await _get_server(server_id, db)
    return await _safe_proxy(server, "GET", "/api/ssh/fail2ban/banned")


@router.post("/server/{server_id}/fail2ban/unban")
async def unban_ip(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    server = await _get_server(server_id, db)
    return await _safe_proxy(server, "POST", "/api/ssh/fail2ban/unban", data)


@router.post("/server/{server_id}/fail2ban/unban-all")
async def unban_all(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    server = await _get_server(server_id, db)
    return await _safe_proxy(server, "POST", "/api/ssh/fail2ban/unban-all")


# === SSH Keys ===

@router.get("/server/{server_id}/keys")
async def get_ssh_keys(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    server = await _get_server(server_id, db)
    return await _safe_proxy(server, "GET", "/api/ssh/keys")


@router.post("/server/{server_id}/keys")
async def add_ssh_key(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    server = await _get_server(server_id, db)
    return await _safe_proxy(server, "POST", "/api/ssh/keys", data)


@router.delete("/server/{server_id}/keys")
async def delete_ssh_key(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    server = await _get_server(server_id, db)
    return await _safe_proxy(server, "DELETE", "/api/ssh/keys", data)


# === Password ===

@router.post("/server/{server_id}/password")
async def change_password(
    server_id: int,
    data: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    server = await _get_server(server_id, db)
    return await _safe_proxy(server, "POST", "/api/ssh/password", {"user": data.user, "password": data.password})


# === Status ===

@router.get("/server/{server_id}/status")
async def get_ssh_status(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    server = await _get_server(server_id, db)
    return await _safe_proxy(server, "GET", "/api/ssh/status")


# === Bulk Operations ===

@router.post("/bulk/config")
async def bulk_apply_ssh_config(
    request: BulkSSHConfigRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    servers = await _get_servers_by_ids(request.server_ids, db)
    results = await asyncio.gather(
        *[_bulk_apply(s, "POST", "/api/ssh/config", request.config) for s in servers]
    )
    _log_bulk_summary("ssh_config", list(results))
    return {"results": results}


@router.post("/bulk/fail2ban")
async def bulk_apply_fail2ban(
    request: BulkFail2banRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    servers = await _get_servers_by_ids(request.server_ids, db)
    results = await asyncio.gather(
        *[_bulk_apply(s, "POST", "/api/ssh/fail2ban/config", request.config) for s in servers]
    )
    _log_bulk_summary("fail2ban_config", list(results))
    return {"results": results}


@router.post("/bulk/keys")
async def bulk_add_ssh_key(
    request: BulkKeyRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    servers = await _get_servers_by_ids(request.server_ids, db)
    key_data = {"public_key": request.public_key, "user": request.user}
    results = await asyncio.gather(
        *[_bulk_apply(s, "POST", "/api/ssh/keys", key_data) for s in servers]
    )
    _log_bulk_summary("ssh_keys", list(results))
    return {"results": results}


@router.post("/bulk/password")
async def bulk_change_password(
    request: BulkPasswordRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    servers = await _get_servers_by_ids(request.server_ids, db)
    pwd_data = {"user": request.user, "password": request.password}
    results = await asyncio.gather(
        *[_bulk_apply(s, "POST", "/api/ssh/password", pwd_data) for s in servers]
    )
    _log_bulk_summary("ssh_password", list(results))
    return {"results": results}


# === Presets ===

CUSTOM_PRESETS_KEY = "ssh_custom_presets"


async def _load_custom_presets(db: AsyncSession) -> list[dict]:
    result = await db.execute(
        select(PanelSettings).where(PanelSettings.key == CUSTOM_PRESETS_KEY)
    )
    row = result.scalar_one_or_none()
    if row and row.value:
        try:
            return json.loads(row.value)
        except (json.JSONDecodeError, TypeError):
            pass
    return []


async def _save_custom_presets(db: AsyncSession, presets: list[dict]):
    result = await db.execute(
        select(PanelSettings).where(PanelSettings.key == CUSTOM_PRESETS_KEY)
    )
    row = result.scalar_one_or_none()
    if row:
        row.value = json.dumps(presets, ensure_ascii=False)
    else:
        db.add(PanelSettings(key=CUSTOM_PRESETS_KEY, value=json.dumps(presets, ensure_ascii=False)))
    await db.commit()


@router.get("/presets")
async def get_presets(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    custom = await _load_custom_presets(db)
    return {
        "recommended": RECOMMENDED_PRESET,
        "maximum": MAXIMUM_PRESET,
        "custom": custom,
    }


@router.post("/presets/custom")
async def save_custom_preset(
    preset: CustomPresetSave,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    presets = await _load_custom_presets(db)
    existing_idx = next((i for i, p in enumerate(presets) if p["name"] == preset.name), None)
    entry = {"name": preset.name, "ssh": preset.ssh, "fail2ban": preset.fail2ban}
    if existing_idx is not None:
        presets[existing_idx] = entry
    else:
        presets.append(entry)
    await _save_custom_presets(db, presets)
    return {"success": True, "presets": presets}


@router.delete("/presets/custom")
async def delete_custom_preset(
    data: CustomPresetDelete,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    presets = await _load_custom_presets(db)
    presets = [p for p in presets if p["name"] != data.name]
    await _save_custom_presets(db, presets)
    return {"success": True, "presets": presets}
