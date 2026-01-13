from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from pydantic import BaseModel
import asyncio
import httpx
import logging

from app.database import get_db
from app.models import Server
from app.auth import verify_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bulk", tags=["bulk"])


class BulkServerIds(BaseModel):
    server_ids: list[int]


class BulkHAProxyRuleCreate(BaseModel):
    server_ids: list[int]
    name: str
    rule_type: str = "tcp"
    listen_port: int
    target_ip: str
    target_port: int
    cert_domain: Optional[str] = None
    target_ssl: bool = False


class BulkHAProxyRuleDelete(BaseModel):
    server_ids: list[int]
    listen_port: int
    target_ip: str
    target_port: int


class BulkTrafficPort(BaseModel):
    server_ids: list[int]
    port: int


class BulkFirewallRuleCreate(BaseModel):
    server_ids: list[int]
    port: int
    protocol: str = "any"
    action: str = "allow"
    from_ip: Optional[str] = None
    direction: str = "in"


class BulkFirewallRuleDelete(BaseModel):
    server_ids: list[int]
    port: int


class BulkResult(BaseModel):
    server_id: int
    server_name: str
    success: bool
    message: str


async def get_servers_by_ids(server_ids: list[int], db: AsyncSession) -> list[Server]:
    """Get multiple servers by their IDs."""
    result = await db.execute(select(Server).where(Server.id.in_(server_ids)))
    servers = result.scalars().all()
    return list(servers)


async def proxy_request_safe(
    server: Server,
    endpoint: str,
    method: str = "GET",
    json_data: dict = None,
    params: dict = None,
    timeout: float = 30.0
) -> tuple[bool, dict | str]:
    """Make a proxy request and return (success, result/error)."""
    url = f"{server.url}{endpoint}"
    
    try:
        async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
            headers = {"X-API-Key": server.api_key}
            
            if method == "GET":
                response = await client.get(url, headers=headers, params=params)
            elif method == "POST":
                response = await client.post(url, headers=headers, json=json_data, params=params)
            elif method == "PUT":
                response = await client.put(url, headers=headers, json=json_data)
            elif method == "DELETE":
                response = await client.delete(url, headers=headers, params=params)
            else:
                return False, "Invalid method"
            
            if response.status_code == 200:
                return True, response.json()
            else:
                error_detail = response.json().get("detail", f"Error {response.status_code}")
                return False, error_detail
    except httpx.TimeoutException:
        return False, "Connection timeout"
    except httpx.RequestError as e:
        return False, f"Connection error: {str(e)}"
    except Exception as e:
        return False, str(e)


# ==================== HAProxy Rules ====================

@router.post("/haproxy/rules", response_model=list[BulkResult])
async def bulk_create_haproxy_rule(
    data: BulkHAProxyRuleCreate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Create HAProxy rule on multiple servers."""
    servers = await get_servers_by_ids(data.server_ids, db)
    
    if not servers:
        raise HTTPException(status_code=404)
    
    rule_data = {
        "name": data.name,
        "rule_type": data.rule_type,
        "listen_port": data.listen_port,
        "target_ip": data.target_ip,
        "target_port": data.target_port,
        "cert_domain": data.cert_domain,
        "target_ssl": data.target_ssl,
    }
    
    async def create_rule(server: Server) -> BulkResult:
        success, result = await proxy_request_safe(
            server, "/api/haproxy/rules", method="POST", json_data=rule_data
        )
        return BulkResult(
            server_id=server.id,
            server_name=server.name,
            success=success,
            message="Rule created" if success else str(result)
        )
    
    results = await asyncio.gather(*[create_rule(s) for s in servers])
    return list(results)


@router.delete("/haproxy/rules", response_model=list[BulkResult])
async def bulk_delete_haproxy_rule(
    data: BulkHAProxyRuleDelete,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Delete HAProxy rule by listen_port + target_ip + target_port on multiple servers."""
    servers = await get_servers_by_ids(data.server_ids, db)
    
    if not servers:
        raise HTTPException(status_code=404)
    
    async def delete_rule(server: Server) -> BulkResult:
        # First, get all rules to find the matching one
        success, rules_result = await proxy_request_safe(server, "/api/haproxy/rules")
        
        if not success:
            return BulkResult(
                server_id=server.id,
                server_name=server.name,
                success=False,
                message=f"Failed to get rules: {rules_result}"
            )
        
        # Find matching rule
        rules = rules_result.get("rules", [])
        matching_rule = None
        for rule in rules:
            if (rule.get("listen_port") == data.listen_port and
                rule.get("target_ip") == data.target_ip and
                rule.get("target_port") == data.target_port):
                matching_rule = rule
                break
        
        if not matching_rule:
            return BulkResult(
                server_id=server.id,
                server_name=server.name,
                success=False,
                message=f"Rule not found (port {data.listen_port} -> {data.target_ip}:{data.target_port})"
            )
        
        # Delete the rule
        rule_name = matching_rule.get("name")
        success, result = await proxy_request_safe(
            server, f"/api/haproxy/rules/{rule_name}", method="DELETE"
        )
        
        return BulkResult(
            server_id=server.id,
            server_name=server.name,
            success=success,
            message=f"Rule '{rule_name}' deleted" if success else str(result)
        )
    
    results = await asyncio.gather(*[delete_rule(s) for s in servers])
    return list(results)


# ==================== Traffic Ports ====================

@router.post("/traffic/ports", response_model=list[BulkResult])
async def bulk_add_tracked_port(
    data: BulkTrafficPort,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Add tracked port on multiple servers."""
    servers = await get_servers_by_ids(data.server_ids, db)
    
    if not servers:
        raise HTTPException(status_code=404)
    
    async def add_port(server: Server) -> BulkResult:
        success, result = await proxy_request_safe(
            server, "/api/traffic/ports/add", method="POST", json_data={"port": data.port}
        )
        return BulkResult(
            server_id=server.id,
            server_name=server.name,
            success=success,
            message=f"Port {data.port} added" if success else str(result)
        )
    
    results = await asyncio.gather(*[add_port(s) for s in servers])
    return list(results)


@router.delete("/traffic/ports", response_model=list[BulkResult])
async def bulk_remove_tracked_port(
    data: BulkTrafficPort,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Remove tracked port from multiple servers."""
    servers = await get_servers_by_ids(data.server_ids, db)
    
    if not servers:
        raise HTTPException(status_code=404)
    
    async def remove_port(server: Server) -> BulkResult:
        # First check if port is tracked
        success, tracked_result = await proxy_request_safe(server, "/api/traffic/ports/tracked")
        
        if not success:
            return BulkResult(
                server_id=server.id,
                server_name=server.name,
                success=False,
                message=f"Failed to get tracked ports: {tracked_result}"
            )
        
        tracked_ports = tracked_result.get("tracked_ports", [])
        if data.port not in tracked_ports:
            return BulkResult(
                server_id=server.id,
                server_name=server.name,
                success=False,
                message=f"Port {data.port} is not tracked"
            )
        
        # Remove the port
        success, result = await proxy_request_safe(
            server, "/api/traffic/ports/remove", method="POST", json_data={"port": data.port}
        )
        
        return BulkResult(
            server_id=server.id,
            server_name=server.name,
            success=success,
            message=f"Port {data.port} removed" if success else str(result)
        )
    
    results = await asyncio.gather(*[remove_port(s) for s in servers])
    return list(results)


# ==================== Firewall Rules ====================

@router.post("/firewall/rules", response_model=list[BulkResult])
async def bulk_add_firewall_rule(
    data: BulkFirewallRuleCreate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Add firewall rule on multiple servers."""
    servers = await get_servers_by_ids(data.server_ids, db)
    
    if not servers:
        raise HTTPException(status_code=404)
    
    rule_data = {
        "port": data.port,
        "protocol": data.protocol,
        "action": data.action,
        "from_ip": data.from_ip,
        "direction": data.direction,
    }
    
    async def add_rule(server: Server) -> BulkResult:
        success, result = await proxy_request_safe(
            server, "/api/haproxy/firewall/rule", method="POST", json_data=rule_data
        )
        
        if success:
            # Check response for success field
            if isinstance(result, dict) and result.get("success") is False:
                return BulkResult(
                    server_id=server.id,
                    server_name=server.name,
                    success=False,
                    message=result.get("message", "Failed to add rule")
                )
        
        return BulkResult(
            server_id=server.id,
            server_name=server.name,
            success=success,
            message=f"Firewall rule added (port {data.port})" if success else str(result)
        )
    
    results = await asyncio.gather(*[add_rule(s) for s in servers])
    return list(results)


@router.delete("/firewall/rules", response_model=list[BulkResult])
async def bulk_delete_firewall_rule(
    data: BulkFirewallRuleDelete,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Delete firewall rule by port on multiple servers."""
    servers = await get_servers_by_ids(data.server_ids, db)
    
    if not servers:
        raise HTTPException(status_code=404)
    
    async def delete_rule(server: Server) -> BulkResult:
        # First get firewall rules to check if port exists
        success, rules_result = await proxy_request_safe(server, "/api/haproxy/firewall/rules")
        
        if not success:
            return BulkResult(
                server_id=server.id,
                server_name=server.name,
                success=False,
                message=f"Failed to get firewall rules: {rules_result}"
            )
        
        # Find rules matching the port
        rules = rules_result.get("rules", [])
        matching_rules = [r for r in rules if r.get("port") == data.port and not r.get("ipv6", False)]
        
        if not matching_rules:
            return BulkResult(
                server_id=server.id,
                server_name=server.name,
                success=False,
                message=f"No firewall rule found for port {data.port}"
            )
        
        # Delete all matching rules (there may be multiple for tcp/udp)
        deleted_count = 0
        errors = []
        
        for rule in matching_rules:
            rule_number = rule.get("number")
            if rule_number:
                success, result = await proxy_request_safe(
                    server, f"/api/haproxy/firewall/rule/{rule_number}", method="DELETE"
                )
                if success:
                    deleted_count += 1
                else:
                    errors.append(str(result))
        
        if deleted_count > 0:
            return BulkResult(
                server_id=server.id,
                server_name=server.name,
                success=True,
                message=f"Deleted {deleted_count} rule(s) for port {data.port}"
            )
        else:
            return BulkResult(
                server_id=server.id,
                server_name=server.name,
                success=False,
                message=f"Failed to delete rules: {'; '.join(errors)}"
            )
    
    results = await asyncio.gather(*[delete_rule(s) for s in servers])
    return list(results)
