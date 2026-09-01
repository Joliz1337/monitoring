import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db, async_session_maker
from app.models import Server, PanelSettings
from app.services.bulk_stream import stream_ndjson
from app.services.node_capabilities import NodeCapabilityError, denial_headers
from app.services.ssh_manager import proxy_to_node, RECOMMENDED_PRESET, MAXIMUM_PRESET

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ssh-security", tags=["ssh-security"])


class BulkApplyRequest(BaseModel):
    server_ids: list[int]
    ssh: dict | None = None
    fail2ban: dict | None = None


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


class BulkStatusRequest(BaseModel):
    server_ids: list[int]


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
    except NodeCapabilityError as e:
        # 409, а не 503: раздел закрыт владельцем ноды, связь при этом в порядке
        raise HTTPException(
            status_code=409,
            detail=str(e),
            headers=denial_headers(e.capability, e.write),
        )
    except LookupError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except (ConnectionError, TimeoutError, RuntimeError) as e:
        raise HTTPException(status_code=503, detail=str(e))


def _valid_port(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value < 65536 else None


async def _cache_sshd_port(server_id: int, port: int | None) -> None:
    """Кэш фактического порта sshd — по нему firewall-профили проверяют allow-правило SSH.

    Best-effort: своя сессия (вызывается и из конкурентных воркеров стрима),
    ошибка БД не должна ломать ответ ноды.
    """
    if port is None:
        return
    try:
        async with async_session_maker() as db:
            await db.execute(
                update(Server).where(Server.id == server_id).values(sshd_port=port)
            )
            await db.commit()
    except Exception as e:
        logger.warning("sshd_port_cache_failed", extra={"server_id": server_id, "error": str(e)})


# Пути с долгой установкой (apt-get install fail2ban может занять 120с)
_LONG_TIMEOUT_PATHS = frozenset({"/api/ssh/fail2ban/config", "/api/ssh/password"})

# Шаг применения: (имя шага, HTTP-метод, путь на ноде, тело запроса)
Step = tuple[str, str, str, dict | None]


async def _apply_steps(server, steps: list[Step]) -> dict:
    """Выполнить шаги одной ноды последовательно, собрать результат по каждому шагу."""
    done: list[dict] = []
    for name, method, path, data in steps:
        long = path in _LONG_TIMEOUT_PATHS
        try:
            res = await proxy_to_node(
                server, method, path, data,
                timeout=120.0 if long else 30.0,
                use_apply_client=long,
            )
            entry = {"step": name, "success": True}
            if res.get("message"):
                entry["message"] = res["message"]
            if res.get("warnings"):
                entry["warnings"] = res["warnings"]
            done.append(entry)
        except (ConnectionError, TimeoutError, LookupError, RuntimeError,
                NodeCapabilityError) as e:
            done.append({"step": name, "success": False, "error": str(e)})
            # Недоступность/таймаут/устаревшая нода/закрытый раздел — остальные шаги обречены
            if isinstance(e, (ConnectionError, TimeoutError, LookupError, NodeCapabilityError)):
                break
    return {
        "server_id": server.id,
        "server_name": server.name,
        "success": all(s["success"] for s in done),
        "steps": done,
    }


async def _fetch_ssh_status(server) -> dict:
    """Собрать SSH-статус одной ноды для обзор-таблицы."""
    try:
        status = await proxy_to_node(server, "GET", "/api/ssh/status", timeout=15.0)
        if isinstance(status, dict):
            await _cache_sshd_port(server.id, _valid_port(status.get("sshd_port")))
        return {"server_id": server.id, "server_name": server.name, "reachable": True, "status": status}
    except NodeCapabilityError as e:
        return {"server_id": server.id, "server_name": server.name, "reachable": False,
                "denied": True, "error": str(e)}
    except LookupError as e:
        return {"server_id": server.id, "server_name": server.name, "reachable": False,
                "outdated": True, "error": str(e)}
    except (ConnectionError, TimeoutError, RuntimeError) as e:
        return {"server_id": server.id, "server_name": server.name, "reachable": False, "error": str(e)}


async def _get_servers_by_ids(server_ids: list[int], db: AsyncSession) -> list[Server]:
    result = await db.execute(select(Server).where(Server.id.in_(server_ids)))
    servers = result.scalars().all()
    if not servers:
        raise HTTPException(status_code=404, detail="No servers found")
    return list(servers)


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
    result = await _safe_proxy(server, "POST", "/api/ssh/config", config)
    if isinstance(result, dict) and result.get("success", True):
        await _cache_sshd_port(server.id, _valid_port(config.get("port")))
    return result


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
    status = await _safe_proxy(server, "GET", "/api/ssh/status")
    if isinstance(status, dict):
        await _cache_sshd_port(server.id, _valid_port(status.get("sshd_port")))
    return status


# === Bulk Operations (NDJSON-стриминг) ===

@router.post("/bulk/apply")
async def bulk_apply(
    request: BulkApplyRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    """Применить SSH-конфиг и/или fail2ban к набору серверов. Стримит прогресс по каждому."""
    if not request.ssh and not request.fail2ban:
        raise HTTPException(status_code=400, detail="Nothing to apply: ssh and fail2ban are empty")
    servers = await _get_servers_by_ids(request.server_ids, db)

    def build_steps(server) -> list[Step]:
        steps: list[Step] = []
        if request.ssh:
            steps.append(("ssh_config", "POST", "/api/ssh/config", request.ssh))
        if request.fail2ban:
            steps.append(("fail2ban", "POST", "/api/ssh/fail2ban/config", request.fail2ban))
        return steps

    new_ssh_port = _valid_port((request.ssh or {}).get("port"))

    async def worker(server):
        result = await _apply_steps(server, build_steps(server))
        ssh_step_ok = any(
            s["step"] == "ssh_config" and s["success"] for s in result["steps"]
        )
        if ssh_step_ok:
            await _cache_sshd_port(server.id, new_ssh_port)
        return result

    return stream_ndjson(servers, worker, log_action="ssh_apply")


@router.post("/bulk/keys")
async def bulk_add_ssh_key(
    request: BulkKeyRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    servers = await _get_servers_by_ids(request.server_ids, db)
    key_data = {"public_key": request.public_key, "user": request.user}

    async def worker(server):
        return await _apply_steps(server, [("key", "POST", "/api/ssh/keys", key_data)])

    return stream_ndjson(servers, worker, log_action="ssh_keys")


@router.post("/bulk/password")
async def bulk_change_password(
    request: BulkPasswordRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    servers = await _get_servers_by_ids(request.server_ids, db)
    pwd_data = {"user": request.user, "password": request.password}

    async def worker(server):
        return await _apply_steps(server, [("password", "POST", "/api/ssh/password", pwd_data)])

    return stream_ndjson(servers, worker, log_action="ssh_password")


@router.post("/bulk/status")
async def bulk_status(
    request: BulkStatusRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    """Собрать SSH-статус набора серверов для обзор-таблицы. Стримит результат по каждому."""
    servers = await _get_servers_by_ids(request.server_ids, db)
    return stream_ndjson(servers, _fetch_ssh_status)


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
