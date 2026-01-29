"""IPSet blocklist management router"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from app.services.ipset_manager import get_ipset_manager

router = APIRouter(prefix="/api/ipset", tags=["ipset"])


class IpAddRequest(BaseModel):
    """Request to add IP/CIDR"""
    ip: str = Field(..., description="IP address or CIDR notation")
    permanent: bool = Field(True, description="Add to permanent list (True) or temp list (False)")


class IpRemoveRequest(BaseModel):
    """Request to remove IP/CIDR"""
    ip: str = Field(..., description="IP address or CIDR notation")
    permanent: bool = Field(True, description="Remove from permanent list (True) or temp list (False)")


class BulkIpRequest(BaseModel):
    """Request for bulk IP operations"""
    ips: list[str] = Field(..., description="List of IP addresses or CIDR notations")
    permanent: bool = Field(True, description="Target permanent list (True) or temp list (False)")


class SyncRequest(BaseModel):
    """Request to sync (replace) entire list"""
    ips: list[str] = Field(..., description="List of IP addresses or CIDR notations")
    permanent: bool = Field(True, description="Target permanent list (True) or temp list (False)")


class TimeoutRequest(BaseModel):
    """Request to change temp list timeout"""
    timeout: int = Field(..., ge=1, le=2592000, description="Timeout in seconds (1-2592000)")


@router.get("/status")
async def get_status():
    """Get ipset lists status"""
    manager = get_ipset_manager()
    status = manager.get_status()
    return {
        "permanent_count": status.permanent_count,
        "temp_count": status.temp_count,
        "temp_timeout": status.temp_timeout,
        "iptables_rules_exist": status.iptables_rules_exist
    }


@router.get("/list/{set_type}")
async def list_ips(set_type: str):
    """Get IPs from list
    
    Args:
        set_type: 'permanent' or 'temp'
    """
    if set_type not in ('permanent', 'temp'):
        raise HTTPException(status_code=400, detail="set_type must be 'permanent' or 'temp'")
    
    manager = get_ipset_manager()
    ips = manager.list_ips(permanent=(set_type == 'permanent'))
    
    return {
        "set_type": set_type,
        "count": len(ips),
        "ips": ips
    }


@router.post("/add")
async def add_ip(request: IpAddRequest):
    """Add IP/CIDR to blocklist"""
    manager = get_ipset_manager()
    success, message = manager.add_ip(request.ip, permanent=request.permanent)
    
    if not success:
        raise HTTPException(status_code=400, detail=message)
    
    return {
        "success": True,
        "message": message,
        "ip": request.ip,
        "list": "permanent" if request.permanent else "temp"
    }


@router.post("/bulk-add")
async def bulk_add_ips(request: BulkIpRequest):
    """Add multiple IPs to blocklist"""
    manager = get_ipset_manager()
    success_count, fail_count, errors = manager.bulk_add(request.ips, permanent=request.permanent)
    
    return {
        "success": fail_count == 0,
        "total": len(request.ips),
        "added": success_count,
        "failed": fail_count,
        "errors": errors[:10],  # Limit errors in response
        "list": "permanent" if request.permanent else "temp"
    }


@router.delete("/remove")
async def remove_ip(request: IpRemoveRequest):
    """Remove IP/CIDR from blocklist"""
    manager = get_ipset_manager()
    success, message = manager.remove_ip(request.ip, permanent=request.permanent)
    
    if not success:
        raise HTTPException(status_code=400, detail=message)
    
    return {
        "success": True,
        "message": message,
        "ip": request.ip,
        "list": "permanent" if request.permanent else "temp"
    }


@router.post("/bulk-remove")
async def bulk_remove_ips(request: BulkIpRequest):
    """Remove multiple IPs from blocklist"""
    manager = get_ipset_manager()
    success_count, fail_count, errors = manager.bulk_remove(request.ips, permanent=request.permanent)
    
    return {
        "success": fail_count == 0,
        "total": len(request.ips),
        "removed": success_count,
        "failed": fail_count,
        "errors": errors[:10],
        "list": "permanent" if request.permanent else "temp"
    }


@router.post("/clear/{set_type}")
async def clear_set(set_type: str):
    """Clear all IPs from list
    
    Args:
        set_type: 'permanent' or 'temp'
    """
    if set_type not in ('permanent', 'temp'):
        raise HTTPException(status_code=400, detail="set_type must be 'permanent' or 'temp'")
    
    manager = get_ipset_manager()
    success, message = manager.clear_set(permanent=(set_type == 'permanent'))
    
    if not success:
        raise HTTPException(status_code=500, detail=message)
    
    return {
        "success": True,
        "message": message,
        "set_type": set_type
    }


@router.put("/timeout")
async def set_timeout(request: TimeoutRequest):
    """Change temp list timeout (recreates the list)"""
    manager = get_ipset_manager()
    success, message = manager.set_timeout(request.timeout)
    
    if not success:
        raise HTTPException(status_code=500, detail=message)
    
    return {
        "success": True,
        "message": message,
        "timeout": request.timeout
    }


@router.post("/sync")
async def sync_list(request: SyncRequest):
    """Sync (replace) entire list with new IPs
    
    This is atomic - calculates diff and applies minimal changes.
    """
    manager = get_ipset_manager()
    success, message, result = manager.sync(request.ips, permanent=request.permanent)
    
    if not success:
        raise HTTPException(status_code=500, detail=message)
    
    return {
        "success": True,
        "message": message,
        "list": "permanent" if request.permanent else "temp",
        "total": result['total'],
        "added": result['added'],
        "removed": result['removed'],
        "invalid": result['invalid'][:10] if result['invalid'] else []
    }
