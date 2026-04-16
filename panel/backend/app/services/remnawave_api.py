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
        self.timeout = aiohttp.ClientTimeout(total=60, connect=15)
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
    
    async def _request(self, method: str, endpoint: str, params: dict = None, retries: int = 3) -> dict:
        if not self.base_url:
            raise RemnawaveAPIError("API URL not configured")

        url = f"{self.base_url}{endpoint}"
        last_error = None

        for attempt in range(retries):
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

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                if attempt < retries - 1:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue

        raise RemnawaveAPIError(f"Request failed: {last_error}")
    
    async def _request_json(self, method: str, endpoint: str, params: dict = None, json_body: dict = None, retries: int = 3) -> dict:
        if not self.base_url:
            raise RemnawaveAPIError("API URL not configured")

        url = f"{self.base_url}{endpoint}"
        last_error = None

        for attempt in range(retries):
            try:
                session = await self._get_session()
                kwargs = {}
                if params:
                    kwargs['params'] = params
                if json_body is not None:
                    kwargs['json'] = json_body

                async with session.request(method, url, **kwargs) as response:
                    response_text = await response.text()
                    try:
                        response_data = await response.json() if response_text else {}
                    except Exception:
                        response_data = {'raw_response': response_text}

                    if response.status >= 400:
                        error_msg = response_data.get('message', f'HTTP {response.status}')
                        raise RemnawaveAPIError(error_msg, status_code=response.status)

                    return response_data

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                if attempt < retries - 1:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue

        raise RemnawaveAPIError(f"Request failed: {last_error}")

    async def get_all_nodes(self) -> list[dict]:
        result = await self._request("GET", "/api/nodes")
        return result.get("response", [])

    async def fetch_users_ips(self, node_uuid: str) -> str:
        """Initiate IP fetch job for all users on a node. Returns jobId."""
        result = await self._request_json("POST", f"/api/ip-control/fetch-users-ips/{node_uuid}")
        return result.get("response", {}).get("jobId", "")

    async def get_fetch_users_ips_result(self, job_id: str) -> dict:
        result = await self._request("GET", f"/api/ip-control/fetch-users-ips/result/{job_id}")
        return result.get("response", {})

    async def poll_users_ips(self, node_uuid: str, timeout: int = 60) -> list[dict]:
        """Fetch user IPs for a node: start job, poll until done. Returns list of user IP entries."""
        job_id = await self.fetch_users_ips(node_uuid)
        if not job_id:
            raise RemnawaveAPIError(f"No jobId returned for node {node_uuid}")

        delay = 1.0
        elapsed = 0.0
        while elapsed < timeout:
            await asyncio.sleep(delay)
            elapsed += delay

            result = await self.get_fetch_users_ips_result(job_id)

            if result.get("isFailed"):
                raise RemnawaveAPIError(f"IP fetch job failed for node {node_uuid}")

            if result.get("isCompleted"):
                inner = result.get("result")
                if inner and inner.get("success"):
                    return inner.get("users", [])
                raise RemnawaveAPIError(f"IP fetch completed but not successful for node {node_uuid}")

            delay = min(delay * 1.5, 5.0)

        raise RemnawaveAPIError(f"IP fetch timeout ({timeout}s) for node {node_uuid}")

    async def delete_all_user_hwid_devices(self, user_uuid: str) -> bool:
        """POST /api/hwid/devices/delete-all — очистить все HWID устройства пользователя."""
        result = await self._request_json("POST", "/api/hwid/devices/delete-all", json_body={"userUuid": user_uuid})
        return bool(result.get("response"))

    async def get_hwid_devices(self, start: int = 0, size: int = 200) -> dict:
        params = {"start": start, "size": size}
        result = await self._request("GET", "/api/hwid/devices", params=params)
        response = result.get("response", {})
        logger.debug(f"HWID devices page start={start}: {response.get('total', '?')} total, {len(response.get('devices', []))} in page")
        return response

    async def get_all_hwid_devices_paginated(self, size: int = 200, concurrency: int = 5) -> list[dict]:
        first_page = await self.get_hwid_devices(start=0, size=size)
        devices = first_page.get('devices', [])
        total = first_page.get('total', len(devices))

        all_devices = list(devices)
        if total <= size:
            return all_devices

        remaining_offsets = list(range(size, total, size))
        sem = asyncio.Semaphore(concurrency)

        async def _fetch_page(offset: int) -> list[dict] | Exception:
            async with sem:
                for attempt in range(3):
                    try:
                        result = await self.get_hwid_devices(start=offset, size=size)
                        return result.get('devices', [])
                    except Exception as e:
                        if attempt < 2:
                            await asyncio.sleep(1 * (attempt + 1))
                            continue
                        return e

        tasks = [_fetch_page(offset) for offset in remaining_offsets]
        results = await asyncio.gather(*tasks)

        failed = 0
        for result in results:
            if isinstance(result, list):
                all_devices.extend(result)
            elif isinstance(result, Exception):
                failed += 1
                logger.warning(f"HWID page fetch failed after retries: {result}")

        if failed:
            logger.warning(f"HWID pagination: {failed}/{len(remaining_offsets)} pages failed")

        return all_devices

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
        first_page = await self.get_all_users(start=0, size=size)
        users = first_page.get('users', [])
        total = first_page.get('total', len(users))

        all_users = list(users)
        if total <= size:
            return all_users

        remaining_offsets = list(range(size, total, size))
        sem = asyncio.Semaphore(concurrency)

        async def _fetch_page(offset: int) -> list[dict] | Exception:
            async with sem:
                for attempt in range(3):
                    try:
                        result = await self.get_all_users(start=offset, size=size)
                        return result.get('users', [])
                    except Exception as e:
                        if attempt < 2:
                            await asyncio.sleep(1 * (attempt + 1))
                            continue
                        return e

        tasks = [_fetch_page(offset) for offset in remaining_offsets]
        results = await asyncio.gather(*tasks)

        failed = 0
        for result in results:
            if isinstance(result, list):
                all_users.extend(result)
            elif isinstance(result, Exception):
                failed += 1
                logger.warning(f"User page fetch failed after retries: {result}")

        if failed:
            logger.warning(f"User pagination: {failed}/{len(remaining_offsets)} pages failed, got {len(all_users)}/{total}")
        else:
            logger.debug(f"Fetched {len(all_users)}/{total} users from Remnawave")

        return all_users

    # ── Torrent Blocker ──

    async def get_torrent_blocker_reports(self, start: int = 0, size: int = 500) -> dict:
        result = await self._request("GET", "/api/node-plugins/torrent-blocker", params={"start": start, "size": size})
        return result.get("response", {})

    async def get_torrent_blocker_stats(self) -> dict:
        result = await self._request("GET", "/api/node-plugins/torrent-blocker/stats")
        return result.get("response", {})

    async def truncate_torrent_blocker_reports(self) -> dict:
        result = await self._request("DELETE", "/api/node-plugins/torrent-blocker/truncate")
        return result.get("response", {})

def get_remnawave_api(api_url: str, api_token: str, cookie_secret: Optional[str] = None) -> RemnawaveAPI:
    return RemnawaveAPI(api_url, api_token, cookie_secret)
