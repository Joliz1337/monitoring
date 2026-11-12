#!/usr/bin/env python3
"""
VK Cloud IP Sniper Worker (Panel Version)
Упрощённая версия для работы через панель управления.

Ключевой функционал:
  - OpenStack API через requests с SOCKS5 прокси
  - Уникальный fingerprint (User-Agent) для каждого аккаунта
  - Чистые логи без ANSI escape кодов

Требования:
  pip3 install pysocks requests[socks] aiohttp
"""

import asyncio
import ipaddress
import itertools
import json
import random
import os
import sys
import signal
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Callable
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ═══════════════════════════════════════════════════════════════════════
# КОНФИГУРАЦИЯ (патчится backend_service.py через regex)
# ═══════════════════════════════════════════════════════════════════════
DELAY_MIN = 60
DELAY_MAX = 90
MAX_IP_PER_PROJECT = 1
NETWORK_NAME = "internet"
STOP_ON_CATCH = True
MAX_AUTH_ERRORS = 3
PROXY_CHECK_INTERVAL = 50
PROXY_REQUIRED = True
LOG_LEVEL = "info"  # "info" или "full"
START_DELAY_MAX = 60  # Максимальная задержка старта аккаунта (минимум всегда 1 сек)

# Telegram (патчится backend_service.py)
TELEGRAM_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TG_CHAT_ID", "")

# Целевые подсети (патчится backend_service.py)
TARGET_SUBNETS = {
    17: "109.120.188.0/22",
    19: "89.208.228.0/22",
    24: "5.188.140.0/22",
    35: "95.163.248.0/22",
}

# Узкий целевой фильтр (передаётся из панели через config).
# Если список непуст — пойманный IP оставляем только при попадании в один из
# этих CIDR (напр. нужный /24 внутри выбранной /22), иначе floating-ip
# освобождается и перебор продолжается. Пустой список = ловим любой IP подсети.
PRECISE_CIDRS: list = []

# Раздача выбранных подсетей: по умолчанию аккаунту достаётся одна,
# в режиме rotate — все, и попытки идут по ним по кругу.
SUBNET_MODE_ROTATE = "rotate"

# Пути
LOG_DIR = Path("./logs")
CONFIG_FILE = Path("./profiles.conf")

# Собираем config из переменных (для совместимости с API)
def _build_config():
    return {
        "delay_min": DELAY_MIN,
        "delay_max": DELAY_MAX,
        "max_ip_per_project": MAX_IP_PER_PROJECT,
        "network_name": NETWORK_NAME,
        "stop_on_catch": STOP_ON_CATCH,
        "max_auth_errors": MAX_AUTH_ERRORS,
        "proxy_check_interval": PROXY_CHECK_INTERVAL,
        "proxy_required": PROXY_REQUIRED,
        "insecure_ssl": False,
        "request_timeout": 30.0,
        "log_level": LOG_LEVEL,
        "start_delay_max": START_DELAY_MAX,
        "precise_cidrs": list(PRECISE_CIDRS),
        "telegram_bot_token": TELEGRAM_BOT_TOKEN,
        "telegram_chat_id": TELEGRAM_CHAT_ID,
    }

DEFAULT_CONFIG = _build_config()

# OpenStack API endpoints
OS_AUTH_URL = "https://infra.mail.ru:35357/v3/"
OS_IDENTITY_URL = "https://infra.mail.ru:35357/v3"
OS_NETWORK_URL = "https://infra.mail.ru:9696/v2.0"

# User-Agents (официальные OpenStack клиенты)
USER_AGENTS = [
    "python-openstackclient/6.6.0 keystoneauth1/5.8.0 python-requests/2.32.3",
    "python-openstackclient/6.5.0 keystoneauth1/5.7.0 python-requests/2.32.2",
    "python-openstackclient/6.4.0 keystoneauth1/5.6.0 python-requests/2.31.0",
    "python-openstackclient/6.3.0 keystoneauth1/5.5.0 python-requests/2.31.0",
    "openstacksdk/3.3.0 keystoneauth1/5.8.0 python-requests/2.32.3",
    "openstacksdk/3.2.0 keystoneauth1/5.7.0 python-requests/2.32.2",
    "openstacksdk/3.1.0 keystoneauth1/5.6.0 python-requests/2.31.0",
    "openstacksdk/1.5.0 keystoneauth1/5.3.0 python-requests/2.28.2",
    "python-neutronclient/11.3.0 osc-lib/3.1.0 keystoneauth1/5.8.0 python-requests/2.32.3",
    "python-neutronclient/11.2.0 osc-lib/3.0.0 keystoneauth1/5.7.0 python-requests/2.32.2",
]


@dataclass
class Project:
    """Проект VK Cloud"""
    project_id: str
    project_name: str
    username: str
    password: str
    proxy: Optional[str] = None
    ip_count: int = 0
    disabled: bool = False
    account_db_id: Optional[int] = None  # ID аккаунта в БД панели
    project_db_id: Optional[int] = None  # ID проекта в БД панели


@dataclass
class AccountFingerprint:
    """Уникальный fingerprint для аккаунта"""
    user_agent: str
    accept: str = "application/json"
    accept_encoding: str = "gzip, deflate"
    
    def get_headers(self) -> dict:
        return {
            "User-Agent": self.user_agent,
            "Accept": self.accept,
            "Accept-Encoding": self.accept_encoding,
        }


@dataclass
class Account:
    """Аккаунт с проектами"""
    email: str
    projects: list[Project]
    account_id: int
    proxy_url: Optional[str] = None
    fingerprint: Optional[AccountFingerprint] = None
    proxy_verified: bool = False
    verified_ip: Optional[str] = None
    session: Optional[requests.Session] = None


def timestamp() -> str:
    """Текущее время для логов"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_valid_port(s: str) -> bool:
    try:
        return 1 <= int(s) <= 65535
    except ValueError:
        return False


def is_valid_host(s: str) -> bool:
    if not s:
        return False
    if s.isdigit():
        return False
    return bool(re.match(r'^[a-zA-Z0-9]([a-zA-Z0-9\-\.]*[a-zA-Z0-9])?$', s))


def parse_proxy_string(proxy_str: str) -> list[dict]:
    """Парсинг строки прокси в разных форматах"""
    proxy_str = proxy_str.strip()
    variants = []
    
    # socks5://user:pass@host:port
    if "://" in proxy_str:
        parsed = urlparse(proxy_str)
        if parsed.hostname and parsed.port:
            variants.append({
                "type": parsed.scheme or "socks5",
                "host": parsed.hostname,
                "port": parsed.port,
                "user": parsed.username or "",
                "pass": parsed.password or "",
            })
        return variants
    
    # user:pass@host:port
    if "@" in proxy_str:
        parsed = urlparse(f"socks5://{proxy_str}")
        if parsed.hostname and parsed.port:
            variants.append({
                "type": "socks5",
                "host": parsed.hostname,
                "port": parsed.port,
                "user": parsed.username or "",
                "pass": parsed.password or "",
            })
        return variants
    
    parts = proxy_str.split(":")
    
    # host:port:user:pass или user:pass:host:port
    if len(parts) == 4:
        host_a, port_a, user_a, pass_a = parts
        if is_valid_host(host_a) and is_valid_port(port_a):
            variants.append({
                "type": "socks5",
                "host": host_a,
                "port": int(port_a),
                "user": user_a,
                "pass": pass_a,
            })
        
        user_b, pass_b, host_b, port_b = parts
        if is_valid_host(host_b) and is_valid_port(port_b) and not is_valid_host(user_b):
            variants.append({
                "type": "socks5",
                "host": host_b,
                "port": int(port_b),
                "user": user_b,
                "pass": pass_b,
            })
    
    # host:port
    elif len(parts) == 2:
        host, port = parts
        if is_valid_host(host) and is_valid_port(port):
            variants.append({
                "type": "socks5",
                "host": host,
                "port": int(port),
                "user": "",
                "pass": "",
            })
    
    return variants


def proxy_to_url(proxy: dict, with_dns: bool = True) -> str:
    """Конвертация dict прокси в URL"""
    scheme = "socks5h" if with_dns else "socks5"
    if proxy["user"] and proxy["pass"]:
        return f"{scheme}://{proxy['user']}:{proxy['pass']}@{proxy['host']}:{proxy['port']}"
    return f"{scheme}://{proxy['host']}:{proxy['port']}"


def generate_fingerprint(account_id: int) -> AccountFingerprint:
    """Генерация уникального fingerprint для аккаунта"""
    random.seed(account_id * 31337 + 12345)
    fingerprint = AccountFingerprint(user_agent=random.choice(USER_AGENTS))
    random.seed()
    return fingerprint


def create_session(account: Account, insecure_ssl: bool = False) -> requests.Session:
    """Создание requests.Session с прокси и fingerprint"""
    session = requests.Session()
    
    if account.proxy_url:
        session.proxies = {
            "http": account.proxy_url,
            "https": account.proxy_url,
        }
    
    if account.fingerprint:
        session.headers.update(account.fingerprint.get_headers())
    
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.verify = not insecure_ssl
    
    return session


def test_proxy(session: requests.Session, timeout: float = 15, check_server: str = None) -> tuple[bool, str]:
    """Тестирование прокси"""
    if check_server:
        server = check_server.replace("http://", "").replace("https://", "")
        check_url = f"http://{server}"
    else:
        check_url = "https://api.ipify.org"
    
    try:
        response = session.get(check_url, timeout=timeout)
        if response.status_code == 200:
            if check_server:
                try:
                    data = response.json()
                    ip = data.get("ip", "").strip()
                except:
                    ip = response.text.strip()
            else:
                ip = response.text.strip()
            
            if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip):
                return True, ip
            return False, f"INVALID_IP: {ip[:50]}"
        return False, f"HTTP_{response.status_code}"
    except requests.exceptions.ProxyError as e:
        return False, f"PROXY_ERROR: {str(e)[:60]}"
    except requests.exceptions.SSLError as e:
        return False, f"SSL_ERROR: {str(e)[:60]}"
    except requests.exceptions.Timeout:
        return False, "TIMEOUT"
    except requests.exceptions.ConnectionError as e:
        return False, f"CONNECTION_ERROR: {str(e)[:60]}"
    except Exception as e:
        return False, f"ERROR: {str(e)[:60]}"


def normalize_proxy(raw_proxy: str, insecure_ssl: bool = False, check_server: str = None) -> tuple[Optional[dict], str]:
    """Нормализация и проверка прокси"""
    if not raw_proxy or not raw_proxy.strip():
        return None, "EMPTY"
    
    variants = parse_proxy_string(raw_proxy.strip())
    if not variants:
        return None, "INVALID_FORMAT"
    
    errors = []
    for idx, variant in enumerate(variants):
        proxy_url = proxy_to_url(variant, with_dns=True)
        
        test_session = requests.Session()
        test_session.proxies = {"http": proxy_url, "https": proxy_url}
        test_session.verify = not insecure_ssl
        
        success, result = test_proxy(test_session, timeout=15, check_server=check_server)
        test_session.close()
        
        if success:
            variant['verified_ip'] = result
            return variant, f"OK ({result})"
        errors.append(f"#{idx+1}: {result}")
    
    return None, f"FAILED: {'; '.join(errors)}" if len(variants) > 1 else errors[0].split(": ", 1)[-1]


class OpenStackAPI:
    """Прямой доступ к OpenStack API через requests"""
    
    def __init__(self, account: Account, project: Project, config: dict = None):
        self.account = account
        self.project = project
        self.session = account.session
        self.config = config or DEFAULT_CONFIG
        self.token: Optional[str] = None
        self.token_expires: Optional[float] = None
        self.subnet_cache: dict[str, str] = {}
        self.network_id_cache: Optional[str] = None
        self.timeout = self.config.get("request_timeout", 30.0)
    
    def _get_auth_headers(self) -> dict:
        headers = {}
        if self.account.fingerprint:
            headers.update(self.account.fingerprint.get_headers())
        if self.token:
            headers["X-Auth-Token"] = self.token
        headers["Content-Type"] = "application/json"
        return headers
    
    def _is_token_valid(self) -> bool:
        if not self.token or not self.token_expires:
            return False
        return time.time() < self.token_expires - 60
    
    def authenticate(self) -> tuple[bool, str]:
        """Авторизация через Keystone API"""
        if self._is_token_valid():
            return True, "TOKEN_CACHED"
        
        auth_data = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": self.project.username,
                            "domain": {"name": "users"},
                            "password": self.project.password,
                        }
                    }
                },
                "scope": {
                    "project": {"id": self.project.project_id}
                }
            }
        }
        
        headers = {}
        if self.account.fingerprint:
            headers.update(self.account.fingerprint.get_headers())
        headers["Content-Type"] = "application/json"
        
        try:
            response = self.session.post(
                f"{OS_IDENTITY_URL}/auth/tokens",
                json=auth_data,
                headers=headers,
                timeout=self.timeout,
            )
            
            if response.status_code == 201:
                self.token = response.headers.get("X-Subject-Token")
                try:
                    data = response.json()
                    expires_at = data.get("token", {}).get("expires_at", "")
                    if expires_at:
                        from datetime import datetime as dt
                        exp_dt = dt.fromisoformat(expires_at.replace("Z", "+00:00"))
                        self.token_expires = exp_dt.timestamp()
                    else:
                        self.token_expires = time.time() + 3600
                except:
                    self.token_expires = time.time() + 3600
                return True, "OK"
            
            if response.status_code == 401:
                return False, "AUTH_ERROR_401"
            if response.status_code == 403:
                return False, "ACCESS_DENIED_403"
            return False, f"HTTP_{response.status_code}"
            
        except requests.exceptions.ProxyError as e:
            return False, f"PROXY_ERROR: {str(e)[:60]}"
        except requests.exceptions.SSLError as e:
            return False, f"SSL_ERROR: {str(e)[:60]}"
        except requests.exceptions.Timeout:
            return False, "TIMEOUT"
        except requests.exceptions.ConnectionError as e:
            return False, f"CONNECTION_ERROR: {str(e)[:60]}"
        except Exception as e:
            return False, f"ERROR: {str(e)[:60]}"
    
    def get_subnet_id(self, subnet_name: str) -> tuple[Optional[str], str]:
        """Получение ID подсети по имени"""
        if subnet_name in self.subnet_cache:
            return self.subnet_cache[subnet_name], "CACHED"
        
        auth_ok, auth_err = self.authenticate()
        if not auth_ok:
            return None, auth_err
        
        try:
            response = self.session.get(
                f"{OS_NETWORK_URL}/subnets",
                params={"name": subnet_name},
                headers=self._get_auth_headers(),
                timeout=self.timeout,
            )
            
            if response.status_code == 200:
                data = response.json()
                subnets = data.get("subnets", [])
                if subnets:
                    subnet_id = subnets[0].get("id")
                    if subnet_id:
                        self.subnet_cache[subnet_name] = subnet_id
                        return subnet_id, "OK"
                    return None, "NO_ID_IN_RESPONSE"
                return None, "SUBNET_NOT_FOUND"
            
            if response.status_code == 401:
                self.token = None
                return None, "AUTH_ERROR_401"
            return None, f"HTTP_{response.status_code}"
            
        except requests.exceptions.ProxyError as e:
            return None, f"PROXY_ERROR: {str(e)[:60]}"
        except requests.exceptions.Timeout:
            return None, "TIMEOUT"
        except Exception as e:
            return None, f"ERROR: {str(e)[:60]}"
    
    def _get_network_id(self) -> tuple[Optional[str], str]:
        """Получение ID сети internet"""
        if self.network_id_cache:
            return self.network_id_cache, "CACHED"
        
        try:
            response = self.session.get(
                f"{OS_NETWORK_URL}/networks",
                params={"name": self.config.get("network_name", "internet")},
                headers=self._get_auth_headers(),
                timeout=self.timeout,
            )
            
            if response.status_code == 200:
                networks = response.json().get("networks", [])
                if networks:
                    self.network_id_cache = networks[0]["id"]
                    return self.network_id_cache, "OK"
                return None, "NETWORK_NOT_FOUND"
            return None, f"HTTP_{response.status_code}"
        except Exception as e:
            return None, f"ERROR: {str(e)[:50]}"
    
    def create_floating_ip(self, subnet_id: str) -> tuple[bool, str, Optional[str], Optional[str]]:
        """Создание Floating IP. Возвращает (ok, status, ip, floating_ip_id)."""
        auth_ok, auth_err = self.authenticate()
        if not auth_ok:
            return False, auth_err, None, None

        network_id, net_err = self._get_network_id()
        if not network_id:
            return False, f"NETWORK_ERROR: {net_err}", None, None

        body = {
            "floatingip": {
                "floating_network_id": network_id,
                "subnet_id": subnet_id,
            }
        }

        try:
            response = self.session.post(
                f"{OS_NETWORK_URL}/floatingips",
                json=body,
                headers=self._get_auth_headers(),
                timeout=self.timeout,
            )

            if response.status_code in (200, 201):
                fip = response.json().get("floatingip", {})
                ip = fip.get("floating_ip_address")
                fip_id = fip.get("id")
                if ip:
                    return True, "SUCCESS", ip, fip_id
                return False, "NO_IP_IN_RESPONSE", None, None

            try:
                error_data = response.json()
                error_msg = str(error_data).lower()
                # Извлекаем читаемое сообщение об ошибке
                error_detail = ""
                if isinstance(error_data, dict):
                    # OpenStack обычно возвращает {"NeutronError": {"message": "...", "type": "..."}}
                    for key in ["NeutronError", "error", "message"]:
                        if key in error_data:
                            inner = error_data[key]
                            if isinstance(inner, dict):
                                error_detail = inner.get("message", "") or inner.get("detail", "")
                            elif isinstance(inner, str):
                                error_detail = inner
                            break
                    if not error_detail:
                        error_detail = str(error_data)[:200]
                else:
                    error_detail = str(error_data)[:200]
            except:
                error_msg = response.text.lower()
                error_detail = response.text[:200]

            if response.status_code == 401:
                self.token = None
                return False, "AUTH_ERROR", None, None
            if response.status_code == 403:
                return False, f"ACCESS_DENIED: {error_detail}", None, None
            if response.status_code == 409:
                if "quota" in error_msg:
                    return False, f"QUOTA_EXCEEDED: {error_detail}", None, None
                if "no more" in error_msg or "exhausted" in error_msg:
                    return False, "NO_MORE_IP", None, None
                # Dike IPAM возвращает 409 когда пул пуст
                if "dike" in error_msg:
                    return False, "NO_MORE_IP", None, None
                # Другие конфликты
                return False, f"CONFLICT: {error_detail}", None, None
            if "quota" in error_msg:
                return False, f"QUOTA_EXCEEDED: {error_detail}", None, None
            if "no more ip" in error_msg or "exhausted" in error_msg:
                return False, "NO_MORE_IP", None, None

            return False, f"HTTP_{response.status_code}: {error_detail}", None, None

        except requests.exceptions.ProxyError as e:
            return False, f"PROXY_ERROR: {str(e)[:60]}", None, None
        except requests.exceptions.Timeout:
            return False, "TIMEOUT", None, None
        except requests.exceptions.ConnectionError as e:
            return False, f"CONNECTION_ERROR: {str(e)[:60]}", None, None
        except Exception as e:
            return False, f"ERROR: {str(e)[:60]}", None, None

    def delete_floating_ip(self, fip_id: str) -> tuple[bool, str]:
        """Освобождение (release) floating IP по id — вернуть в пул подсети."""
        auth_ok, auth_err = self.authenticate()
        if not auth_ok:
            return False, auth_err

        try:
            response = self.session.delete(
                f"{OS_NETWORK_URL}/floatingips/{fip_id}",
                headers=self._get_auth_headers(),
                timeout=self.timeout,
            )
            # 404 = уже удалён, считаем успехом
            if response.status_code in (200, 202, 204, 404):
                return True, "DELETED"
            if response.status_code == 401:
                self.token = None
                return False, "AUTH_ERROR"
            return False, f"HTTP_{response.status_code}"
        except requests.exceptions.ProxyError as e:
            return False, f"PROXY_ERROR: {str(e)[:60]}"
        except requests.exceptions.Timeout:
            return False, "TIMEOUT"
        except requests.exceptions.ConnectionError as e:
            return False, f"CONNECTION_ERROR: {str(e)[:60]}"
        except Exception as e:
            return False, f"ERROR: {str(e)[:60]}"


def emit_event(event: dict):
    """Отправка структурированного события в stdout для парсинга backend"""
    try:
        line = json.dumps(event, ensure_ascii=False)
        print(f"EVENT:{line}", flush=True)
    except Exception:
        pass


# Бэкенд панели внутри контейнера: префикс /api срезает nginx, сюда он не приходит
PANEL_INTERNAL_URL = "http://localhost:8000/_int"

# Shared aiohttp session для панели (избегаем Unclosed connection warnings)
_panel_session: Optional["aiohttp.ClientSession"] = None


async def _get_panel_session():
    """Получить или создать shared aiohttp session"""
    global _panel_session
    import aiohttp
    if _panel_session is None or _panel_session.closed:
        _panel_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        )
    return _panel_session


async def close_panel_session():
    """Закрыть shared aiohttp session"""
    global _panel_session
    if _panel_session and not _panel_session.closed:
        await _panel_session.close()
        _panel_session = None


async def save_ip_to_panel(
    ip_address: str,
    account_db_id: int,
    project_db_id: int,
    subnet_name: str
):
    """Сохранить пойманный IP в БД панели через HTTP"""
    try:
        session = await _get_panel_session()
        async with session.post(
            f"{PANEL_INTERNAL_URL}/ext/internal/caught-ip",
            params={
                "account_id": account_db_id,
                "project_id": project_db_id,
                "ip_address": ip_address,
                "subnet_name": subnet_name
            },
        ) as resp:
            pass  # Просто отправляем, не блокируем
    except Exception:
        pass  # Не блокируем основной процесс


async def report_received_to_panel(account_db_id: int, ip_address: str, subnet_name: str, caught: bool):
    """Каждый выданный облаком адрес — в статистику панели, не только попадания"""
    import aiohttp
    try:
        session = await _get_panel_session()
        async with session.post(
            f"{PANEL_INTERNAL_URL}/ext/internal/received-ips",
            json={
                "account_id": account_db_id,
                "items": [{"ip": ip_address, "zone": subnet_name, "caught": caught}],
            },
        ):
            pass
    except (aiohttp.ClientError, asyncio.TimeoutError):
        pass  # Статистика не должна останавливать снайпинг


class SniperWorker:
    """Воркер для снайпинга IP адресов"""
    
    def __init__(
        self,
        account: Account,
        subnet_name: str,
        config: dict = None,
        log_callback: Callable[[str], None] = None,
        on_ip_caught: Callable[[str, str], None] = None,
        subnet_names: Optional[list[str]] = None,
    ):
        self.account = account
        # subnet_name — подсеть текущей попытки; при нескольких подсетях меняется по кругу
        self.subnet_names = subnet_names or [subnet_name]
        self.subnet_cycle = itertools.cycle(self.subnet_names)
        self.subnet_name = self.subnet_names[0]
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.log_callback = log_callback or (lambda msg: print(f"{timestamp()} {msg}"))
        self.on_ip_caught = on_ip_caught
        self.log_level = self.config.get("log_level", "info")  # "info" или "full"
        
        self.running = False
        self._stop_requested = asyncio.Event()
        self.total_attempts = 0
        self.total_caught = 0
        self.total_released = 0  # IP вне узкого фильтра, освобождённые обратно в пул
        self.current_project_idx = 0
        self.consecutive_auth_errors = 0
        self.consecutive_proxy_errors = 0
        self.access_denied_projects: set = set()  # Проекты с ACCESS_DENIED (баланс)
        self.last_error = ""
        self.account_db_id: Optional[int] = None  # DB ID аккаунта для событий
        if account.projects:
            self.account_db_id = account.projects[0].account_db_id

        # Узкий целевой фильтр: держим только IP из этих CIDR, остальные освобождаем
        self.precise_networks: list = []
        for cidr in (self.config.get("precise_cidrs") or []):
            try:
                self.precise_networks.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                self.log(f"BAD_PRECISE_CIDR (ignored): {cidr}")
    
    def log(self, msg: str, level: str = "info"):
        """
        Логирование с поддержкой уровней.
        level: "info" - всегда показывается, "full" - только при log_level=full
        """
        if level == "full" and self.log_level != "full":
            return
        full_msg = f"[ACC {self.account.account_id}] {msg}"
        self.log_callback(full_msg)
    
    def log_full(self, msg: str):
        """Лог только для режима FULL"""
        self.log(msg, level="full")
    
    def stop(self):
        """Остановка воркера: начатая попытка доходит до конца, паузы прерываются"""
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
    
    async def notify_telegram(self, message: str):
        """Уведомление в Telegram — только о пойманном IP, остальное видно в панели"""
        bot_token = self.config.get("telegram_bot_token") or TELEGRAM_BOT_TOKEN
        chat_id = self.config.get("telegram_chat_id") or TELEGRAM_CHAT_ID

        if not bot_token or not chat_id:
            return

        try:
            import aiohttp
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            async with aiohttp.ClientSession() as session:
                await session.post(
                    url,
                    data={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
                    timeout=aiohttp.ClientTimeout(total=10)
                )
        except Exception as e:
            self.log_full(f"Telegram error: {e}")

    def _get_subnet_cidr(self) -> str:
        """Получить CIDR для текущей подсети"""
        try:
            subnet_num = int(self.subnet_name.replace("ext-sub", ""))
            return TARGET_SUBNETS.get(subnet_num, "")
        except:
            return ""

    def _ip_matches_precise(self, ip: str) -> bool:
        """Попадает ли IP в узкий целевой фильтр. Без фильтра — любой IP подходит."""
        if not self.precise_networks:
            return True
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self.precise_networks)
    
    async def verify_proxy(self) -> tuple[bool, str]:
        """Проверка работоспособности прокси"""
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None,
                lambda: test_proxy(self.account.session, timeout=10)
            )
        except Exception as e:
            return False, str(e)

    async def _wait_for_proxy_recovery(self) -> bool:
        """Ожидание восстановления прокси. Возвращает True если прокси ожил, False если воркер остановлен."""
        self.log("PROXY_DEAD - waiting for recovery...")

        emit_event({
            "type": "error",
            "account_id": self.account_db_id,
            "error_type": "PROXY_DEAD",
            "message": f"Proxy dead for {self.account.email}, waiting for recovery",
        })
        emit_event({
            "type": "proxy_waiting",
            "account_id": self.account_db_id,
        })

        check_interval = 30
        checks = 0
        while self.running:
            await self._pause(check_interval)
            if not self.running:
                return False

            checks += 1
            alive, check_ip = await self.verify_proxy()
            if alive:
                self.consecutive_proxy_errors = 0
                self.log(f"PROXY_RECOVERED after {checks * check_interval}s ({check_ip})")

                emit_event({
                    "type": "proxy_recovered",
                    "account_id": self.account_db_id,
                })

                return True

            if checks % 10 == 0:
                self.log_full(f"Proxy still down, waiting... ({checks * check_interval}s elapsed)")

        return False

    def _get_available_project(self) -> Optional[int]:
        """Получение доступного проекта"""
        max_ip = self.config.get("max_ip_per_project", 1)
        for i in range(len(self.account.projects)):
            idx = (self.current_project_idx + i) % len(self.account.projects)
            proj = self.account.projects[idx]
            if not proj.disabled and proj.ip_count < max_ip:
                return idx
        return None
    
    async def run(self) -> int:
        """Основной цикл снайпера"""
        self.running = True
        actual_start_delay = 0.0
        
        # Рандомная задержка старта (от 1 до start_delay_max секунд)
        start_delay_max = self.config.get("start_delay_max", 60)
        if start_delay_max > 1:
            actual_start_delay = random.uniform(1, start_delay_max)
            scheduled_start = datetime.now() + timedelta(seconds=actual_start_delay)
            self.log(f"SCHEDULED | Start in {actual_start_delay:.1f}s at {scheduled_start.strftime('%H:%M:%S')}")
            await self._pause(actual_start_delay)
        
        # Проверяем, не был ли воркер остановлен во время ожидания
        if not self.running:
            return 0
        
        self.log_full(f"=== WORKER INIT === log_level={self.log_level}")
        self.log_full(f"Config: delay={self.config.get('delay_min')}-{self.config.get('delay_max')}s, max_ip={self.config.get('max_ip_per_project')}, stop_on_catch={self.config.get('stop_on_catch')}, start_delay_max={start_delay_max}s")
        
        if self.config.get("proxy_required", True) and not self.account.proxy_verified:
            self.log("PROXY_NOT_VERIFIED - account disabled")
            return 0
        
        proxy_info = "direct"
        if self.account.proxy_url:
            if '@' in self.account.proxy_url:
                proxy_info = self.account.proxy_url.split('@')[-1]
            else:
                proxy_info = self.account.proxy_url.replace("socks5h://", "").replace("socks5://", "")
        
        self.log(f"START | {self.account.email} | Projects: {len(self.account.projects)} | Subnets: {', '.join(self.subnet_names)}")
        self.log_full(f"Proxy: {proxy_info}")
        self.log_full(f"User-Agent: {self.account.fingerprint.user_agent if self.account.fingerprint else 'default'}")
        self.log_full(f"Projects list: {[p.project_name for p in self.account.projects]}")
        
        apis = {idx: OpenStackAPI(self.account, proj, self.config) for idx, proj in enumerate(self.account.projects)}
        subnet_ids: dict[str, str] = {}
        
        loop = asyncio.get_event_loop()
        base_delay = random.uniform(
            self.config.get("delay_min", 60),
            self.config.get("delay_max", 90)
        )
        self.log_full(f"Base delay calculated: {base_delay:.1f}s")
        
        while self.running:
            # Периодическая проверка прокси
            check_interval = self.config.get("proxy_check_interval", 50)
            if self.config.get("proxy_required") and self.total_attempts > 0 and self.total_attempts % check_interval == 0:
                self.log_full(f"Proxy check triggered (interval={check_interval})")
                alive, check_ip = await self.verify_proxy()
                if not alive:
                    self.consecutive_proxy_errors += 1
                    self.log(f"PROXY_CHECK_FAILED ({self.consecutive_proxy_errors}/3)")
                    if self.consecutive_proxy_errors >= 3:
                        recovered = await self._wait_for_proxy_recovery()
                        if not recovered:
                            break
                    else:
                        await asyncio.sleep(10)
                    continue
                else:
                    self.consecutive_proxy_errors = 0
                    self.log_full(f"Proxy check OK: {check_ip}")
            
            proj_idx = self._get_available_project()
            if proj_idx is None:
                self.log(f"ALL_PROJECTS_EXHAUSTED | Caught: {self.total_caught}")
                emit_event({
                    "type": "account_finished",
                    "account_id": self.account_db_id,
                    "reason": "ALL_PROJECTS_EXHAUSTED",
                    "attempts": self.total_attempts,
                    "caught": self.total_caught,
                })
                break
            
            self.current_project_idx = proj_idx
            project = self.account.projects[proj_idx]
            api = apis[proj_idx]
            
            self.log_full(f"Selected project: {project.project_name} (idx={proj_idx}, ip_count={project.ip_count})")
            
            self.subnet_name = next(self.subnet_cycle)

            # Получение subnet_id
            if self.subnet_name not in subnet_ids:
                self.log_full(f"Fetching subnet_id for: {self.subnet_name}")
                subnet_id, subnet_err = await loop.run_in_executor(
                    None, lambda: api.get_subnet_id(self.subnet_name)
                )
                
                if subnet_id:
                    subnet_ids[self.subnet_name] = subnet_id
                    self.log(f"SUBNET_FOUND: {self.subnet_name} = {subnet_id[:8]}...")
                    self.log_full(f"Full subnet_id: {subnet_id}")
                else:
                    if "AUTH" in subnet_err:
                        self.consecutive_auth_errors += 1
                        max_auth_errors = self.config.get("max_auth_errors", 3)
                        self.log(f"AUTH_ERROR ({self.consecutive_auth_errors}/{max_auth_errors})")
                        self.last_error = "AUTH_ERROR"
                        if self.consecutive_auth_errors >= max_auth_errors:
                            self.log("ACCOUNT_BLOCKED")
                            self.running = False
                            break
                    else:
                        self.log(f"SUBNET_ERROR: {self.subnet_name}: {subnet_err}")
                        self.last_error = subnet_err
                    await asyncio.sleep(2)
                    continue
            
            self.total_attempts += 1
            
            # Попытка создания IP
            success, status, ip, fip_id = await loop.run_in_executor(
                None, lambda: api.create_floating_ip(subnet_ids[self.subnet_name])
            )

            # Вычисляем задержку для следующей попытки
            delay = base_delay + random.uniform(-5, 5)
            next_attempt_time = datetime.now().timestamp() + delay
            next_attempt_str = datetime.fromtimestamp(next_attempt_time).strftime("%H:%M:%S")

            if success and ip and self.account_db_id:
                await report_received_to_panel(
                    self.account_db_id, ip, self.subnet_name, self._ip_matches_precise(ip)
                )

            if success and ip and not self._ip_matches_precise(ip):
                # Промах узкого фильтра: IP вне целевого диапазона — освобождаем и продолжаем
                self.total_released += 1
                self.consecutive_auth_errors = 0
                self.last_error = ""
                self.log(
                    f"IP_RELEASED #{self.total_released}: {ip} вне целевого диапазона "
                    f"({project.project_name}) | Next: {next_attempt_str}"
                )
                if fip_id:
                    del_ok, del_status = await loop.run_in_executor(
                        None, lambda: api.delete_floating_ip(fip_id)
                    )
                    if not del_ok:
                        self.log(f"RELEASE_FAILED {ip}: {del_status}")
                # Слот квоты освобождён — ip_count не трогаем, аккаунт не останавливаем
                emit_event({
                    "type": "ip_released",
                    "account_id": self.account_db_id,
                    "ip": ip,
                    "project": project.project_name,
                    "project_db_id": project.project_db_id,
                    "subnet": self.subnet_name,
                    "total_released": self.total_released,
                    "total_attempts": self.total_attempts,
                })

            elif success and ip:
                self.total_caught += 1
                project.ip_count += 1
                self.consecutive_auth_errors = 0
                self.last_error = ""
                
                self.log(f"IP_CAUGHT #{self.total_caught}: {ip} ({project.project_name})")
                
                # Structured event
                emit_event({
                    "type": "ip_caught",
                    "account_id": self.account_db_id,
                    "ip": ip,
                    "project": project.project_name,
                    "project_db_id": project.project_db_id,
                    "subnet": self.subnet_name,
                    "total_caught": self.total_caught,
                    "total_released": self.total_released,
                    "total_attempts": self.total_attempts,
                })

                # Сохранить в БД панели
                if project.account_db_id and project.project_db_id:
                    await save_ip_to_panel(
                        ip_address=ip,
                        account_db_id=project.account_db_id,
                        project_db_id=project.project_db_id,
                        subnet_name=self.subnet_name
                    )
                
                # Telegram: уведомление о поимке IP
                cidr = self._get_subnet_cidr()
                subnet_display = f"{self.subnet_name} ({cidr})" if cidr else self.subnet_name
                catch_msg = (
                    f"🔥 <b>IP CAUGHT!</b>\n\n"
                    f"📧 {self.account.email}\n"
                    f"📁 {project.project_name}\n\n"
                    f"🎯 <b>{ip}</b>\n"
                    f"🌐 Subnet: {subnet_display}\n"
                    f"📊 Attempts: {self.total_attempts} | Total: {self.total_caught}"
                )
                await self.notify_telegram(catch_msg)
                
                if self.on_ip_caught:
                    self.on_ip_caught(ip, project.project_name)
                
                if self.config.get("stop_on_catch", True):
                    self.log("TARGET_REACHED - stopping")
                    self.running = False
                    break
                
                max_ip = self.config.get("max_ip_per_project", 1)
                if project.ip_count >= max_ip:
                    self.log(f"PROJECT_LIMIT: {project.project_name}")
                    # Сразу проверяем — есть ли ещё доступные проекты
                    if self._get_available_project() is None:
                        self.log(f"ALL_PROJECTS_EXHAUSTED | Caught: {self.total_caught}")
                        emit_event({
                            "type": "account_finished",
                            "account_id": self.account_db_id,
                            "reason": "ALL_PROJECTS_EXHAUSTED",
                            "attempts": self.total_attempts,
                            "caught": self.total_caught,
                        })
                        self.running = False
                        break
                    self.current_project_idx = (self.current_project_idx + 1) % len(self.account.projects)
            
            elif status.startswith("QUOTA_EXCEEDED"):
                self.log(f"QUOTA_EXCEEDED: {project.project_name}")
                self.last_error = "QUOTA_EXCEEDED"
                project.ip_count = self.config.get("max_ip_per_project", 1)
                emit_event({
                    "type": "error",
                    "account_id": self.account_db_id,
                    "error_type": "QUOTA_EXCEEDED",
                    "project": project.project_name,
                    "project_db_id": project.project_db_id,
                    "message": f"Quota exceeded for project {project.project_name}",
                })
                # Сразу проверяем — есть ли ещё доступные проекты
                if self._get_available_project() is None:
                    self.log(f"ALL_PROJECTS_EXHAUSTED | Caught: {self.total_caught}")
                    emit_event({
                        "type": "account_finished",
                        "account_id": self.account_db_id,
                        "reason": "ALL_PROJECTS_EXHAUSTED",
                        "attempts": self.total_attempts,
                        "caught": self.total_caught,
                    })
                    break
                # Есть другие проекты — переключаемся без задержки
                self.current_project_idx = (self.current_project_idx + 1) % len(self.account.projects)
                continue
            
            elif status.startswith("ACCESS_DENIED"):
                # ACCESS_DENIED = баланс исчерпан (policy rule: create_floatingip)
                self.access_denied_projects.add(project.project_name)
                project.disabled = True  # Отключаем проект — баланс пуст
                self.last_error = "ACCESS_DENIED"
                self.log(f"ACCESS_DENIED: {project.project_name} (баланс исчерпан)")
                
                emit_event({
                    "type": "error",
                    "account_id": self.account_db_id,
                    "error_type": "ACCESS_DENIED",
                    "project": project.project_name,
                    "project_db_id": project.project_db_id,
                    "message": f"Balance depleted for {project.project_name} — top up required",
                })
                
                # Проверяем, все ли проекты получили ACCESS_DENIED
                all_denied = all(
                    p.disabled or p.project_name in self.access_denied_projects
                    for p in self.account.projects
                )
                if all_denied:
                    self.last_error = "BALANCE_DEPLETED"
                    self.log("BALANCE_DEPLETED: all projects denied — stopping")
                    emit_event({
                        "type": "error",
                        "account_id": self.account_db_id,
                        "error_type": "BALANCE_DEPLETED",
                        "message": f"All projects for {self.account.email} have balance depleted",
                    })
                    self.running = False
                    break
                else:
                    # Переключаемся на следующий проект
                    self.current_project_idx = (self.current_project_idx + 1) % len(self.account.projects)
                    self.log_full("Switching to next project after ACCESS_DENIED")
                    await asyncio.sleep(2)
                    continue
            
            elif "AUTH_ERROR" in status or status == "ACCESS_DENIED_403":
                self.consecutive_auth_errors += 1
                max_auth_errors = self.config.get("max_auth_errors", 3)
                self.log(f"AUTH_ERROR ({self.consecutive_auth_errors}/{max_auth_errors})")
                self.last_error = status
                emit_event({
                    "type": "error",
                    "account_id": self.account_db_id,
                    "error_type": "AUTH_ERROR",
                    "project": project.project_name,
                    "message": f"Auth error ({self.consecutive_auth_errors}/{max_auth_errors})",
                })
                if self.consecutive_auth_errors >= max_auth_errors:
                    self.log("ACCOUNT_BLOCKED")
                    emit_event({
                        "type": "error",
                        "account_id": self.account_db_id,
                        "error_type": "ACCOUNT_BLOCKED",
                        "message": f"Account {self.account.email} blocked after {max_auth_errors} auth errors",
                    })
                    self.running = False
                    break
            
            elif status == "NO_MORE_IP":
                self.consecutive_auth_errors = 0
                # INFO: логируем каждую 10-ю попытку, FULL: каждую
                if self.log_level == "full":
                    self.log(f"#{self.total_attempts} | POOL_EMPTY | Next: {next_attempt_str}")
                elif self.total_attempts % 10 == 0:
                    self.log(f"#{self.total_attempts} | POOL_EMPTY | Next: {next_attempt_str}")
            
            else:
                # Для других статусов (CONFLICT, HTTP_xxx и т.д.)
                if "TIMEOUT" in status or "CONNECTION" in status:
                    self.consecutive_proxy_errors += 1
                    self.last_error = status
                    self.log(f"#{self.total_attempts} | {status}")
                    if self.consecutive_proxy_errors >= 5:
                        alive, _ = await self.verify_proxy()
                        if not alive:
                            recovered = await self._wait_for_proxy_recovery()
                            if not recovered:
                                break
                        else:
                            self.consecutive_proxy_errors = 0
                    await asyncio.sleep(5)
                    continue
                
                # Неизвестная ошибка - всегда логируем
                self.log(f"#{self.total_attempts} | {status} | Next: {next_attempt_str}")
                self.last_error = status
            
            # Emit periodic status event (every 10 attempts)
            if self.total_attempts % 10 == 0:
                emit_event({
                    "type": "status",
                    "account_id": self.account_db_id,
                    "status": "running",
                    "attempts": self.total_attempts,
                    "caught": self.total_caught,
                    "released": self.total_released,
                })
            
            await self._pause(delay)

        self.log(
            f"FINISHED | Attempts: {self.total_attempts} | Caught: {self.total_caught} "
            f"| Released: {self.total_released}"
        )
        emit_event({
            "type": "account_finished",
            "account_id": self.account_db_id,
            "reason": self.last_error or "FINISHED",
            "attempts": self.total_attempts,
            "caught": self.total_caught,
            "released": self.total_released,
        })
        return self.total_caught


async def run_sniper(
    profiles: list[dict],
    subnet_name: str,
    config: dict = None,
    log_callback: Callable[[str], None] = None,
    on_ip_caught: Callable[[str, str, str], None] = None,
    check_server: str = None,
) -> dict:
    """
    Запуск снайпера с профилями.
    
    Args:
        profiles: Список профилей вида:
            [{"project_id": "...", "project_name": "...", "username": "...", "password": "...", "proxy": "..."}]
        subnet_name: Имя подсети (например "ext-sub26")
        config: Конфигурация (опционально)
        log_callback: Колбэк для логов
        on_ip_caught: Колбэк при поимке IP (ip, project_name, email)
        check_server: Сервер для проверки прокси (опционально)
    
    Returns:
        {"total_attempts": int, "total_caught": int, "results": [...]}
    """
    config = {**DEFAULT_CONFIG, **(config or {})}
    log_fn = log_callback or (lambda msg: print(f"{timestamp()} {msg}"))
    
    # Группируем профили по email (username)
    accounts_dict: dict[str, list[dict]] = {}
    for profile in profiles:
        email = profile.get("username", "")
        if email not in accounts_dict:
            accounts_dict[email] = []
        accounts_dict[email].append(profile)
    
    # Создаём аккаунты
    accounts: list[Account] = []
    for idx, (email, profs) in enumerate(accounts_dict.items()):
        projects = [
            Project(
                project_id=p.get("project_id", ""),
                project_name=p.get("project_name", ""),
                username=p.get("username", ""),
                password=p.get("password", ""),
                proxy=p.get("proxy"),
                account_db_id=p.get("account_db_id"),
                project_db_id=p.get("project_db_id"),
            )
            for p in profs
        ]
        
        acc = Account(
            email=email,
            projects=projects,
            account_id=idx,
            fingerprint=generate_fingerprint(idx),
        )
        accounts.append(acc)
    
    log_fn(f"Loaded {len(accounts)} accounts with {sum(len(a.projects) for a in accounts)} projects")
    
    is_full_log = config.get("log_level", "info") == "full"
    
    if is_full_log:
        log_fn("[INIT] Log level: FULL - detailed logging enabled")
        log_fn(f"[INIT] Config: {json.dumps({k: v for k, v in config.items() if k != 'log_level'}, indent=2)}")
    
    # Верификация прокси
    for acc in accounts:
        raw_proxy = acc.projects[0].proxy if acc.projects else None
        
        if is_full_log:
            log_fn(f"[ACC {acc.account_id}] Verifying proxy for {acc.email}...")
            log_fn(f"[ACC {acc.account_id}] Raw proxy: {raw_proxy[:50] + '...' if raw_proxy and len(raw_proxy) > 50 else raw_proxy}")
        
        if not raw_proxy:
            if config.get("proxy_required", True):
                log_fn(f"[ACC {acc.account_id}] {acc.email}: NO_PROXY - disabled")
                for proj in acc.projects:
                    proj.disabled = True
            else:
                log_fn(f"[ACC {acc.account_id}] {acc.email}: DIRECT_CONNECTION")
                acc.proxy_verified = True
                acc.session = create_session(acc, config.get("insecure_ssl", False))
            continue
        
        proxy_dict, status = normalize_proxy(
            raw_proxy,
            insecure_ssl=config.get("insecure_ssl", False),
            check_server=check_server,
        )
        
        if proxy_dict:
            acc.proxy_url = proxy_to_url(proxy_dict, with_dns=True)
            acc.proxy_verified = True
            acc.verified_ip = proxy_dict.get('verified_ip')
            acc.session = create_session(acc, config.get("insecure_ssl", False))
            log_fn(f"[ACC {acc.account_id}] {acc.email}: PROXY_OK ({acc.verified_ip})")
            if is_full_log:
                log_fn(f"[ACC {acc.account_id}] Proxy URL: {acc.proxy_url.split('@')[-1] if '@' in acc.proxy_url else acc.proxy_url}")
        else:
            log_fn(f"[ACC {acc.account_id}] {acc.email}: PROXY_FAILED - {status}")
            if is_full_log:
                log_fn(f"[ACC {acc.account_id}] Proxy verification error details: {status}")
            acc.proxy_verified = False
            for proj in acc.projects:
                proj.disabled = True
    
    # Фильтруем активные аккаунты
    active_accounts = [
        acc for acc in accounts
        if acc.proxy_verified and not all(p.disabled for p in acc.projects)
    ]
    
    if not active_accounts:
        log_fn("No active accounts available")
        return {"total_attempts": 0, "total_caught": 0, "results": []}
    
    start_delay_max = config.get("start_delay_max", 60)
    log_fn(f"Starting {len(active_accounts)} snipers for subnet {subnet_name} (start delay: 1-{start_delay_max}s)")
    
    # Запуск воркеров
    workers: list[SniperWorker] = []
    tasks = []
    
    for acc in active_accounts:
        def make_callback(account):
            def callback(ip, project_name):
                if on_ip_caught:
                    on_ip_caught(ip, project_name, account.email)
            return callback
        
        worker = SniperWorker(
            account=acc,
            subnet_name=subnet_name,
            config=config,
            log_callback=log_fn,
            on_ip_caught=make_callback(acc),
        )
        workers.append(worker)
        tasks.append(asyncio.create_task(worker.run()))
    
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        for worker in workers:
            worker.stop()
        results = []
    
    # Закрываем сессии
    for acc in accounts:
        if acc.session:
            acc.session.close()
    
    total_caught = sum(r for r in results if isinstance(r, int))
    total_attempts = sum(w.total_attempts for w in workers)
    
    log_fn(f"COMPLETED | Total attempts: {total_attempts} | Total caught: {total_caught}")
    
    return {
        "total_attempts": total_attempts,
        "total_caught": total_caught,
        "results": [
            {
                "account_id": w.account.account_id,
                "email": w.account.email,
                "attempts": w.total_attempts,
                "caught": w.total_caught,
            }
            for w in workers
        ],
    }


# ═══════════════════════════════════════════════════════════════════════
# CLI режим (вызывается из backend_service.py с --auto)
# ═══════════════════════════════════════════════════════════════════════
def load_profiles_from_config() -> list[dict]:
    """Загрузка профилей из profiles.conf"""
    if not CONFIG_FILE.exists():
        return []
    
    profiles = []
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            # Новый формат: project_id|project_name|email|proxy|password|account_db_id|project_db_id
            if len(parts) == 7:
                project_id, project_name, username, proxy, password, account_db_id, project_db_id = parts
                profiles.append({
                    "project_id": project_id,
                    "project_name": project_name,
                    "username": username,
                    "password": password,
                    "proxy": proxy if proxy else None,
                    "account_db_id": int(account_db_id) if account_db_id else None,
                    "project_db_id": int(project_db_id) if project_db_id else None,
                })
            # Старый формат для совместимости: project_id|project_name|username|proxy|password
            elif len(parts) == 5:
                project_id, project_name, username, proxy, password = parts
                profiles.append({
                    "project_id": project_id,
                    "project_name": project_name,
                    "username": username,
                    "password": password,
                    "proxy": proxy if proxy else None,
                    "account_db_id": None,
                    "project_db_id": None,
                })
    return profiles


async def main_auto():
    """Автоматический режим — использует TARGET_SUBNETS и profiles.conf"""
    LOG_DIR.mkdir(exist_ok=True)
    
    profiles = load_profiles_from_config()
    if not profiles:
        print("No profiles in profiles.conf")
        sys.exit(1)
    
    # Группируем по username (email)
    accounts_by_email: dict[str, list[dict]] = {}
    for p in profiles:
        email = p["username"]
        if email not in accounts_by_email:
            accounts_by_email[email] = []
        accounts_by_email[email].append(p)
    
    subnet_list = list(TARGET_SUBNETS.keys())
    if not subnet_list:
        print("No target subnets configured")
        sys.exit(1)
    
    print(f"Loaded {len(profiles)} profiles in {len(accounts_by_email)} accounts")
    print(f"Target subnets: {', '.join(f'ext-sub{s}' for s in subnet_list)}")
    print(f"Start delay: 1-{START_DELAY_MAX}s (random per account)")
    print()
    
    # Запускаем по одному воркеру на каждый аккаунт
    # Распределяем подсети между аккаунтами
    config = _build_config()
    
    all_tasks = []
    workers = []
    
    for idx, (email, profs) in enumerate(accounts_by_email.items()):
        subnet_num = subnet_list[idx % len(subnet_list)]
        subnet_name = f"ext-sub{subnet_num}"
        
        # Создаём аккаунт
        projects = [
            Project(
                project_id=p["project_id"],
                project_name=p["project_name"],
                username=p["username"],
                password=p["password"],
                proxy=p.get("proxy"),
                account_db_id=p.get("account_db_id"),
                project_db_id=p.get("project_db_id"),
            )
            for p in profs
        ]
        
        acc = Account(
            email=email,
            projects=projects,
            account_id=idx,
            fingerprint=generate_fingerprint(idx),
        )
        
        # Проверяем прокси
        raw_proxy = profs[0].get("proxy") if profs else None
        if raw_proxy:
            proxy_dict, status = normalize_proxy(raw_proxy, insecure_ssl=False)
            if proxy_dict:
                acc.proxy_url = proxy_to_url(proxy_dict, with_dns=True)
                acc.proxy_verified = True
                acc.verified_ip = proxy_dict.get('verified_ip')
                acc.session = create_session(acc, insecure_ssl=False)
                print(f"[ACC {idx}] {email}: PROXY_OK ({acc.verified_ip}) -> {subnet_name}")
            else:
                print(f"[ACC {idx}] {email}: PROXY_FAILED - {status}")
                continue
        elif not PROXY_REQUIRED:
            acc.proxy_verified = True
            acc.session = create_session(acc, insecure_ssl=False)
            print(f"[ACC {idx}] {email}: DIRECT -> {subnet_name}")
        else:
            print(f"[ACC {idx}] {email}: NO_PROXY - skipped")
            continue
        
        worker = SniperWorker(
            account=acc,
            subnet_name=subnet_name,
            config=config,
            log_callback=lambda msg: print(msg),
            on_ip_caught=None,  # Telegram уведомление уже отправляется в SniperWorker.run()
        )
        workers.append(worker)
        all_tasks.append(asyncio.create_task(worker.run()))
    
    if not all_tasks:
        print("No active accounts")
        sys.exit(1)
    
    print()
    print(f"Starting {len(all_tasks)} snipers...")
    print()
    
    # Graceful shutdown
    shutdown_event = asyncio.Event()
    
    def signal_handler():
        print("\nStopping...")
        shutdown_event.set()
        for w in workers:
            w.stop()
    
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Windows
            signal.signal(sig, lambda s, f: signal_handler())
    
    try:
        results = await asyncio.gather(*all_tasks, return_exceptions=True)
        total_caught = sum(r for r in results if isinstance(r, int))
        total_attempts = sum(w.total_attempts for w in workers)
        print()
        print(f"FINISHED | Attempts: {total_attempts} | Caught: {total_caught}")
    except asyncio.CancelledError:
        pass
    finally:
        for acc_dict in accounts_by_email.values():
            pass  # sessions already closed by workers


async def main_single_account():
    """Режим одного аккаунта — получает JSON конфигурацию из stdin"""
    # Читаем JSON из stdin
    raw_input = sys.stdin.read().strip()
    if not raw_input:
        print("ERROR: No JSON config on stdin", flush=True)
        sys.exit(1)
    
    try:
        account_config = json.loads(raw_input)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON: {e}", flush=True)
        sys.exit(1)
    
    account_db_id = account_config.get("account_id", 0)
    email = account_config.get("email", "unknown")
    password = account_config.get("password", "")
    proxy = account_config.get("proxy")
    projects_data = account_config.get("projects", [])
    config = account_config.get("config", {})
    target_subnets = account_config.get("target_subnets", {})
    
    # Override global TARGET_SUBNETS if provided
    global TARGET_SUBNETS
    if target_subnets:
        TARGET_SUBNETS = {int(k): v for k, v in target_subnets.items()}
    
    if not projects_data:
        print(f"ERROR: No projects for account {email}", flush=True)
        sys.exit(1)
    
    # Merge config
    merged_config = {**_build_config(), **config}
    
    # Build projects
    projects = [
        Project(
            project_id=p.get("project_id", ""),
            project_name=p.get("project_name", ""),
            username=email,
            password=password,
            proxy=proxy,
            account_db_id=account_db_id,
            project_db_id=p.get("project_db_id"),
        )
        for p in projects_data
    ]
    
    acc = Account(
        email=email,
        projects=projects,
        account_id=account_db_id,
        fingerprint=generate_fingerprint(account_db_id),
    )
    
    # Verify proxy
    if proxy:
        proxy_dict, status = normalize_proxy(
            proxy,
            insecure_ssl=merged_config.get("insecure_ssl", False),
        )
        if proxy_dict:
            acc.proxy_url = proxy_to_url(proxy_dict, with_dns=True)
            acc.proxy_verified = True
            acc.verified_ip = proxy_dict.get('verified_ip')
            acc.session = create_session(acc, merged_config.get("insecure_ssl", False))
            print(f"[ACC {account_db_id}] {email}: PROXY_OK ({acc.verified_ip})", flush=True)
        else:
            print(f"[ACC {account_db_id}] {email}: PROXY_FAILED - {status}", flush=True)
            emit_event({
                "type": "error",
                "account_id": account_db_id,
                "error_type": "PROXY_FAILED",
                "message": f"Proxy verification failed: {status}",
            })
            emit_event({
                "type": "account_finished",
                "account_id": account_db_id,
                "reason": "PROXY_FAILED",
                "attempts": 0,
                "caught": 0,
            })
            sys.exit(1)
    elif not merged_config.get("proxy_required", True):
        acc.proxy_verified = True
        acc.session = create_session(acc, merged_config.get("insecure_ssl", False))
        print(f"[ACC {account_db_id}] {email}: DIRECT_CONNECTION", flush=True)
    else:
        print(f"[ACC {account_db_id}] {email}: NO_PROXY - disabled", flush=True)
        emit_event({
            "type": "error",
            "account_id": account_db_id,
            "error_type": "NO_PROXY",
            "message": "Proxy required but not configured",
        })
        emit_event({
            "type": "account_finished",
            "account_id": account_db_id,
            "reason": "NO_PROXY",
            "attempts": 0,
            "caught": 0,
        })
        sys.exit(1)
    
    # Determine subnet
    subnet_list = list(TARGET_SUBNETS.keys())
    if not subnet_list:
        print("ERROR: No target subnets configured", flush=True)
        sys.exit(1)
    
    # Круг начинается со «своей» подсети аккаунта — так аккаунты не бьют в одну подсеть одновременно
    own_index = account_db_id % len(subnet_list)
    subnet_names = [f"ext-sub{num}" for num in subnet_list[own_index:] + subnet_list[:own_index]]
    if merged_config.get("subnet_mode") != SUBNET_MODE_ROTATE:
        subnet_names = subnet_names[:1]

    worker = SniperWorker(
        account=acc,
        subnet_name=subnet_names[0],
        subnet_names=subnet_names,
        config=merged_config,
        log_callback=lambda msg: print(msg, flush=True),
    )
    
    # Graceful shutdown
    def signal_handler():
        worker.stop()
    
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            signal.signal(sig, lambda s, f: signal_handler())
    
    max_retries = 5
    retry_count = 0
    
    try:
        while retry_count < max_retries:
            try:
                await worker.run()
                break  # Normal exit
            except asyncio.CancelledError:
                break
            except Exception as e:
                retry_count += 1
                print(f"FATAL_ERROR #{retry_count}: {type(e).__name__}: {e}", flush=True)
                emit_event({
                    "type": "error",
                    "account_id": account_db_id,
                    "error_type": "FATAL_CRASH",
                    "message": f"{type(e).__name__}: {str(e)[:200]}",
                })
                if retry_count < max_retries:
                    print(f"Restarting worker in 30s... ({retry_count}/{max_retries})", flush=True)
                    try:
                        await asyncio.sleep(30)
                    except asyncio.CancelledError:
                        break
                    if worker.stop_requested:
                        break
                    # Reset worker state for retry
                    worker.running = True
                    worker.consecutive_auth_errors = 0
                    worker.consecutive_proxy_errors = 0
                    worker.last_error = ""
                else:
                    print(f"Max retries ({max_retries}) reached, exiting", flush=True)
                    emit_event({
                        "type": "account_finished",
                        "account_id": account_db_id,
                        "reason": "FATAL_CRASH",
                        "attempts": worker.total_attempts,
                        "caught": worker.total_caught,
                    })
    finally:
        if acc.session:
            acc.session.close()
        await close_panel_session()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="VK Cloud IP Sniper Worker")
    parser.add_argument("--auto", "-a", action="store_true", help="Auto mode (use TARGET_SUBNETS and profiles.conf)")
    parser.add_argument("--single-account", action="store_true", help="Single account mode (reads JSON from stdin)")
    parser.add_argument("--config", "-c", help="Path to profiles.conf (manual mode)")
    parser.add_argument("--subnet", "-s", help="Subnet name, e.g. ext-sub26 (manual mode)")
    parser.add_argument("--insecure", "-k", action="store_true", help="Disable SSL verification")
    parser.add_argument("--check-server", help="Custom server for proxy check")
    args = parser.parse_args()
    
    if args.single_account:
        # Режим одного аккаунта — вызывается из панели для per-account управления
        try:
            asyncio.run(main_single_account())
        except KeyboardInterrupt:
            print("\nStopped")
    elif args.auto:
        # Автоматический режим — вызывается из панели (legacy)
        try:
            asyncio.run(main_auto())
        except KeyboardInterrupt:
            print("\nStopped")
    else:
        # Ручной режим
        config_file = Path(args.config) if args.config else CONFIG_FILE
        
        if not config_file.exists():
            print(f"Config file not found: {config_file}")
            sys.exit(1)
        
        profiles = load_profiles_from_config() if config_file == CONFIG_FILE else []
        if not profiles and args.config:
            with open(config_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("|")
                    if len(parts) != 5:
                        continue
                    project_id, project_name, username, proxy, password = parts
                    profiles.append({
                        "project_id": project_id,
                        "project_name": project_name,
                        "username": username,
                        "password": password,
                        "proxy": proxy if proxy else None,
                    })
        
        if not profiles:
            print("No profiles loaded")
            sys.exit(1)
        
        subnet = args.subnet or f"ext-sub{list(TARGET_SUBNETS.keys())[0]}"
        print(f"Loaded {len(profiles)} profiles, target: {subnet}")
        
        config = {"insecure_ssl": args.insecure}
        
        try:
            result = asyncio.run(run_sniper(
                profiles=profiles,
                subnet_name=subnet,
                config=config,
                check_server=args.check_server,
            ))
            print(f"\nResult: {json.dumps(result, indent=2)}")
        except KeyboardInterrupt:
            print("\nStopped")
