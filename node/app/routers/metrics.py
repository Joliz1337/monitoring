"""Metrics API endpoints - Simple API returning current values only"""

from fastapi import APIRouter, Query

from app.models.metrics import (
    AllMetrics,
    CPUInfo,
    DiskInfo,
    MemoryInfo,
    NetworkInfo,
    ProcessesInfo,
    SystemInfo,
)
from app.services.metrics_collector import get_collector

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get("", response_model=AllMetrics)
async def get_all_metrics():
    """Get all current system metrics"""
    collector = get_collector()
    return collector.get_all_metrics()


@router.get("/cpu", response_model=CPUInfo)
async def get_cpu_metrics():
    """Get CPU information and usage"""
    collector = get_collector()
    return collector.get_cpu_info()


@router.get("/memory", response_model=MemoryInfo)
async def get_memory_metrics():
    """Get RAM and swap information"""
    collector = get_collector()
    return collector.get_memory_info()


@router.get("/disk", response_model=DiskInfo)
async def get_disk_metrics():
    """Get disk partitions, usage and I/O statistics"""
    collector = get_collector()
    return collector.get_disk_info()


@router.get("/network", response_model=NetworkInfo)
async def get_network_metrics():
    """Get network interfaces and traffic statistics"""
    collector = get_collector()
    return collector.get_network_info()


@router.get("/processes", response_model=ProcessesInfo)
async def get_processes_metrics(top_n: int = Query(10, ge=1, le=100)):
    """Get process statistics and top processes by CPU/memory"""
    collector = get_collector()
    return collector.get_processes_info(top_n=top_n)


@router.get("/system", response_model=SystemInfo)
async def get_system_metrics():
    """Get general system information"""
    collector = get_collector()
    return collector.get_system_info()


