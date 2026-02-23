"""Security middleware: Connection drop for unauthorized/invalid requests

Instead of returning HTTP error responses (which leak information),
this middleware silently closes the connection without any response.
Attackers get no feedback - just a dropped connection.
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
        is_banned = record.banned_until > time.time()
        if is_banned:
            remaining = int(record.banned_until - time.time())
            logger.debug(f"IP {ip} is banned, {remaining}s remaining")
        return is_banned
    
    async def check_request(self, request: Request) -> str:
        """Check request - raises ConnectionDrop if IP is banned."""
        await self._cleanup_old_records()
        
        ip = self._get_client_ip(request)
        
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
    Middleware that drops connections for banned IPs and failed login attempts.
    
    - Drops connections from banned IPs
    - Records failed login attempts and bans IPs after too many failures
    - Returns 444 (no response) for login failures to give attackers no info
    """
    
    PUBLIC_PATHS = {"/health", "/auth/login"}
    
    async def dispatch(self, request: Request, call_next):
        security = get_security_manager()
        
        try:
            await security.check_request(request)
        except ConnectionDrop:
            # Return empty response and close connection
            return Response(status_code=444, content=b"")
        
        try:
            response = await call_next(request)
            
            # Drop connection on auth failures - only for login endpoint
            # For other endpoints, 401 is normal (expired token) - let frontend redirect to login
            if response.status_code in (401, 403):
                if request.url.path.endswith("/auth/login"):
                    # Only record failure for actual login attempts
                    ip = security._get_client_ip(request)
                    await security.record_auth_failure(ip)
                    logger.warning(f"Auth failure from {ip}: {request.url.path}")
                    return Response(status_code=444, content=b"")
                # For other endpoints, return normal 401 to allow proper redirect
            
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
