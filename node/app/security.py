"""Security: Rate limiting, IP banning, connection drop

Instead of HTTP error responses, connections are silently dropped.
This gives attackers zero information about what went wrong.
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
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
    request_times: list = field(default_factory=list)


class SecurityManager:
    """Rate limiting and IP banning with connection drop"""
    
    def __init__(
        self,
        max_failed_attempts: int = 10,
        ban_duration_seconds: int = 3600,
        rate_limit_requests: int = 100,
        rate_limit_window: int = 60,
        cleanup_interval: int = 300
    ):
        self.max_failed_attempts = max_failed_attempts
        self.ban_duration = ban_duration_seconds
        self.rate_limit_requests = rate_limit_requests
        self.rate_limit_window = rate_limit_window
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
    
    def _check_rate_limit(self, ip: str) -> bool:
        """Check if rate limited"""
        record = self._records[ip]
        now = time.time()
        
        window_start = now - self.rate_limit_window
        record.request_times = [t for t in record.request_times if t > window_start]
        
        return len(record.request_times) >= self.rate_limit_requests
    
    async def check_request(self, request: Request) -> str:
        """Check request - raises ConnectionDrop if blocked"""
        await self._cleanup_old_records()
        
        ip = self._get_client_ip(request)
        
        # Banned - drop connection
        if self.is_banned(ip):
            logger.warning(f"Dropping connection from banned IP: {ip}")
            raise ConnectionDrop()
        
        # Rate limited - drop connection
        if self._check_rate_limit(ip):
            logger.warning(f"Dropping connection - rate limit: {ip}")
            raise ConnectionDrop()
        
        # Record request
        async with self._lock:
            self._records[ip].request_times.append(time.time())
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
            ban_duration_seconds=settings.security_ban_duration,
            rate_limit_requests=settings.security_rate_limit_requests,
            rate_limit_window=settings.security_rate_limit_window
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
        
        # Check API key - valid key bypasses rate limiting
        api_key = request.headers.get("X-API-Key")
        valid_key = settings and api_key and api_key == settings.api_key
        
        if not valid_key:
            # Check rate limiting and bans
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
            
            # Drop on rate limit
            if response.status_code == 429:
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
