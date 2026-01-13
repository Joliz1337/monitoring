"""Security middleware: Connection drop for unauthorized/invalid requests

Instead of returning HTTP error responses (which leak information),
this middleware silently closes the connection without any response.
Attackers get no feedback - just a dropped connection.
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
    """Raise to immediately drop connection without response"""
    pass


@dataclass
class IPRecord:
    """Track IP activity for rate limiting and banning"""
    failed_attempts: int = 0
    last_attempt: float = 0
    banned_until: float = 0
    request_times: list = field(default_factory=list)


class SecurityManager:
    """Rate limiting and IP banning with connection drop"""
    
    def __init__(
        self,
        max_failed_attempts: int = 5,
        ban_duration_seconds: int = 900,
        rate_limit_requests: int = 60,
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
        
        # Banned IP - drop connection
        if self.is_banned(ip):
            logger.warning(f"Dropping connection from banned IP: {ip}")
            raise ConnectionDrop()
        
        # Rate limited - drop connection
        if self._check_rate_limit(ip):
            logger.warning(f"Dropping connection - rate limit exceeded: {ip}")
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
    
    async def ban_ip(self, ip: str, duration: int = None):
        """Manually ban IP"""
        async with self._lock:
            self._records[ip].banned_until = time.time() + (duration or self.ban_duration)
            logger.info(f"IP {ip} manually banned")
    
    async def unban_ip(self, ip: str) -> bool:
        """Manually unban IP"""
        async with self._lock:
            if ip in self._records:
                self._records[ip].banned_until = 0
                self._records[ip].failed_attempts = 0
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
            max_failed_attempts=settings.max_failed_attempts,
            ban_duration_seconds=settings.ban_duration_seconds
        )
    return _security


class SecurityMiddleware(BaseHTTPMiddleware):
    """
    Middleware that drops connections instead of returning error responses.
    
    For security-sensitive errors (401, 403, 429), connection is dropped
    without any response - giving attackers zero information.
    """
    
    # Paths that don't require auth (still rate limited)
    PUBLIC_PATHS = {"/health", "/auth/login"}
    
    async def dispatch(self, request: Request, call_next):
        security = get_security_manager()
        
        # Check rate limiting and bans
        try:
            await security.check_request(request)
        except ConnectionDrop:
            # Return empty response and close connection
            return Response(status_code=444, content=b"")
        
        try:
            response = await call_next(request)
            
            # Drop connection on auth/security errors
            if response.status_code in (401, 403):
                ip = security._get_client_ip(request)
                await security.record_auth_failure(ip)
                logger.warning(f"Auth failure from {ip}: {request.url.path}")
                return Response(status_code=444, content=b"")
            
            # Drop connection on rate limit
            if response.status_code == 429:
                return Response(status_code=444, content=b"")
            
            return response
            
        except ConnectionDrop:
            return Response(status_code=444, content=b"")
        except Exception as e:
            # Log unexpected errors but don't expose details
            logger.error(f"Request error: {e}")
            return Response(status_code=444, content=b"")


def drop_connection():
    """Helper to drop connection from anywhere"""
    raise ConnectionDrop()
