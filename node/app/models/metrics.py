"""Pydantic models for metrics API responses"""

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
    is_virtual: bool = False
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


class NetworkTotal(BaseModel):
    rx_bytes: int
    tx_bytes: int
    rx_packets: int
    tx_packets: int
    rx_bytes_per_sec: float = 0.0
    tx_bytes_per_sec: float = 0.0


class NetworkPortCounter(BaseModel):
    port: int
    rx_bytes: int = 0
    tx_bytes: int = 0


class NetworkInfo(BaseModel):
    interfaces: list[NetworkInterface]
    total: NetworkTotal
    # Кумулятивные счётчики цепочек учёта: дельты и историю считает панель
    ports: list[NetworkPortCounter] = Field(default_factory=list)
    ports_available: bool = False
    ports_sampled_at: Optional[float] = None


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
    # Меняется только с перезагрузкой хоста — по нему панель точно отличает
    # ребут от отрицательной дельты счётчика
    boot_id: Optional[str] = None


class CertificateExpiry(BaseModel):
    domain: str
    days_left: int
    expiry_date: str
    expired: bool


class CertificatesInfo(BaseModel):
    count: int
    closest_expiry: Optional[CertificateExpiry] = None


class AntiDdosInfo(BaseModel):
    """Anti-DDoS state and the kernel counters the watchdog decides on.

    The panel already gets a Telegram alert on an off->on transition, but it had
    no history and no "how much is actually being dropped" number — so there was
    no way to tell a real attack from a threshold that is simply set too low.
    """
    mode: str = "off"                      # off | on
    source: str = "none"                   # none | auto | manual
    since: int = 0                         # epoch seconds of the last transition
    watchdog: str = "off"                  # is auto-detection enabled
    conntrack_count: Optional[int] = None
    conntrack_max: Optional[int] = None
    conntrack_fill_pct: Optional[float] = None
    insert_failed_total: Optional[int] = None
    syncookies_sent_total: Optional[int] = None
    softnet_dropped_total: Optional[int] = None
    listen_overflows_total: Optional[int] = None   # полная очередь accept
    listen_drops_total: Optional[int] = None       # шире: включает смену сокетов


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
    antiddos: Optional[AntiDdosInfo] = None
    agent_version: Optional[str] = None
    # Карта прав панели на этой ноде; null — ограничений нет. Поле обязано быть
    # объявлено здесь: то, чего нет в response_model, FastAPI из ответа вырежет.
    capabilities: Optional[dict[str, str]] = None

