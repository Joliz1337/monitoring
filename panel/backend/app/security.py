"""Security middleware: Connection drop for unauthorized/invalid requests

Instead of returning HTTP error responses (which leak information),
this middleware silently closes the connection without any response.
Attackers get no feedback - just a dropped connection.
"""

import asyncio
import ipaddress
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


# Сети, из которых к бэкенду может прийти собственный nginx панели, — список
# перечислен явно, а не через `is_private`: тот считает своими ещё и CGNAT
# (100.64.0.0/10), и документационные диапазоны, то есть адреса настоящих клиентов.
# Должен совпадать с --forwarded-allow-ips в backend/Dockerfile.
TRUSTED_PROXY_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "::1/128", "fc00::/7", "fe80::/10",
    )
)


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return None
    if addr.version == 6 and addr.ipv4_mapped:
        return addr.ipv4_mapped
    return addr


def _is_internal_peer(host: str) -> bool:
    addr = _parse_ip(host)
    if addr is None:
        return False
    return any(addr.version == net.version and addr in net for net in TRUSTED_PROXY_NETWORKS)


def get_client_ip(request: Request) -> str:
    """Реальный IP клиента за nginx панели.

    Прямой пир — контейнер nginx из bridge-сети Docker (172.x), а не 127.0.0.1,
    поэтому доверие включается по признаку внутреннего адреса. Порядок источников
    принципиален: X-Real-IP nginx ставит из `$remote_addr` и клиент его перебить
    не может, а X-Forwarded-For приходит списком, где первый элемент — то, что
    прислал сам клиент. Из XFF поэтому берётся последний элемент — его дописал
    наш nginx. Схлопывание всех клиентов в один адрес nginx означало бы, что пять
    неверных паролей от кого угодно банят вход всем сразу.
    """
    direct = request.client.host if request.client else "unknown"
    if not _is_internal_peer(direct):
        return direct

    forwarded = request.headers.get("X-Forwarded-For", "")
    candidates = (
        request.headers.get("X-Real-IP", ""),
        forwarded.rsplit(",", 1)[-1],
    )
    for candidate in candidates:
        value = candidate.strip()
        if _parse_ip(value):
            return value

    return direct


class ConnectionDrop(Exception):
    """Raise to immediately drop connection without response"""
    pass


@dataclass
class IPRecord:
    """Track IP activity for banning on failed logins"""
    failed_attempts: int = 0
    last_attempt: float = 0
    banned_until: float = 0


class SecurityManager:
    """IP banning for auth failures with connection drop."""
    
    def __init__(
        self,
        max_failed_attempts: int = 5,
        ban_duration_seconds: int = 900,
        cleanup_interval: int = 300
    ):
        self.max_failed_attempts = max_failed_attempts
        self.ban_duration = ban_duration_seconds
        self.cleanup_interval = cleanup_interval
        
        self._records: dict[str, IPRecord] = defaultdict(IPRecord)
        self._lock = asyncio.Lock()
        self._last_cleanup = time.time()
    
    async def _cleanup_old_records(self):
        """Remove expired records"""
        now = time.time()
        
        if now - self._last_cleanup < self.cleanup_interval:
            return
        
        async with self._lock:
            self._last_cleanup = now
            expired = [
                ip for ip, rec in self._records.items()
                if rec.banned_until < now and now - rec.last_attempt > self.ban_duration
            ]
            for ip in expired:
                del self._records[ip]
    
    def is_banned(self, ip: str) -> bool:
        """Check if IP is banned"""
        record = self._records.get(ip)
        if not record:
            return False
        is_banned = record.banned_until > time.time()
        if is_banned:
            remaining = int(record.banned_until - time.time())
            logger.debug(f"IP {ip} is banned, {remaining}s remaining")
        return is_banned
    
    async def check_request(self, request: Request) -> str:
        """Check request - raises ConnectionDrop if IP is banned."""
        await self._cleanup_old_records()
        
        ip = get_client_ip(request)
        
        # Banned IP - drop connection
        if self.is_banned(ip):
            logger.warning(f"Dropping connection from banned IP: {ip}")
            raise ConnectionDrop()
        
        return ip
    
    async def record_auth_failure(self, ip: str):
        """Record failed auth - may result in ban"""
        async with self._lock:
            record = self._records[ip]
            record.failed_attempts += 1
            record.last_attempt = time.time()
            
            logger.warning(f"Auth failure from {ip}: attempt {record.failed_attempts}/{self.max_failed_attempts}")
            
            if record.failed_attempts >= self.max_failed_attempts:
                record.banned_until = time.time() + self.ban_duration
                logger.warning(f"IP {ip} banned for {self.ban_duration}s after {record.failed_attempts} failed attempts")
    
    async def record_auth_success(self, ip: str):
        """Reset failures and ban on success"""
        async with self._lock:
            if ip in self._records:
                self._records[ip].failed_attempts = 0
                self._records[ip].banned_until = 0
                logger.info(f"Auth success from {ip}, cleared ban and failures")
    
# Global instance
_security: Optional[SecurityManager] = None


def get_security_manager() -> SecurityManager:
    """Get or create security manager"""
    global _security
    if _security is None:
        from app.config import get_settings
        settings = get_settings()
        _security = SecurityManager(
            max_failed_attempts=settings.max_failed_attempts,
            ban_duration_seconds=settings.ban_duration_seconds
        )
    return _security


class SecurityMiddleware(BaseHTTPMiddleware):
    """
    Middleware that drops connections for banned IPs and failed login attempts.

    - Drops connections from banned IPs
    - Returns 444 (no response) on ConnectionDrop to give attackers no info
    """

    async def dispatch(self, request: Request, call_next):
        security = get_security_manager()

        try:
            await security.check_request(request)
            return await call_next(request)
        except ConnectionDrop:
            # Return empty response and close connection
            return Response(status_code=444, content=b"")


def drop_connection():
    """Helper to drop connection from anywhere"""
    raise ConnectionDrop()
