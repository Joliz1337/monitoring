"""API Key authentication with connection drop security

All auth failures result in connection drop - no HTTP error responses.
"""

import logging
import secrets

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from app.config import get_settings
from app.security import get_security_manager, drop_connection

logger = logging.getLogger(__name__)

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(
    request: Request,
    api_key: str = Security(API_KEY_HEADER)
) -> str:
    """Verify API key - drops connection on failure"""
    security = get_security_manager()
    settings = get_settings()
    
    client_ip = security._get_client_ip(request)
    
    # No API key - drop connection
    if not api_key:
        await security.record_auth_failure(client_ip)
        logger.warning(f"Missing API key from {client_ip}")
        raise HTTPException(status_code=403)
    
    # Invalid API key - drop connection
    if not secrets.compare_digest(api_key, settings.api_key):
        await security.record_auth_failure(client_ip)
        logger.warning(f"Invalid API key from {client_ip}")
        raise HTTPException(status_code=403)
    
    # Success
    await security.record_auth_success(client_ip)
    return api_key


def get_api_key_dependency():
    """Get API key dependency"""
    return Depends(verify_api_key)
