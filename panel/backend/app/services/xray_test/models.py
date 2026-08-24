"""Единая модель прокси-конфигурации и результата проверки.

Ссылка, вставленный JSON и подписка сводятся к одному `ProxyEndpoint` — дальше
код не знает, откуда конфиг пришёл. Модель заморожена: мульти-SNI порождает
копии через dataclasses.replace, и общий объект не должен меняться под ногами у
параллельных ячеек.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Protocol(str, Enum):
    VLESS = "vless"
    VMESS = "vmess"
    TROJAN = "trojan"
    SHADOWSOCKS = "shadowsocks"
    SOCKS = "socks"
    HTTP = "http"
    HYSTERIA2 = "hysteria2"
    TUIC = "tuic"
    ANYTLS = "anytls"
    SHADOWTLS = "shadowtls"


class Transport(str, Enum):
    TCP = "tcp"
    WS = "ws"
    GRPC = "grpc"
    HTTPUPGRADE = "httpupgrade"
    XHTTP = "xhttp"
    MKCP = "kcp"
    H2 = "http"


class Security(str, Enum):
    NONE = "none"
    TLS = "tls"
    REALITY = "reality"


class Core(str, Enum):
    XRAY = "xray"
    SINGBOX = "sing-box"


class Verdict(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    FAIL = "fail"


class FailReason(str, Enum):
    """Машинные коды отказа: текст подставляет фронт через i18n."""

    DNS_FAIL = "DNS_FAIL"
    TCP_REFUSED = "TCP_REFUSED"
    TCP_TIMEOUT = "TCP_TIMEOUT"
    CORE_START_FAILED = "CORE_START_FAILED"
    CORE_CRASHED = "CORE_CRASHED"
    PROXY_HANDSHAKE_FAILED = "PROXY_HANDSHAKE_FAILED"
    HTTP_TIMEOUT = "HTTP_TIMEOUT"
    HTTP_BAD_STATUS = "HTTP_BAD_STATUS"
    TLS_REJECTED = "TLS_REJECTED"
    UNSUPPORTED = "UNSUPPORTED"
    # Не отказы: трафик прошёл, но результат с оговоркой
    SLOW_RTT = "SLOW_RTT"
    EXIT_IP_UNKNOWN = "EXIT_IP_UNKNOWN"
    CANCELLED = "CANCELLED"
    NODE_ERROR = "NODE_ERROR"
    INTERNAL = "INTERNAL"


# QUIC-протоколы живут поверх UDP: TCP-проба по их порту не значит ничего и
# показала бы «сервер мёртв» на полностью рабочем сервере.
UDP_PROTOCOLS = frozenset({Protocol.HYSTERIA2, Protocol.TUIC})

# Транспорты, у которых Host-заголовок обычно совпадает с SNI: при подмене SNI
# без подмены Host сервер ответит 404, и ячейка соврёт про блокировку.
HOST_BOUND_TRANSPORTS = frozenset({
    Transport.WS, Transport.HTTPUPGRADE, Transport.XHTTP, Transport.GRPC, Transport.H2,
})


@dataclass(frozen=True)
class TlsSettings:
    security: Security = Security.NONE
    sni: Optional[str] = None
    alpn: tuple[str, ...] = ()
    fingerprint: Optional[str] = None
    allow_insecure: bool = False
    reality_public_key: Optional[str] = None
    reality_short_id: Optional[str] = None
    reality_spider_x: Optional[str] = None


@dataclass(frozen=True)
class TransportSettings:
    kind: Transport = Transport.TCP
    path: Optional[str] = None
    host: Optional[str] = None
    service_name: Optional[str] = None
    mode: Optional[str] = None
    header_type: Optional[str] = None
    seed: Optional[str] = None
    authority: Optional[str] = None


@dataclass(frozen=True)
class ProxyEndpoint:
    protocol: Protocol
    address: str
    port: int
    remark: str = ""
    uuid: Optional[str] = None
    password: Optional[str] = None
    method: Optional[str] = None
    alter_id: int = 0
    flow: Optional[str] = None
    encryption: Optional[str] = None
    obfs: Optional[tuple[str, str]] = None
    tls: TlsSettings = field(default_factory=TlsSettings)
    transport: TransportSettings = field(default_factory=TransportSettings)
    extra: tuple[tuple[str, str], ...] = ()

    @property
    def effective_sni(self) -> str:
        return self.tls.sni or self.transport.host or self.address

    @property
    def is_udp_protocol(self) -> bool:
        return self.protocol in UDP_PROTOCOLS


@dataclass
class ProbeTimings:
    dns_ms: Optional[float] = None
    tcp_min_ms: Optional[float] = None
    tcp_avg_ms: Optional[float] = None
    tcp_jitter_ms: Optional[float] = None
    handshake_ms: Optional[float] = None
    rtt_ms: Optional[float] = None
    speed_mbps: Optional[float] = None


@dataclass
class TlsInfo:
    reachable: bool = False
    issuer: Optional[str] = None
    subject: Optional[str] = None
    not_after: Optional[str] = None
    version: Optional[str] = None
    alpn: Optional[str] = None
    self_signed: bool = False
    error: Optional[str] = None


@dataclass
class CellResult:
    """Результат одной ячейки матрицы «конфиг × SNI × локация»."""

    index: int
    remark: str
    protocol: str
    address: str
    port: int
    sni: Optional[str]
    transport: str
    security: str
    core: Optional[str] = None
    verdict: Verdict = Verdict.FAIL
    reason: Optional[FailReason] = None
    detail: str = ""
    hint: Optional[str] = None
    resolved_ip: Optional[str] = None
    exit_ip: Optional[str] = None
    exit_country: Optional[str] = None
    exit_asn: Optional[str] = None
    http_status: Optional[int] = None
    timings: ProbeTimings = field(default_factory=ProbeTimings)
    tls_info: Optional[TlsInfo] = None
    link: Optional[str] = None
    location: str = "panel"
    location_name: str = ""

    def as_event(self) -> dict:
        """Плоский JSON для NDJSON-стрима и таблицы на фронте."""
        return {
            "index": self.index,
            "remark": self.remark,
            "location": self.location,
            "location_name": self.location_name,
            "protocol": self.protocol,
            "address": self.address,
            "port": self.port,
            "sni": self.sni,
            "transport": self.transport,
            "security": self.security,
            "core": self.core,
            "verdict": self.verdict.value,
            "reason": self.reason.value if self.reason else None,
            "detail": self.detail,
            "hint": self.hint,
            "resolved_ip": self.resolved_ip,
            "exit_ip": self.exit_ip,
            "exit_country": self.exit_country,
            "exit_asn": self.exit_asn,
            "http_status": self.http_status,
            "dns_ms": self.timings.dns_ms,
            "tcp_min_ms": self.timings.tcp_min_ms,
            "tcp_avg_ms": self.timings.tcp_avg_ms,
            "tcp_jitter_ms": self.timings.tcp_jitter_ms,
            "handshake_ms": self.timings.handshake_ms,
            "rtt_ms": self.timings.rtt_ms,
            "speed_mbps": self.timings.speed_mbps,
            "tls": self.tls_info.__dict__ if self.tls_info else None,
        }


@dataclass(frozen=True)
class TestCell:
    """Ячейка матрицы: конфиг с уже применённым SNI, привязанный к месту запуска."""

    index: int
    endpoint: ProxyEndpoint
    sni_label: Optional[str]
    link: Optional[str] = None
    location: str = "panel"
    location_name: str = ""
