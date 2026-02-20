"""
Remnawave API Client for panel integration.

Provides methods to fetch users from Remnawave Panel for caching.
Based on reference implementation from vpn_v2/services/remnawave_api.py
"""

import asyncio
import logging
import ssl
from typing import Optional
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)


class RemnawaveAPIError(Exception):
    """Base exception for Remnawave API errors."""
    def __init__(self, message: str, status_code: int = None):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class RemnawaveAPI:
    """API client for Remnawave Panel."""
    
    def __init__(self, api_url: str, api_token: str, cookie_secret: Optional[str] = None):
        """
        Initialize Remnawave API client.
        
        Args:
            api_url: Base URL of Remnawave Panel API
            api_token: API token for authentication
            cookie_secret: Optional cookie in format "name:value" for Nginx auth
        """
        self.base_url = api_url.rstrip('/') if api_url else ""
        self.token = api_token or ""
        self.cookie_secret = cookie_secret
        self.timeout = aiohttp.ClientTimeout(total=30, connect=10)
        self._session: Optional[aiohttp.ClientSession] = None
        self._cookies: Optional[dict] = None
        
        # Parse cookie secret
        if self.cookie_secret and ':' in self.cookie_secret:
            key_name, key_value = self.cookie_secret.split(':', 1)
            self._cookies = {key_name: key_value}
    
    def _prepare_headers(self) -> dict:
        """Prepare headers for authorization."""
        return {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-Forwarded-Proto': 'https',
            'X-Forwarded-For': '127.0.0.1',
            'X-Real-IP': '127.0.0.1',
            'X-Api-Key': self.token,
            'Authorization': f'Bearer {self.token}'
        }
    
    async def _create_session(self) -> aiohttp.ClientSession:
        """Create new session with configured connector."""
        # SSL context - disable verification for internal connections
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        ssl_context.set_alpn_protocols(['http/1.1'])
        
        connector = aiohttp.TCPConnector(
            limit=20,
            limit_per_host=10,
            enable_cleanup_closed=True,
            force_close=True,
            ssl=ssl_context
        )
        
        session_kwargs = {
            'connector': connector,
            'timeout': self.timeout,
            'headers': self._prepare_headers()
        }
        
        if self._cookies:
            session_kwargs['cookies'] = self._cookies
        
        self._session = aiohttp.ClientSession(**session_kwargs)
        return self._session
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create session."""
        if self._session is None or self._session.closed:
            return await self._create_session()
        return self._session
    
    async def close(self):
        """Close session."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
    
    async def check_connection(self) -> dict:
        """
        Check connection to Remnawave API.
        
        Returns:
            dict with connection status
        """
        result = {
            "url": self.base_url,
            "api_reachable": False,
            "auth_valid": False,
            "error": None
        }
        
        if not self.base_url or not self.token:
            result["error"] = "API URL or token not configured"
            return result
        
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.base_url}/api/system/stats",
                timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                result["api_reachable"] = True
                
                if response.status == 200:
                    result["auth_valid"] = True
                elif response.status == 401:
                    result["error"] = "Invalid API token (401 Unauthorized)"
                elif response.status == 403:
                    result["error"] = "Access forbidden (403)"
                else:
                    response_text = await response.text()
                    result["error"] = f"HTTP {response.status}: {response_text[:200]}"
                    
        except aiohttp.ClientSSLError as e:
            result["error"] = f"SSL error: {e}"
        except aiohttp.ClientConnectorError as e:
            result["error"] = f"Connection error: {e}"
        except asyncio.TimeoutError:
            result["error"] = "Connection timeout"
        except Exception as e:
            result["error"] = f"Unexpected error: {type(e).__name__}: {e}"
        
        return result
    
    async def _request(self, method: str, endpoint: str, params: dict = None) -> dict:
        """Execute API request."""
        if not self.base_url:
            raise RemnawaveAPIError("API URL not configured")
        
        url = f"{self.base_url}{endpoint}"
        
        try:
            session = await self._get_session()
            async with session.request(method, url, params=params) as response:
                response_text = await response.text()
                
                try:
                    response_data = await response.json() if response_text else {}
                except Exception:
                    response_data = {'raw_response': response_text}
                
                if response.status >= 400:
                    error_msg = response_data.get('message', f'HTTP {response.status}')
                    raise RemnawaveAPIError(error_msg, status_code=response.status)
                
                return response_data
                
        except aiohttp.ClientError as e:
            raise RemnawaveAPIError(f"Request failed: {e}")
        except asyncio.TimeoutError:
            raise RemnawaveAPIError("Request timeout")
    
    async def get_all_users(self, start: int = 0, size: int = 200) -> dict:
        """
        Get users with pagination.
        
        Args:
            start: Offset
            size: Page size
            
        Returns:
            dict with 'users' list and 'total' count
        """
        params = {"start": start, "size": size}
        result = await self._request("GET", "/api/users", params=params)
        return result.get("response", {})
    
    async def get_all_users_paginated(self, size: int = 200, concurrency: int = 5) -> list[dict]:
        """
        Get ALL users through parallel paginated requests.
        
        First request fetches page 0 to learn `total`, then remaining
        pages are fetched concurrently (up to `concurrency` at a time).
        
        Args:
            size: Page size per request (default 200)
            concurrency: Max parallel requests
            
        Returns:
            List of all users
        """
        first_page = await self.get_all_users(start=0, size=size)
        users = first_page.get('users', [])
        total = first_page.get('total', len(users))
        
        all_users = list(users)
        
        if total <= size:
            logger.debug(f"Fetched {len(all_users)}/{total} users from Remnawave (single page)")
            return all_users
        
        remaining_offsets = list(range(size, total, size))
        sem = asyncio.Semaphore(concurrency)
        
        async def _fetch_page(offset: int) -> list[dict]:
            async with sem:
                result = await self.get_all_users(start=offset, size=size)
                return result.get('users', [])
        
        tasks = [_fetch_page(offset) for offset in remaining_offsets]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results:
            if isinstance(result, list):
                all_users.extend(result)
            elif isinstance(result, Exception):
                raise RemnawaveAPIError(f"Page fetch failed: {result}")
        
        if len(all_users) < total:
            logger.warning(f"Fetched {len(all_users)}/{total} users (incomplete)")
        else:
            logger.debug(f"Fetched {len(all_users)}/{total} users from Remnawave")
        
        return all_users
    
    async def get_user_by_id(self, user_id: int) -> Optional[dict]:
        """
        Get user by ID (email field in logs).
        
        Note: Remnawave uses 'id' as internal integer ID.
        The 'email' field in xray logs corresponds to this ID.
        """
        try:
            result = await self._request("GET", f"/api/users/by-id/{user_id}")
            return result.get("response", {})
        except RemnawaveAPIError as e:
            if e.status_code == 404:
                return None
            raise
    
    async def get_user_by_uuid(self, uuid: str) -> Optional[dict]:
        """
        Get user by UUID.
        
        Returns full user info including traffic data.
        """
        try:
            result = await self._request("GET", f"/api/users/{uuid}")
            return result.get("response", {})
        except RemnawaveAPIError as e:
            if e.status_code == 404:
                return None
            raise
    
    async def get_user_subscription_history(self, uuid: str) -> Optional[dict]:
        """
        Get user subscription request history (recent 24 records).
        
        Contains IP addresses from which subscription was accessed.
        """
        try:
            result = await self._request("GET", f"/api/users/{uuid}/subscription-request-history")
            return result.get("response", {})
        except RemnawaveAPIError as e:
            if e.status_code == 404:
                return None
            raise
    
    async def get_user_bandwidth_stats(
        self, 
        uuid: str, 
        start_date: str, 
        end_date: str, 
        top_nodes_limit: int = 10
    ) -> Optional[dict]:
        """
        Get user bandwidth statistics for date range.
        
        Args:
            uuid: User UUID
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            top_nodes_limit: Limit of top nodes to return
            
        Returns:
            dict with bandwidth statistics or None if user not found
        """
        try:
            params = {
                "start": start_date,
                "end": end_date,
                "topNodesLimit": top_nodes_limit
            }
            result = await self._request("GET", f"/api/bandwidth-stats/users/{uuid}", params=params)
            return result.get("response", {})
        except RemnawaveAPIError as e:
            if e.status_code == 404:
                return None
            raise
    
    async def get_user_accessible_nodes(self, uuid: str) -> Optional[dict]:
        """
        Get nodes accessible by user.
        """
        try:
            result = await self._request("GET", f"/api/users/{uuid}/accessible-nodes")
            return result.get("response", {})
        except RemnawaveAPIError as e:
            if e.status_code == 404:
                return None
            raise
    
    async def get_user_hwid_devices(self, uuid: str) -> Optional[dict]:
        """
        Get HWID devices for user.
        """
        try:
            result = await self._request("GET", f"/api/hwid/devices/{uuid}")
            return result.get("response", {})
        except RemnawaveAPIError as e:
            if e.status_code == 404:
                return None
            raise
    
    async def get_all_hwid_devices(self, start: int = 0, size: int = 100) -> dict:
        """
        Get all HWID devices with pagination.
        
        Args:
            start: Offset
            size: Page size
            
        Returns:
            dict with 'devices' list and pagination info
        """
        params = {"start": start, "size": size}
        result = await self._request("GET", "/api/hwid/devices", params=params)
        return result.get("response", {})
    
    async def get_all_hwid_devices_paginated(self, size: int = 100) -> list[dict]:
        """
        Get ALL HWID devices through pagination.
        
        Args:
            size: Page size per request
            
        Returns:
            List of all HWID devices
        """
        all_devices = []
        start = 0
        
        while True:
            result = await self.get_all_hwid_devices(start=start, size=size)
            devices = result.get('devices', [])
            all_devices.extend(devices)
            
            if len(devices) < size:
                break
            start += size
        
        logger.debug(f"Fetched {len(all_devices)} HWID devices from Remnawave")
        return all_devices


# Singleton for settings-based instance
_api_instance: Optional[RemnawaveAPI] = None


def get_remnawave_api(api_url: str, api_token: str, cookie_secret: Optional[str] = None) -> RemnawaveAPI:
    """
    Get Remnawave API instance.
    
    Note: Creates new instance each time to support settings changes.
    Caller should manage connection lifecycle.
    """
    return RemnawaveAPI(api_url, api_token, cookie_secret)
