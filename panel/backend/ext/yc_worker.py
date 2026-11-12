#!/usr/bin/env python3
"""
Yandex Cloud IP Sniper Worker
Subprocess-воркер: читает JSON из stdin, циклически резервирует IP в Yandex Cloud,
проверяет принадлежность к целевым подсетям.
"""

import asyncio
import ipaddress
import json
import os
import random
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from urllib.parse import urlparse


# ═══════════════════════════════════════════════════════════════════════
# PROXY PARSER
# ═══════════════════════════════════════════════════════════════════════

def _parse_proxy(proxy_str: str) -> str | None:
    if not proxy_str or not proxy_str.strip():
        return None
    proxy_str = proxy_str.strip()
    if "://" in proxy_str:
        parsed = urlparse(proxy_str)
        if parsed.hostname and parsed.port:
            scheme = parsed.scheme or "socks5h"
            if scheme == "socks5":
                scheme = "socks5h"
            if parsed.username and parsed.password:
                return f"{scheme}://{parsed.username}:{parsed.password}@{parsed.hostname}:{parsed.port}"
            return f"{scheme}://{parsed.hostname}:{parsed.port}"
        return None
    if "@" in proxy_str:
        parsed = urlparse(f"socks5h://{proxy_str}")
        if parsed.hostname and parsed.port:
            if parsed.username and parsed.password:
                return f"socks5h://{parsed.username}:{parsed.password}@{parsed.hostname}:{parsed.port}"
            return f"socks5h://{parsed.hostname}:{parsed.port}"
        return None
    parts = proxy_str.split(":")
    if len(parts) == 4:
        host, port, user, pw = parts
        try:
            int(port)
            return f"socks5h://{user}:{pw}@{host}:{port}"
        except ValueError:
            user2, pw2, host2, port2 = parts
            try:
                int(port2)
                return f"socks5h://{user2}:{pw2}@{host2}:{port2}"
            except ValueError:
                return None
    elif len(parts) == 2:
        host, port = parts
        try:
            int(port)
            return f"socks5h://{host}:{port}"
        except ValueError:
            return None
    return None


# ═══════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════

YC_IAM_URL = "https://iam.api.cloud.yandex.net/iam/v1/tokens"
YC_VPC_URL = "https://vpc.api.cloud.yandex.net/vpc/v1/addresses"
YC_OPERATIONS_URL = "https://operation.api.cloud.yandex.net/operations"

IAM_TOKEN_LIFETIME = 11 * 3600  # обновлять через 11 часов (живёт 12)
OPERATION_POLL_TIMEOUT = 60
OPERATION_POLL_INTERVAL = 2
QUOTA_PAUSE_SECONDS = 300

USER_AGENT = "yandex-cloud-sdk/1.0 python-requests/2.32.3"
MSK = timezone(timedelta(hours=3))

MAX_RETRIES = 5
RETRY_BACKOFF_BASE = 30


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════

def msk_now() -> str:
    return datetime.now(MSK).strftime("%H:%M:%S")


def format_uptime(start_time: float) -> str:
    elapsed = int(time.time() - start_time)
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def emit_event(event: dict):
    try:
        print(f"EVENT:{json.dumps(event, ensure_ascii=False)}", flush=True)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════
# DATACLASSES
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class YcProject:
    folder_id: str
    oauth_token: str
    proxy: Optional[str]
    account_db_id: int


@dataclass
class YcConfig:
    delay_min: int = 5
    delay_max: int = 15
    stop_on_catch: bool = True
    max_auth_errors: int = 3
    proxy_required: bool = False
    proxy_check_interval: int = 300
    log_level: str = "info"
    start_delay_max: int = 30
    request_timeout: float = 30.0
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


# ═══════════════════════════════════════════════════════════════════════
# WORKER
# ═══════════════════════════════════════════════════════════════════════

class YcWorker:

    def __init__(
        self,
        project: YcProject,
        target_subnets: list[str],
        zones: list[str],
        config: YcConfig,
        panel_base_url: str = "",
    ):
        self.project = project
        self.networks = [ipaddress.ip_network(s, strict=False) for s in target_subnets]
        self.target_subnets_raw = target_subnets
        self.zones = zones
        self.config = config
        self.panel_base_url = panel_base_url.rstrip("/")

        self.iam_token: Optional[str] = None
        self.iam_token_time: Optional[float] = None

        self.attempts = 0
        self.caught = 0
        self.zone_idx = 0
        self.running = False
        self._stop_requested = asyncio.Event()
        self.start_time: float = 0.0
        self.last_error = ""
        self.consecutive_auth_errors = 0

        self._client: Optional[httpx.AsyncClient] = None
        self._panel_client: Optional[httpx.AsyncClient] = None

    # ──────────────────────── lifecycle ────────────────────────

    def _build_client(self) -> httpx.AsyncClient:
        kwargs: dict = {
            "timeout": self.config.request_timeout,
            "headers": {"User-Agent": USER_AGENT},
            "follow_redirects": True,
        }
        if self.project.proxy:
            proxy_url = _parse_proxy(self.project.proxy)
            if proxy_url:
                kwargs["proxy"] = proxy_url
        return httpx.AsyncClient(**kwargs)

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = self._build_client()
        return self._client

    async def _ensure_panel_client(self) -> httpx.AsyncClient:
        if self._panel_client is None or self._panel_client.is_closed:
            self._panel_client = httpx.AsyncClient(timeout=10.0)
        return self._panel_client

    async def close(self):
        for c in (self._client, self._panel_client):
            if c and not c.is_closed:
                await c.aclose()

    def log(self, msg: str, level: str = "info"):
        if level == "full" and self.config.log_level != "full":
            return
        print(f"[{msk_now()}] [ACC {self.project.account_db_id}] {msg}", flush=True)

    def stop(self):
        self.running = False
        self._stop_requested.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    async def _pause(self, seconds: float):
        """Пауза, которую прерывает остановка: иначе SIGTERM ждал бы, пока она истечёт."""
        try:
            await asyncio.wait_for(self._stop_requested.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ──────────────────────── IAM token ────────────────────────

    def _iam_token_valid(self) -> bool:
        if not self.iam_token or not self.iam_token_time:
            return False
        return (time.time() - self.iam_token_time) < IAM_TOKEN_LIFETIME

    async def get_iam_token(self, force: bool = False) -> bool:
        if not force and self._iam_token_valid():
            return True

        client = await self._ensure_client()
        try:
            resp = await client.post(
                YC_IAM_URL,
                json={"yandexPassportOauthToken": self.project.oauth_token},
            )
            if resp.status_code == 200:
                data = resp.json()
                self.iam_token = data["iamToken"]
                self.iam_token_time = time.time()
                self.consecutive_auth_errors = 0
                self.log("IAM token obtained", level="full")
                return True

            self.log(f"IAM token error: HTTP {resp.status_code}")
            return False

        except httpx.ProxyError as e:
            self.log(f"IAM token proxy error: {str(e)[:80]}")
            return False
        except Exception as e:
            self.log(f"IAM token error: {type(e).__name__}: {str(e)[:80]}")
            return False

    def _auth_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.iam_token}",
            "Content-Type": "application/json",
        }

    # ──────────────────────── VPC operations ────────────────────────

    async def reserve_ip(self, zone: str) -> tuple[Optional[str], str]:
        """Резервирование IP. Возвращает (operation_id, error_or_status)."""
        client = await self._ensure_client()
        name = f"ip-pick-{os.urandom(4).hex()}"
        body = {
            "folderId": self.project.folder_id,
            "name": name,
            "externalIpv4AddressSpec": {"zoneId": zone},
        }

        try:
            resp = await client.post(YC_VPC_URL, json=body, headers=self._auth_headers())

            if resp.status_code == 200:
                data = resp.json()
                op_id = data.get("id")
                if op_id:
                    return op_id, "OK"
                return None, "NO_OPERATION_ID"

            if resp.status_code == 401:
                return None, "AUTH_ERROR_401"

            error_text = ""
            try:
                err_data = resp.json()
                error_text = str(err_data.get("message", err_data))[:200]
            except Exception:
                error_text = resp.text[:200]

            if resp.status_code == 429 or "QUOTA_EXCEEDED" in error_text.upper():
                return None, f"QUOTA_EXCEEDED: {error_text}"

            return None, f"HTTP_{resp.status_code}: {error_text}"

        except httpx.ProxyError as e:
            return None, f"PROXY_ERROR: {str(e)[:80]}"
        except httpx.TimeoutException:
            return None, "TIMEOUT"
        except Exception as e:
            return None, f"ERROR: {type(e).__name__}: {str(e)[:80]}"

    async def poll_operation(self, op_id: str) -> tuple[Optional[dict], str]:
        """Ждёт завершения операции. Возвращает (response_obj, error)."""
        client = await self._ensure_client()
        url = f"{YC_OPERATIONS_URL}/{op_id}"
        deadline = time.time() + OPERATION_POLL_TIMEOUT

        while time.time() < deadline:
            try:
                resp = await client.get(url, headers=self._auth_headers())

                if resp.status_code == 401:
                    return None, "AUTH_ERROR_401"

                if resp.status_code != 200:
                    return None, f"POLL_HTTP_{resp.status_code}"

                data = resp.json()
                if data.get("done"):
                    if "error" in data:
                        err = data["error"]
                        return None, f"OP_ERROR: {err.get('message', str(err))[:200]}"
                    return data.get("response", {}), "OK"

            except httpx.TimeoutException:
                pass
            except Exception as e:
                return None, f"POLL_ERROR: {type(e).__name__}: {str(e)[:80]}"

            await asyncio.sleep(OPERATION_POLL_INTERVAL)

        return None, "POLL_TIMEOUT"

    async def release_ip(self, address_id: str) -> bool:
        client = await self._ensure_client()
        url = f"{YC_VPC_URL}/{address_id}"
        try:
            resp = await client.delete(url, headers=self._auth_headers())
            if resp.status_code in (200, 204):
                self.log(f"Released {address_id[:12]}...", level="full")
                return True
            self.log(f"Release error: HTTP {resp.status_code}", level="full")
            return False
        except Exception as e:
            self.log(f"Release error: {type(e).__name__}: {str(e)[:60]}", level="full")
            return False

    # ──────────────────────── subnet check ────────────────────────

    def check_subnet(self, ip_str: str) -> Optional[str]:
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            return None
        for net in self.networks:
            if addr in net:
                return str(net)
        return None

    # ──────────────────────── callbacks ────────────────────────

    async def notify_panel(self, ip: str, address_id: str, zone: str):
        if not self.panel_base_url:
            return
        try:
            client = await self._ensure_panel_client()
            await client.post(
                f"{self.panel_base_url}/yc/internal/caught-ip",
                params={
                    "account_id": self.project.account_db_id,
                    "ip_address": ip,
                    "address_id": address_id,
                    "zone": zone,
                },
            )
        except Exception:
            pass

    async def report_received(self, ip: str, zone: str, caught: bool):
        """Каждый выданный облаком адрес — в статистику панели, не только попадания"""
        if not self.panel_base_url:
            return
        try:
            client = await self._ensure_panel_client()
            await client.post(
                f"{self.panel_base_url}/yc/internal/received-ips",
                json={
                    "account_id": self.project.account_db_id,
                    "items": [{"ip": ip, "zone": zone, "caught": caught}],
                },
            )
        except httpx.HTTPError as e:
            self.log(f"Stats report error: {str(e)[:60]}", level="full")

    async def notify_telegram(self, message: str):
        if not self.config.telegram_bot_token or not self.config.telegram_chat_id:
            return
        try:
            client = await self._ensure_panel_client()
            await client.post(
                f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage",
                data={
                    "chat_id": self.config.telegram_chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                },
            )
        except Exception as e:
            self.log(f"Telegram error: {str(e)[:60]}", level="full")

    # ──────────────────────── main loop ────────────────────────

    async def run(self):
        self.running = True

        start_delay_max = self.config.start_delay_max
        if start_delay_max > 1:
            delay = random.uniform(1, start_delay_max)
            scheduled = datetime.now() + timedelta(seconds=delay)
            self.log(f"SCHEDULED | Start in {delay:.1f}s at {scheduled.strftime('%H:%M:%S')}")
            await self._pause(delay)

        if not self.running:
            return

        self.start_time = time.time()
        self.log(f"START | folder={self.project.folder_id[:8]}... | zones={self.zones} | subnets={self.target_subnets_raw}")

        if self.config.proxy_required and not self.project.proxy:
            self.log("NO_PROXY — proxy_required=True, waiting")
            emit_event({
                "type": "error",
                "account_id": self.project.account_db_id,
                "error_type": "NO_PROXY",
                "message": "Proxy required but not configured",
            })
            emit_event({
                "type": "account_finished",
                "account_id": self.project.account_db_id,
                "reason": "NO_PROXY",
                "attempts": 0,
                "caught": 0,
            })
            return

        if not await self.get_iam_token():
            self.log("FAILED to get initial IAM token")
            emit_event({
                "type": "error",
                "account_id": self.project.account_db_id,
                "error_type": "IAM_ERROR",
                "message": "Failed to obtain initial IAM token",
            })
            emit_event({
                "type": "account_finished",
                "account_id": self.project.account_db_id,
                "reason": "IAM_ERROR",
                "attempts": 0,
                "caught": 0,
            })
            return

        try:
            await self._loop()
        finally:
            await self.close()

        self.log(f"FINISHED | Attempts: {self.attempts} | Caught: {self.caught} | Uptime: {format_uptime(self.start_time)}")
        emit_event({
            "type": "account_finished",
            "account_id": self.project.account_db_id,
            "reason": self.last_error or "FINISHED",
            "attempts": self.attempts,
            "caught": self.caught,
        })

    async def _loop(self):
        while self.running:
            # Обновление IAM-токена если устарел
            if not self._iam_token_valid():
                if not await self.get_iam_token(force=True):
                    self.consecutive_auth_errors += 1
                    if self.consecutive_auth_errors >= self.config.max_auth_errors:
                        self.log(f"IAM_BLOCKED after {self.consecutive_auth_errors} errors")
                        self.last_error = "IAM_BLOCKED"
                        emit_event({
                            "type": "error",
                            "account_id": self.project.account_db_id,
                            "error_type": "ACCOUNT_BLOCKED",
                            "message": f"IAM token failures: {self.consecutive_auth_errors}",
                        })
                        break
                    await self._pause(5)
                    continue

            zone = self.zones[self.zone_idx % len(self.zones)]
            self.zone_idx += 1

            delay = random.uniform(self.config.delay_min, self.config.delay_max)
            await self._pause(delay)

            if not self.running:
                break

            self.attempts += 1

            # Резервирование
            op_id, reserve_err = await self.reserve_ip(zone)

            if not op_id:
                handled = await self._handle_reserve_error(reserve_err, zone)
                if not handled:
                    break
                continue

            # Polling операции
            address_obj, poll_err = await self.poll_operation(op_id)
            if not address_obj:
                if "AUTH_ERROR" in poll_err:
                    ok = await self._handle_auth_error()
                    if not ok:
                        break
                else:
                    self.log(f"#{self.attempts} | POLL_FAIL: {poll_err} | zone={zone}")
                    self.last_error = poll_err
                continue

            address_id = address_obj.get("id", "")
            ip = (address_obj.get("externalIpv4Address") or {}).get("address", "")

            if not ip:
                self.log(f"#{self.attempts} | NO_IP_IN_RESPONSE | zone={zone}")
                if address_id:
                    await self.release_ip(address_id)
                continue

            # Проверка подсети
            subnet = self.check_subnet(ip)
            await self.report_received(ip, zone, caught=subnet is not None)
            if subnet:
                await self._handle_catch(ip, address_id, zone, subnet)
                if self.config.stop_on_catch:
                    break
            else:
                if self.config.log_level == "full" or self.attempts % 10 == 0:
                    self.log(f"#{self.attempts} | MISS: {ip} — не в целевых подсетях, освобождаю | zone={zone}")
                await self.release_ip(address_id)

            # Периодический status event
            if self.attempts % 10 == 0:
                emit_event({
                    "type": "status",
                    "account_id": self.project.account_db_id,
                    "status": "running",
                    "attempts": self.attempts,
                    "caught": self.caught,
                })

    # ──────────────────────── error handlers ────────────────────────

    async def _handle_reserve_error(self, error: str, zone: str) -> bool:
        """Обработка ошибки резервирования. True = продолжать цикл, False = выход."""
        if "AUTH_ERROR" in error:
            return await self._handle_auth_error()

        if "QUOTA_EXCEEDED" in error:
            self.log(f"QUOTA_EXCEEDED — пауза {QUOTA_PAUSE_SECONDS}s | zone={zone}")
            self.last_error = "QUOTA_EXCEEDED"
            emit_event({
                "type": "error",
                "account_id": self.project.account_db_id,
                "error_type": "QUOTA_EXCEEDED",
                "message": error,
            })
            await self._pause(QUOTA_PAUSE_SECONDS)
            return True

        if "PROXY_ERROR" in error:
            self.log(f"PROXY_ERROR: {error}")
            self.last_error = "PROXY_ERROR"
            emit_event({
                "type": "proxy_waiting",
                "account_id": self.project.account_db_id,
            })
            await self._pause(30)
            return True

        if "TIMEOUT" in error or "CONNECTION" in error:
            self.log(f"#{self.attempts} | {error} | zone={zone}")
            self.last_error = error
            await self._pause(5)
            return True

        self.log(f"#{self.attempts} | RESERVE_FAIL: {error} | zone={zone}")
        self.last_error = error
        return True

    async def _handle_auth_error(self) -> bool:
        """Обновить IAM-токен при 401. True = продолжать, False = выход."""
        self.log("AUTH_ERROR_401 — refreshing IAM token")
        self.iam_token = None
        self.iam_token_time = None

        if await self.get_iam_token(force=True):
            self.log("IAM token refreshed OK")
            return True

        self.consecutive_auth_errors += 1
        if self.consecutive_auth_errors >= self.config.max_auth_errors:
            self.log(f"IAM_BLOCKED after {self.consecutive_auth_errors} consecutive errors")
            self.last_error = "IAM_BLOCKED"
            emit_event({
                "type": "error",
                "account_id": self.project.account_db_id,
                "error_type": "ACCOUNT_BLOCKED",
                "message": f"IAM auth errors: {self.consecutive_auth_errors}",
            })
            return False

        await self._pause(5)
        return True

    # ──────────────────────── catch ────────────────────────

    async def _handle_catch(self, ip: str, address_id: str, zone: str, subnet: str):
        self.caught += 1
        self.log(f"CAUGHT: {ip} в подсети {subnet}, зона {zone}")

        emit_event({
            "type": "ip_caught",
            "account_id": self.project.account_db_id,
            "ip": ip,
            "address_id": address_id,
            "zone": zone,
            "subnet": subnet,
            "total_caught": self.caught,
            "total_attempts": self.attempts,
        })

        await asyncio.gather(
            self.notify_panel(ip, address_id, zone),
            self.notify_telegram(
                f"<b>YC IP CAUGHT</b>\n\n"
                f"IP: <b>{ip}</b>\n"
                f"Subnet: {subnet}\n"
                f"Zone: {zone}\n"
                f"Account: {self.project.account_db_id}\n"
                f"Attempts: {self.attempts}"
            ),
            return_exceptions=True,
        )


# ═══════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════

def parse_stdin() -> dict:
    raw = sys.stdin.read().strip()
    if not raw:
        print("ERROR: No JSON config on stdin", flush=True)
        sys.exit(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON: {e}", flush=True)
        sys.exit(1)


def build_worker(data: dict) -> YcWorker:
    cfg_raw = data.get("config", {})
    config = YcConfig(
        delay_min=cfg_raw.get("delay_min", 5),
        delay_max=cfg_raw.get("delay_max", 15),
        stop_on_catch=cfg_raw.get("stop_on_catch", True),
        max_auth_errors=cfg_raw.get("max_auth_errors", 3),
        proxy_required=cfg_raw.get("proxy_required", False),
        proxy_check_interval=cfg_raw.get("proxy_check_interval", 300),
        log_level=cfg_raw.get("log_level", "info"),
        start_delay_max=cfg_raw.get("start_delay_max", 30),
        request_timeout=cfg_raw.get("request_timeout", 30.0),
        telegram_bot_token=cfg_raw.get("telegram_bot_token", ""),
        telegram_chat_id=cfg_raw.get("telegram_chat_id", ""),
    )

    project = YcProject(
        folder_id=data["folder_id"],
        oauth_token=data["oauth_token"],
        proxy=data.get("proxy"),
        account_db_id=data.get("account_id", 0),
    )

    target_subnets = data.get("target_subnets", [])
    zones = data.get("zones", ["ru-central1-b"])

    return YcWorker(
        project=project,
        target_subnets=target_subnets,
        zones=zones,
        config=config,
        panel_base_url=data.get("panel_base_url", ""),
    )


async def main_single_account():
    data = parse_stdin()
    worker = build_worker(data)

    def signal_handler():
        worker.stop()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            signal.signal(sig, lambda s, f: signal_handler())

    retry_count = 0
    while retry_count < MAX_RETRIES:
        try:
            await worker.run()
            break
        except asyncio.CancelledError:
            break
        except Exception as e:
            retry_count += 1
            print(f"FATAL_ERROR #{retry_count}: {type(e).__name__}: {e}", flush=True)
            emit_event({
                "type": "error",
                "account_id": worker.project.account_db_id,
                "error_type": "FATAL_CRASH",
                "message": f"{type(e).__name__}: {str(e)[:200]}",
            })
            if retry_count < MAX_RETRIES:
                print(f"Restarting in {RETRY_BACKOFF_BASE}s... ({retry_count}/{MAX_RETRIES})", flush=True)
                try:
                    await asyncio.sleep(RETRY_BACKOFF_BASE)
                except asyncio.CancelledError:
                    break
                if worker.stop_requested:
                    break
                worker.running = True
                worker.consecutive_auth_errors = 0
                worker.last_error = ""
            else:
                print(f"Max retries ({MAX_RETRIES}) reached, exiting", flush=True)
                emit_event({
                    "type": "account_finished",
                    "account_id": worker.project.account_db_id,
                    "reason": "FATAL_CRASH",
                    "attempts": worker.attempts,
                    "caught": worker.caught,
                })


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Yandex Cloud IP Sniper Worker")
    parser.add_argument("--single-account", action="store_true", help="Single account mode (reads JSON from stdin)")
    parser.parse_args()

    try:
        asyncio.run(main_single_account())
    except KeyboardInterrupt:
        print("\nStopped", flush=True)
