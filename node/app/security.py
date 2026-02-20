"""Security: IP banning, connection drop

Instead of HTTP error responses, connections are silently dropped.
This gives attackers zero information about what went wrong.
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


class ConnectionDrop(Exception):
    """Raise to drop connection without response"""
    pass


@dataclass
class IPRecord:
    """Track IP activity"""
    failed_attempts: int = 0
    last_attempt: float = 0
    banned_until: float = 0


class SecurityManager:
    """IP banning with connection drop"""
    
    def __init__(
        self,
        max_failed_attempts: int = 10,
        ban_duration_seconds: int = 3600,
        cleanup_interval: int = 300
    ):
        self.max_failed_attempts = max_failed_attempts
        self.ban_duration = ban_duration_seconds
        self.cleanup_interval = cleanup_interval
        
        self._records: dict[str, IPRecord] = defaultdict(IPRecord)
        self._lock = asyncio.Lock()
        self._last_cleanup = time.time()
    
    def _get_client_ip(self, request: Request) -> str:
        """Extract real client IP"""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip
        
        if request.client:
            return request.client.host
        
        return "unknown"
    
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
        return record.banned_until > time.time()
    
    async def check_request(self, request: Request) -> str:
        """Check request - raises ConnectionDrop if blocked"""
        await self._cleanup_old_records()
        
        ip = self._get_client_ip(request)
        
        if self.is_banned(ip):
            logger.warning(f"Dropping connection from banned IP: {ip}")
            raise ConnectionDrop()
        
        async with self._lock:
            self._records[ip].last_attempt = time.time()
        
        return ip
    
    async def record_auth_failure(self, ip: str):
        """Record failed auth - may result in ban"""
        async with self._lock:
            record = self._records[ip]
            record.failed_attempts += 1
            record.last_attempt = time.time()
            
            if record.failed_attempts >= self.max_failed_attempts:
                record.banned_until = time.time() + self.ban_duration
                logger.warning(f"IP {ip} banned after {record.failed_attempts} failed attempts")
    
    async def record_auth_success(self, ip: str):
        """Reset failures on success"""
        async with self._lock:
            if ip in self._records:
                self._records[ip].failed_attempts = 0
    
    def get_banned_ips(self) -> list[dict]:
        """Get banned IPs list"""
        now = time.time()
        return [
            {
                "ip": ip,
                "banned_until": rec.banned_until,
                "remaining_seconds": int(rec.banned_until - now),
                "failed_attempts": rec.failed_attempts
            }
            for ip, rec in self._records.items()
            if rec.banned_until > now
        ]
    
    async def unban_ip(self, ip: str) -> bool:
        """Manually unban IP"""
        async with self._lock:
            if ip in self._records:
                self._records[ip].banned_until = 0
                self._records[ip].failed_attempts = 0
                logger.info(f"IP {ip} unbanned")
                return True
            return False


# Global instance
_security: Optional[SecurityManager] = None


def get_security_manager() -> SecurityManager:
    """Get or create security manager"""
    global _security
    if _security is None:
        from app.config import get_settings
        settings = get_settings()
        _security = SecurityManager(
            max_failed_attempts=settings.security_max_failed_attempts,
            ban_duration_seconds=settings.security_ban_duration
        )
    return _security


class SecurityMiddleware(BaseHTTPMiddleware):
    """
    Drop connections on security failures.
    
    No HTTP error responses - just connection drop.
    """
    
    async def dispatch(self, request: Request, call_next):
        security = get_security_manager()
        settings = None
        
        try:
            from app.config import get_settings
            settings = get_settings()
        except Exception:
            pass
        
        api_key = request.headers.get("X-API-Key")
        valid_key = settings and api_key and api_key == settings.api_key
        
        if not valid_key:
            try:
                await security.check_request(request)
            except ConnectionDrop:
                return Response(status_code=444, content=b"")
        
        try:
            response = await call_next(request)
            
            # Drop connection on auth failures
            if response.status_code in (401, 403):
                ip = security._get_client_ip(request)
                await security.record_auth_failure(ip)
                logger.warning(f"Auth failure from {ip}: {request.url.path}")
                return Response(status_code=444, content=b"")
            
            return response
            
        except ConnectionDrop:
            return Response(status_code=444, content=b"")
        except Exception as e:
            logger.error(f"Request error: {e}")
            return Response(status_code=444, content=b"")


def drop_connection():
    """Helper to drop connection"""
    raise ConnectionDrop()
