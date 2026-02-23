"""Pydantic models for metrics API responses"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CPUFrequency(BaseModel):
    current: float
    min: float
    max: float


class TemperatureReading(BaseModel):
    label: str
    current: float
    high: Optional[float] = None
    critical: Optional[float] = None


class CPUInfo(BaseModel):
    cores_physical: int
    cores_logical: int
    model: str
    usage_percent: float
    per_cpu_percent: list[float]
    load_avg_1: float
    load_avg_5: float
    load_avg_15: float
    frequency: CPUFrequency
    temperatures: dict[str, list[TemperatureReading]] = Field(default_factory=dict)


class RAMInfo(BaseModel):
    total: int
    used: int
    free: int
    available: int
    percent: float
    buffers: int = 0
    cached: int = 0


class SwapInfo(BaseModel):
    total: int
    used: int
    free: int
    percent: float


class MemoryInfo(BaseModel):
    ram: RAMInfo
    swap: SwapInfo


class DiskPartition(BaseModel):
    device: str
    mountpoint: str
    fstype: str
    total: int
    used: int
    free: int
    percent: float


class DiskIO(BaseModel):
    read_bytes: int
    write_bytes: int
    read_count: int
    write_count: int
    read_time_ms: int
    write_time_ms: int
    read_bytes_per_sec: Optional[float] = None
    write_bytes_per_sec: Optional[float] = None


class DiskInfo(BaseModel):
    partitions: list[DiskPartition]
    io: dict[str, DiskIO]


class NetworkAddress(BaseModel):
    type: str
    address: str
    netmask: Optional[str] = None


class NetworkInterface(BaseModel):
    name: str
    addresses: list[NetworkAddress]
    mac: Optional[str] = None
    mtu: Optional[int] = None
    speed_mbps: Optional[int] = None
    is_up: bool
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_packets: int = 0
    tx_packets: int = 0
    rx_errors: int = 0
    tx_errors: int = 0
    rx_drops: int = 0
    tx_drops: int = 0
    rx_bytes_per_sec: Optional[float] = None
    tx_bytes_per_sec: Optional[float] = None
    rx_peak_per_sec: Optional[float] = None
    tx_peak_per_sec: Optional[float] = None


class NetworkTotal(BaseModel):
    rx_bytes: int
    tx_bytes: int
    rx_packets: int
    tx_packets: int
    rx_bytes_per_sec: float = 0.0
    tx_bytes_per_sec: float = 0.0
    rx_peak_per_sec: float = 0.0
    tx_peak_per_sec: float = 0.0


class NetworkInfo(BaseModel):
    interfaces: list[NetworkInterface]
    total: NetworkTotal


class ProcessInfo(BaseModel):
    pid: int
    name: str
    cpu_percent: float
    memory_percent: float
    status: str


class ProcessesInfo(BaseModel):
    total: int
    running: int
    sleeping: int
    top_by_cpu: list[ProcessInfo]
    top_by_memory: list[ProcessInfo]


class ConnectionStats(BaseModel):
    established: int
    listen: int
    time_wait: int
    other: int


class TCPStats(BaseModel):
    total: int = 0
    established: int = 0
    listen: int = 0
    time_wait: int = 0
    close_wait: int = 0
    syn_sent: int = 0
    syn_recv: int = 0
    fin_wait: int = 0
    other: int = 0


class UDPStats(BaseModel):
    total: int = 0


class ConnectionsDetailed(BaseModel):
    tcp: TCPStats
    udp: UDPStats


class TimezoneInfo(BaseModel):
    name: str
    offset: str
    offset_seconds: int


class SystemInfo(BaseModel):
    hostname: str
    os: str
    kernel: str
    architecture: str
    boot_time: str
    uptime_seconds: int
    uptime_human: str
    open_files: int
    connections: ConnectionStats
    connections_detailed: Optional[ConnectionsDetailed] = None
    server_name: str
    timezone: Optional[TimezoneInfo] = None


class CertificateExpiry(BaseModel):
    domain: str
    days_left: int
    expiry_date: str
    expired: bool


class CertificatesInfo(BaseModel):
    count: int
    closest_expiry: Optional[CertificateExpiry] = None


class AllMetrics(BaseModel):
    timestamp: str
    server_name: str
    timezone: Optional[TimezoneInfo] = None
    cpu: CPUInfo
    memory: MemoryInfo
    disk: DiskInfo
    network: NetworkInfo
    processes: ProcessesInfo
    system: SystemInfo
    certificates: Optional[CertificatesInfo] = None


class MetricsHistoryPoint(BaseModel):
    timestamp: datetime
    cpu_usage: Optional[float] = None
    load_avg_1: Optional[float] = None
    memory_used: Optional[int] = None
    memory_available: Optional[int] = None
    swap_used: Optional[int] = None
    disk_read_bytes_per_sec: Optional[float] = None
    disk_write_bytes_per_sec: Optional[float] = None
    net_rx_bytes_per_sec: Optional[float] = None
    net_tx_bytes_per_sec: Optional[float] = None
    net_rx_peak_per_sec: Optional[float] = None
    net_tx_peak_per_sec: Optional[float] = None
    process_count: Optional[int] = None


class MetricsHistoryResponse(BaseModel):
    from_time: datetime
    to_time: datetime
    count: int
    data: list[MetricsHistoryPoint]
