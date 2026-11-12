#!/usr/bin/env python3
"""
Cloud.ru (Advanced edition) IP Hunter Worker.

Subprocess-воркер: читает JSON из stdin, в бесконечном цикле создаёт
публичные IP (EIP) пачками до квоты, ждёт выделения, классифицирует по
whitelist CIDR (HIT — оставляем, MISS — удаляем). Все попадания репортятся
обратно в панель через /cloud/adv/internal/caught-ip, плюс Telegram; все
выданные за раунд адреса — через /cloud/adv/internal/received-ips для статистики.

Advanced построен на Huawei Cloud Stack: регион один (ru-moscow-1), запросы
подписываются ключом доступа AK/SK (SDK-HMAC-SHA256), адреса — VPC API v1
/v1/{project_id}/publicips. Цикл повторяет cloud_evo_worker.
"""

import argparse
import hashlib
import hmac
import ipaddress
import json
import random
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, unquote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ═══════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════

CLOUD_ADV_REGION = "ru-moscow-1"
CLOUD_ADV_VPC_URL = f"https://vpc.{CLOUD_ADV_REGION}.hc.sbercloud.ru"

# Единственный тип адреса в Advanced. Полоса на каждый EIP обязательна:
# минимальная выделенная с оплатой по трафику — неприкреплённый адрес
# тарифицируется только арендой
EIP_TYPE = "5_bgp"
EIP_BANDWIDTH = {"size": 1, "share_type": "PER", "charge_mode": "traffic"}

BUSY_STATUSES = {
    "PENDING_CREATE", "PENDING_UPDATE", "PENDING_DELETE",
    "NOTIFYING", "NOTIFY_DELETE", "BINDING",
}

SIGN_ALGORITHM = "SDK-HMAC-SHA256"
SIGN_DATE_FORMAT = "%Y%m%dT%H%M%SZ"

# Антифлуд Cloud.ru: пауза-откат с удвоением на каждом подряд срабатывании
RATE_LIMIT_COOLDOWN_BASE = 20.0
RATE_LIMIT_COOLDOWN_MAX = 180.0

REPORT_TIMEOUT = 10.0
TELEGRAM_TIMEOUT = 10.0


# ═══════════════════════════════════════════════════════════════════════
# PROXY PARSER (зеркало cloud_adv_backend_service)
# ═══════════════════════════════════════════════════════════════════════

def _parse_proxy(proxy_str: str) -> Optional[str]:
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
# AK/SK SIGNING (зеркало cloud_adv_backend_service)
# ═══════════════════════════════════════════════════════════════════════

def sign_request(
    access_key: str, secret_key: str, method: str, url: str,
    body: bytes, content_type: Optional[str], project_id: str,
    security_token: Optional[str] = None,
) -> dict:
    """Заголовки запроса с подписью SDK-HMAC-SHA256 (алгоритм apig signer Cloud.ru Advanced).
    Запросы воркера идут без query-строки, поэтому её каноническая форма пустая.
    X-Project-Id и STS-токен временного ключа (X-Security-Token) кладутся до подписи и
    входят в подписанные заголовки, как в huaweicloudsdkcore.BasicCredentials: без
    X-Project-Id временный ключ получает 401 VPC.0008 «Invalid token in the header»."""
    parsed = urlparse(url)
    headers = {
        "host": parsed.netloc,
        "x-sdk-date": datetime.now(timezone.utc).strftime(SIGN_DATE_FORMAT),
        "x-project-id": project_id,
    }
    if content_type:
        headers["content-type"] = content_type
    if security_token:
        headers["x-security-token"] = security_token
    signed_names = sorted(headers)

    # Канонический путь для подписи всегда со слэшем в конце, в самом запросе — без него
    path = "/".join(quote(part, safe="~") for part in unquote(parsed.path).split("/"))
    if not path.endswith("/"):
        path += "/"

    canonical_request = "\n".join([
        method.upper(),
        path,
        "",
        "".join(f"{name}:{headers[name].strip()}\n" for name in signed_names),
        ";".join(signed_names),
        hashlib.sha256(body).hexdigest(),
    ])
    string_to_sign = "\n".join([
        SIGN_ALGORITHM,
        headers["x-sdk-date"],
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(
        secret_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256,
    ).hexdigest()

    result = {
        "Host": headers["host"],
        "X-Sdk-Date": headers["x-sdk-date"],
        "X-Project-Id": project_id,
        "Authorization": (
            f"{SIGN_ALGORITHM} Access={access_key}, "
            f"SignedHeaders={';'.join(signed_names)}, Signature={signature}"
        ),
    }
    if content_type:
        result["Content-Type"] = content_type
    if security_token:
        result["X-Security-Token"] = security_token
    return result


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════

def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def emit_event(event: dict) -> None:
    try:
        print(f"EVENT:{json.dumps(event, ensure_ascii=False)}", flush=True)
    except Exception:
        pass


def jittered(base: float, jitter: float) -> float:
    return max(0.0, base + random.uniform(-jitter, jitter))


def in_whitelist(ip: Optional[str], nets: list) -> Optional[ipaddress.IPv4Network]:
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for net in nets:
        if addr in net:
            return net
    return None


# ═══════════════════════════════════════════════════════════════════════
# FRIENDLY ERRORS (зеркало cloud_adv_backend_service)
# {target} — «Адрес 1.2.3.4», если запрашивался конкретный, иначе просто «Адрес»
# ═══════════════════════════════════════════════════════════════════════

_KEY_REJECTED = "Ключ не принят — временный ключ истёк или AK, SK и STS Token из разных генераций. Сгенерируйте новый и обновите аккаунт"
_QUOTA_EXCEEDED = "Квота на публичные IP исчерпана — удалите лишние адреса в проекте или попросите поддержку Cloud.ru поднять лимит"
_POOL_EMPTY = "У Cloud.ru закончились свободные публичные адреса — попробуйте позже"
_RATE_LIMITED = "Cloud.ru временно ограничил частоту запросов (антифлуд). Увеличьте задержку Burst или сузьте список"

_NEUTRON_ERRORS = {
    "IpAddressInUse": "{target} занят — его использует другой клиент Cloud.ru",
    "InvalidIpForNetwork": "{target} не входит в пул публичных адресов Cloud.ru",
    "InvalidIpForSubnet": "{target} не входит в пул публичных адресов Cloud.ru",
    "IpAddressGenerationFailure": _POOL_EMPTY,
    "OverQuota": _QUOTA_EXCEEDED,
}

_VPC_ERRORS = {
    "VPC.0521": _QUOTA_EXCEEDED,
    "VPC.0532": _POOL_EMPTY,
    "VPC.0502": "Аккаунт Cloud.ru ограничен — проверьте баланс",
    "VPC.2701": "Нет прав на публичные IP или не хватает средств на балансе",
    "VPC.0008": _KEY_REJECTED,
    "APIGW.0301": _KEY_REJECTED,
    "VPC.0514": _RATE_LIMITED,
    "SYS.0429": _RATE_LIMITED,
    "APIGW.0308": _RATE_LIMITED,
}

# Классификация по коду, а не по статусу/подстроке: антифлуд приходит и со статусом
# 403, и «429» внутри кода «SYS.0429» — иначе он уехал бы в auth или в квоту
_QUOTA_CODES = {"VPC.0521"}
_RATE_LIMIT_CODES = {"VPC.0514", "SYS.0429", "APIGW.0308"}
_AUTH_CODES = {"VPC.0008", "APIGW.0301"}


def parse_error(r: requests.Response) -> tuple:
    """(code, neutron_type, message). VPC отвечает {code, message}, шлюз и IAM —
    {error_code, error_msg}; у Neutron и у шлюза бывает JSON-строка внутри message —
    из неё достаём вложенный тип/код и чистый текст."""
    try:
        data = r.json()
    except ValueError:
        return "", "", r.text[:200]
    if not isinstance(data, dict):
        return "", "", r.text[:200]
    code = data.get("code") or data.get("error_code") or ""
    message = data.get("message") or data.get("error_msg") or r.text[:200]
    neutron_type = ""
    if isinstance(message, str) and message.lstrip().startswith("{"):
        try:
            inner = json.loads(message)
            neutron = inner.get("NeutronError") or {}
            neutron_type = neutron.get("type") or ""
            message = neutron.get("message") or inner.get("error_msg") or message
        except (ValueError, AttributeError):
            pass
    return code, neutron_type, str(message)[:200]


def classify_error(e: "CloudAdvAPIError") -> str:
    """auth | rate_limit | quota | server | address. Код важнее статуса: антифлуд бывает
    со статусом 403 — проверь его раньше auth, иначе воркер встанет как от плохого ключа."""
    if e.code in _RATE_LIMIT_CODES or e.status_code == 429:
        return "rate_limit"
    if e.code in _QUOTA_CODES:
        return "quota"
    if e.code in _AUTH_CODES or e.status_code in (401, 403):
        return "auth"
    if e.status_code >= 500:
        return "server"
    return "address"


def friendly_error(r: requests.Response, ip_address: Optional[str] = None) -> str:
    code, neutron_type, message = parse_error(r)
    tag = code or f"HTTP {r.status_code}"
    template = _NEUTRON_ERRORS.get(neutron_type) or _VPC_ERRORS.get(code)
    if not template and r.status_code == 429:
        template = _RATE_LIMITED
    if not template and r.status_code in (401, 403):
        template = _KEY_REJECTED
    if template:
        target = f"Адрес {ip_address}" if ip_address else "Адрес"
        return f"{template.format(target=target)} ({tag})"
    return f"Cloud.ru отказал ({tag}): {message}"


def describe(e: Exception) -> str:
    """Текст для лога и панели: понятный для отказа Cloud.ru, как есть для сетевых сбоев."""
    return getattr(e, "friendly", None) or str(e)


# ═══════════════════════════════════════════════════════════════════════
# DATACLASSES
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class CloudAdvConfig:
    delay_burst: float = 0.5
    delay_round: float = 8.0
    delay_delete: float = 3.0
    jitter: float = 2.0
    provision_timeout: int = 60
    provision_poll: float = 2.0
    proxy_required: bool = False
    log_level: str = "info"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # "random" — брать что выдаст облако и отбирать по whitelist;
    # "targeted" — запрашивать конкретные адреса из whitelist напрямую
    mode: str = "random"


@dataclass
class CloudAdvAccount:
    account_id: int
    label: str
    key_id: str
    key_secret: str
    project_id: str
    proxy: Optional[str] = None
    key_token: Optional[str] = None
    whitelist_cidrs: list = field(default_factory=list)
    panel_base_url: str = ""


# ═══════════════════════════════════════════════════════════════════════
# CLOUD.RU ADVANCED API CLIENT
# ═══════════════════════════════════════════════════════════════════════

class CloudAdvAPIError(RuntimeError):
    """friendly — текст для лога и панели; code/status_code — для классификации."""

    def __init__(self, status_code: int, message: str, friendly: str = "", code: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.friendly = friendly or message
        self.code = code


def api_error(r: requests.Response, action: str, ip_address: Optional[str] = None) -> CloudAdvAPIError:
    code, _neutron, _msg = parse_error(r)
    return CloudAdvAPIError(
        r.status_code, f"{action} {r.status_code}: {r.text[:500]}",
        friendly_error(r, ip_address), code,
    )


class CloudAdvAPI:

    def __init__(self, key_id: str, key_secret: str, project_id: str,
                 proxy_str: Optional[str] = None, key_token: Optional[str] = None):
        self.key_id = key_id
        self.key_secret = key_secret
        self.key_token = key_token or None
        self.project_id = project_id
        self.base = f"{CLOUD_ADV_VPC_URL}/v1/{project_id}/publicips"

        self.session = requests.Session()
        if proxy_str:
            proxy_url = _parse_proxy(proxy_str)
            if proxy_url:
                self.session.proxies = {"http": proxy_url, "https": proxy_url}

        retry = Retry(total=2, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update({"Accept": "application/json"})

    def _req(self, method: str, url: str, body: Optional[dict] = None) -> requests.Response:
        # Тело сериализуем сами: подписываются ровно те байты, что уйдут в запрос
        payload = json.dumps(body).encode("utf-8") if body is not None else b""
        headers = sign_request(
            self.key_id, self.key_secret, method, url, payload,
            "application/json" if body is not None else None,
            self.project_id, security_token=self.key_token,
        )
        return self.session.request(method, url, headers=headers, data=payload or None, timeout=60)

    def list_eips(self) -> list:
        r = self._req("GET", self.base)
        if r.status_code >= 400:
            raise api_error(r, "GET publicips")
        return r.json().get("publicips", [])

    def create_eip(self, name: str, ip_address: Optional[str] = None) -> dict:
        publicip = {"type": EIP_TYPE}
        if ip_address:
            publicip["ip_address"] = ip_address
        body = {
            "publicip": publicip,
            "bandwidth": {"name": name, **EIP_BANDWIDTH},
        }
        r = self._req("POST", self.base, body=body)
        if r.status_code >= 400:
            raise api_error(r, "POST publicips", ip_address)
        return r.json().get("publicip") or {}

    def get_eip(self, eip_id: str, allow_404: bool = False) -> Optional[dict]:
        r = self._req("GET", f"{self.base}/{eip_id}")
        if r.status_code == 404 and allow_404:
            return None
        if r.status_code >= 400:
            raise api_error(r, f"GET publicips/{eip_id}")
        return r.json().get("publicip")

    def delete_eip(self, eip_id: str) -> tuple:
        r = self._req("DELETE", f"{self.base}/{eip_id}")
        if r.status_code < 300 or r.status_code == 404:
            return True, ""
        return False, friendly_error(r)

    def wait_ready(self, eip_id: str, timeout: float, poll: float) -> tuple:
        deadline = time.time() + timeout
        last_ip, last_status = None, ""
        while time.time() < deadline:
            try:
                obj = self.get_eip(eip_id, allow_404=True)
            except Exception as e:
                log(f"  wait_ready get_eip error: {describe(e)}")
                time.sleep(poll)
                continue
            if obj is None:
                time.sleep(poll)
                continue
            last_ip = obj.get("public_ip_address") or last_ip
            last_status = (obj.get("status") or "").upper()
            if last_status == "ERROR":
                return last_ip, last_status
            if last_ip and last_status not in BUSY_STATUSES:
                return last_ip, last_status
            time.sleep(poll)
        return last_ip, last_status

    def safe_delete(self, eip_id: str, retry_pause_fn, timeout: float, poll: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                obj = self.get_eip(eip_id, allow_404=True)
            except Exception:
                obj = None
            if obj is None:
                time.sleep(poll)
                continue
            if (obj.get("status") or "").upper() not in BUSY_STATUSES:
                break
            time.sleep(poll)

        for i in range(5):
            ok, err = self.delete_eip(eip_id)
            if ok:
                return True
            log(f"  delete try {i+1}/5 failed: {err}")
            time.sleep(retry_pause_fn())
        return False


# ═══════════════════════════════════════════════════════════════════════
# WORKER
# ═══════════════════════════════════════════════════════════════════════

class CloudAdvWorker:

    def __init__(self, account: CloudAdvAccount, config: CloudAdvConfig):
        self.account = account
        self.config = config

        self.networks = []
        for cidr in account.whitelist_cidrs or []:
            try:
                self.networks.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                log(f"BAD CIDR in whitelist (ignored): {cidr}")

        self.api = CloudAdvAPI(
            key_id=account.key_id,
            key_secret=account.key_secret,
            project_id=account.project_id,
            proxy_str=account.proxy,
            key_token=account.key_token,
        )

        self.preexisting_ids: set = set()
        self.preexisting_ips: set = set()
        self.created_in_session: set = set()
        self.caught_ips: set = set()

        self.attempts: int = 0
        self.caught: int = 0
        self.round_no: int = 0
        self._stop_flag: bool = False
        self._auth_failed: bool = False

        self._report_session = requests.Session()
        self._report_session.headers.update({"Content-Type": "application/json"})

    def stop(self) -> None:
        self._stop_flag = True

    # ──────────────────────── reporting ────────────────────────

    def report_caught(self, ip: str, eip_id: str, subnet: str) -> None:
        self.caught += 1
        self.caught_ips.add(ip)

        emit_event({
            "type": "ip_caught",
            "account_id": self.account.account_id,
            "ip": ip,
            "eip_id": eip_id,
            "zone": CLOUD_ADV_REGION,
            "subnet": subnet,
            "total_caught": self.caught,
            "total_attempts": self.attempts,
        })

        if self.account.panel_base_url:
            try:
                self._report_session.post(
                    f"{self.account.panel_base_url.rstrip('/')}/cloud/adv/internal/caught-ip",
                    params={
                        "account_id": self.account.account_id,
                        "ip_address": ip,
                        "eip_id": eip_id,
                        "zone": CLOUD_ADV_REGION,
                    },
                    timeout=REPORT_TIMEOUT,
                )
            except Exception as e:
                log(f"  panel report error: {e}")

        if self.config.telegram_bot_token and self.config.telegram_chat_id:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage",
                    data={
                        "chat_id": self.config.telegram_chat_id,
                        "text": (
                            f"<b>Cloud.ru Advanced IP CAUGHT</b>\n\n"
                            f"IP: <b>{ip}</b>\n"
                            f"Subnet: {subnet}\n"
                            f"Region: {CLOUD_ADV_REGION}\n"
                            f"Account: {self.account.label} (id={self.account.account_id})\n"
                            f"Caught total: {self.caught}\n"
                            f"Attempts: {self.attempts}"
                        ),
                        "parse_mode": "HTML",
                    },
                    timeout=TELEGRAM_TIMEOUT,
                )
            except Exception as e:
                log(f"  telegram error: {e}")

    def report_received(self, ready: list) -> None:
        """Все выданные облаком адреса раунда — для статистики, не только попадания."""
        items = [
            {"ip": ip, "zone": CLOUD_ADV_REGION, "caught": in_whitelist(ip, self.networks) is not None}
            for _eip_id, ip, _status in ready
            if ip
        ]
        if not items or not self.account.panel_base_url:
            return
        try:
            self._report_session.post(
                f"{self.account.panel_base_url.rstrip('/')}/cloud/adv/internal/received-ips",
                json={"account_id": self.account.account_id, "items": items},
                timeout=REPORT_TIMEOUT,
            )
        except requests.RequestException as e:
            log(f"  panel stats report error: {e}")

    # ──────────────────────── main ────────────────────────

    def safe_to_delete(self, eip_id: str) -> bool:
        if eip_id in self.preexisting_ids:
            log(f"  SKIP DELETE: id={eip_id} был ДО запуска")
            return False
        if eip_id not in self.created_in_session:
            log(f"  SKIP DELETE: id={eip_id} не наш")
            return False
        try:
            cur = self.api.get_eip(eip_id, allow_404=True)
        except Exception as e:
            log(f"  SKIP DELETE: id={eip_id} get_eip error: {describe(e)}")
            return False
        if cur is None:
            log(f"  SKIP DELETE: id={eip_id} не виден")
            return False
        if cur.get("port_id"):
            log(f"  SKIP DELETE: id={eip_id} привязан к порту {cur.get('port_id')}")
            return False
        return True

    def _finish_account(self, reason: str) -> None:
        emit_event({
            "type": "account_finished",
            "account_id": self.account.account_id,
            "reason": reason,
            "attempts": self.attempts,
            "caught": self.caught,
        })

    def _emit_error(self, error_type: str, message: str) -> None:
        emit_event({
            "type": "error",
            "account_id": self.account.account_id,
            "error_type": error_type,
            "message": message[:200],
        })

    def _check_proxy_requirement(self) -> bool:
        if self.config.proxy_required and not self.account.proxy:
            log("NO_PROXY — proxy_required=True, выход")
            self._emit_error("NO_PROXY", "Proxy required but not configured")
            self._finish_account("NO_PROXY")
            return False
        return True

    def _initial_snapshot(self) -> bool:
        """Отдельного входа у AK/SK нет — первый подписанный запрос и проверяет ключ."""
        try:
            existing = self.api.list_eips()
        except CloudAdvAPIError as e:
            if e.status_code in (401, 403):
                log(f"AUTH failed: {e.friendly}")
                self._emit_error("AUTH_FAILED", e.friendly)
                self._finish_account("AUTH_FAILED")
                return False
            log(f"не получить список адресов проекта: {e.friendly}")
            self._emit_error("LIST_FAILED", e.friendly)
            self._finish_account("LIST_FAILED")
            return False
        except Exception as e:
            log(f"не получить список адресов проекта: {e}")
            self._emit_error("LIST_FAILED", str(e))
            self._finish_account("LIST_FAILED")
            return False

        log("auth ok")
        self.preexisting_ids = {e.get("id") for e in existing if e.get("id")}
        self.preexisting_ips = {e.get("public_ip_address") for e in existing if e.get("public_ip_address")}
        log(f"в проекте уже {len(self.preexisting_ids)} EIP (под защитой)")
        for e in existing:
            log(
                f"  PRE-EXISTING ip={e.get('public_ip_address')} id={e.get('id')} "
                f"status={e.get('status')} port={e.get('port_id')}"
            )
        return True

    def run(self) -> None:
        log(
            f"START account={self.account.account_id} ({self.account.label}) "
            f"region={CLOUD_ADV_REGION} mode={self.config.mode} whitelist={len(self.networks)} CIDR"
        )
        log(
            f"delays: burst={self.config.delay_burst}s round={self.config.delay_round}s "
            f"delete={self.config.delay_delete}s ±{self.config.jitter}"
        )

        if not self._check_proxy_requirement():
            return

        if not self._initial_snapshot():
            return

        try:
            self._loop()
        except Exception as e:
            log(f"FATAL_CRASH: {type(e).__name__}: {e}")
            self._emit_error("FATAL_CRASH", f"{type(e).__name__}: {str(e)[:200]}")
            self._finish_account("FATAL_CRASH")
            return

        if self._auth_failed:
            log(f"AUTH_FAILED attempts={self.attempts} caught={self.caught}")
            self._finish_account("AUTH_FAILED")
            return

        log(f"FINISHED attempts={self.attempts} caught={self.caught}")
        self._finish_account("STOPPED")

    def _loop(self) -> None:
        targeted = self.config.mode == "targeted"
        while not self._stop_flag:
            self.round_no += 1
            emit_event({
                "type": "round_start",
                "account_id": self.account.account_id,
                "round_no": self.round_no,
            })

            if targeted:
                did = self._targeted_round()
            else:
                log(f"=== раунд {self.round_no}: жмём POST до квоты ===")
                batch = self._fill_until_quota()
                if batch:
                    self._finish_round(batch)
                did = bool(batch)

            if self._auth_failed or self._stop_flag:
                break

            pause = jittered(self.config.delay_round, self.config.jitter)
            if did:
                log(f"раунд {self.round_no} закончен, пауза {pause:.1f}s")
            else:
                log(f"раунд {self.round_no}: ничего не создали, пауза {pause:.1f}s")
            self._sleep_interruptible(pause)

    def _candidate_ips(self):
        """Адреса whitelist-подсетей, которых у нас ещё нет (под точечный режим)."""
        held = self.preexisting_ips | self.caught_ips
        for net in self.networks:
            for addr in net.hosts():
                ip = str(addr)
                if ip not in held:
                    yield ip

    def _targeted_round(self) -> bool:
        """Запрашиваем конкретные адреса из whitelist по одному. Успешно взятый
        адрес в whitelist по построению — оставляем и репортим. Если облако
        проигнорировало запрошенный адрес и выдало не из whitelist — удаляем."""
        log(f"=== раунд {self.round_no}: точечный запрос адресов whitelist ===")
        created = 0
        tried = 0
        consec_errors = 0
        rl_streak = 0
        for ip in self._candidate_ips():
            if self._stop_flag:
                break

            tried += 1
            self.attempts += 1
            emit_event({"type": "ip_attempt", "account_id": self.account.account_id})
            name = f"a{random.randint(0, 0xFFFFFF):06x}"

            try:
                obj = self.api.create_eip(name, ip_address=ip)
            except CloudAdvAPIError as e:
                kind = classify_error(e)
                if kind == "auth":
                    log(f"  AUTH: {e.friendly}")
                    self._emit_error("AUTH_FAILED", e.friendly)
                    self._auth_failed = True
                    break
                if kind == "rate_limit":
                    # Антифлуд облака: откат с удвоением, не считаем к аварийному счётчику
                    rl_streak += 1
                    cooldown = min(RATE_LIMIT_COOLDOWN_BASE * (2 ** (rl_streak - 1)), RATE_LIMIT_COOLDOWN_MAX)
                    log(f"  {e.friendly} — пауза {cooldown:.0f}s (откат #{rl_streak})")
                    self._sleep_interruptible(jittered(cooldown, self.config.jitter))
                    continue
                if kind == "quota":
                    log(f"  {e.friendly} — раунд окончен")
                    emit_event({
                        "type": "quota_hit",
                        "account_id": self.account.account_id,
                        "batch_size": created,
                    })
                    break
                if kind == "server":
                    consec_errors += 1
                    rl_streak = 0
                    log(f"  error {ip}: {e.friendly}")
                    if consec_errors >= 5:
                        log("  слишком много подряд сбоев, прерываю раунд")
                        self._emit_error("CREATE_BURST_FAILED", e.friendly)
                        break
                    self._sleep_interruptible(jittered(self.config.delay_burst, self.config.jitter))
                    continue
                # address: занят другим / вне пула — обычный пропуск, не фатально
                consec_errors = 0
                rl_streak = 0
                log(f"  skip: {e.friendly}")
                self._sleep_interruptible(jittered(self.config.delay_burst, self.config.jitter))
                continue
            except Exception as e:
                # Сетевой сбой/прокси — вот это считаем, подряд много → прерываем раунд
                msg = str(e)
                consec_errors += 1
                log(f"  error {ip}: {msg[:160]}")
                if consec_errors >= 5:
                    log("  слишком много подряд сбоев, прерываю раунд")
                    self._emit_error("CREATE_BURST_FAILED", msg)
                    break
                self._sleep_interruptible(jittered(self.config.delay_burst, self.config.jitter))
                continue

            consec_errors = 0
            rl_streak = 0
            eip_id = obj.get("id")
            got_ip = obj.get("public_ip_address") or ip
            if not eip_id:
                log(f"  нет id в ответе: {obj}")
                continue
            self.created_in_session.add(eip_id)

            self.report_received([(eip_id, got_ip, "ACTIVE")])
            match = in_whitelist(got_ip, self.networks)
            if match:
                log(f"  GOT: {got_ip} → {match} → ОСТАВЛЯЮ id={eip_id}")
                self.report_caught(got_ip, eip_id, str(match))
                created += 1
            else:
                # Облако отдало не запрошенный адрес (параметр проигнорирован) — удаляем
                log(f"  запрошен {ip}, выдан {got_ip} вне whitelist → удаляю id={eip_id}")
                if self.safe_to_delete(eip_id):
                    time.sleep(jittered(self.config.delay_delete, self.config.jitter))
                    self.api.safe_delete(
                        eip_id,
                        lambda: jittered(self.config.delay_delete, self.config.jitter),
                        timeout=self.config.provision_timeout,
                        poll=self.config.provision_poll,
                    )

            self._sleep_interruptible(jittered(self.config.delay_burst, self.config.jitter))

        emit_event({
            "type": "round_complete",
            "account_id": self.account.account_id,
            "round_no": self.round_no,
            "kept": created,
            "deleted": 0,
            "total_attempts": self.attempts,
            "total_caught": self.caught,
        })
        return tried > 0

    def _finish_round(self, batch: list) -> None:
        # Остановка посреди раунда всё равно доводит его до конца: созданные
        # промахи иначе остались бы висеть в проекте и копить счёт
        if self._stop_flag:
            log(f"раунд {self.round_no}: остановка — доделываю раунд, создано {len(batch)} IP")

        log(f"раунд {self.round_no}: ждём выделения {len(batch)} IP")
        ready = self._wait_batch_ready(batch)

        kept, to_delete = self._classify(ready)
        log(f"раунд {self.round_no}: HITs={kept}, на удаление={len(to_delete)}")
        self.report_received(ready)

        self._delete_misses(to_delete)

        emit_event({
            "type": "round_complete",
            "account_id": self.account.account_id,
            "round_no": self.round_no,
            "kept": kept,
            "deleted": len(to_delete),
            "total_attempts": self.attempts,
            "total_caught": self.caught,
        })

    def _fill_until_quota(self) -> list:
        """Создаёт IP пачкой до получения ошибки квоты. Возвращает [eip_id, ...]."""
        batch = []
        consec_errors = 0
        rl_streak = 0
        while not self._stop_flag:
            self.attempts += 1
            emit_event({"type": "ip_attempt", "account_id": self.account.account_id})

            name = f"a{random.randint(0, 0xFFFFFF):06x}"

            try:
                obj = self.api.create_eip(name)
            except Exception as e:
                kind = classify_error(e) if isinstance(e, CloudAdvAPIError) else "network"
                if kind == "auth":
                    log(f"  AUTH: {e.friendly}")
                    self._emit_error("AUTH_FAILED", e.friendly)
                    self._auth_failed = True
                    break
                if kind == "rate_limit":
                    rl_streak += 1
                    cooldown = min(RATE_LIMIT_COOLDOWN_BASE * (2 ** (rl_streak - 1)), RATE_LIMIT_COOLDOWN_MAX)
                    log(f"  {e.friendly} — пауза {cooldown:.0f}s (откат #{rl_streak})")
                    self._sleep_interruptible(jittered(cooldown, self.config.jitter))
                    continue
                if kind == "quota":
                    log(f"  {describe(e)}")
                    log(f"  создано в пачке: {len(batch)}, переход к выделению")
                    emit_event({
                        "type": "quota_hit",
                        "account_id": self.account.account_id,
                        "batch_size": len(batch),
                    })
                    break
                rl_streak = 0
                consec_errors += 1
                log(f"  create error #{consec_errors}: {describe(e)}")
                if consec_errors >= 5:
                    log("  слишком много подряд create-ошибок, выход из пачки")
                    self._emit_error("CREATE_BURST_FAILED", describe(e))
                    break
                self._sleep_interruptible(jittered(self.config.delay_burst, self.config.jitter))
                continue

            consec_errors = 0
            rl_streak = 0
            eip_id = obj.get("id")
            if not eip_id:
                log(f"  нет id в ответе: {obj}")
                continue
            self.created_in_session.add(eip_id)
            batch.append(eip_id)
            log(f"  +created #{len(batch)} id={eip_id} ip={obj.get('public_ip_address')}")

            self._sleep_interruptible(jittered(self.config.delay_burst, self.config.jitter))

        return batch

    def _wait_batch_ready(self, batch: list) -> list:
        """Возвращает [(eip_id, ip, status), ...]."""
        ready = []
        for eip_id in batch:
            try:
                ip, status = self.api.wait_ready(
                    eip_id,
                    timeout=self.config.provision_timeout,
                    poll=self.config.provision_poll,
                )
            except Exception as e:
                log(f"  wait_ready {eip_id}: {describe(e)}")
                ip, status = None, "?"
            ready.append((eip_id, ip, status))
            log(f"  ready id={eip_id} ip={ip} status={status}")
        return ready

    def _classify(self, ready: list) -> tuple:
        kept = 0
        to_delete = []
        for eip_id, ip, _status in ready:
            match = in_whitelist(ip, self.networks)
            if match:
                kept += 1
                log(f"  HIT: {ip} → {match} → ОСТАВЛЯЮ id={eip_id}")
                self.report_caught(ip, eip_id, str(match))
            else:
                to_delete.append((eip_id, ip or "?"))
        return kept, to_delete

    def _delete_misses(self, to_delete: list) -> None:
        for eip_id, ip in to_delete:
            if not self.safe_to_delete(eip_id):
                continue
            # Темп удаления держим и при остановке — пачка DELETE подряд выглядит
            # для облака не так, как обычный раунд
            time.sleep(jittered(self.config.delay_delete, self.config.jitter))
            ok = self.api.safe_delete(
                eip_id,
                lambda: jittered(self.config.delay_delete, self.config.jitter),
                timeout=self.config.provision_timeout,
                poll=self.config.provision_poll,
            )
            if ok:
                log(f"  deleted id={eip_id} ip={ip}")
            else:
                log(f"  DELETE НЕ УДАЛСЯ id={eip_id} ip={ip}")

    def _sleep_interruptible(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._stop_flag:
                return
            time.sleep(min(0.5, deadline - time.time()))


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


def build_worker(data: dict) -> CloudAdvWorker:
    cfg_raw = data.get("config", {})
    config = CloudAdvConfig(
        delay_burst=float(cfg_raw.get("delay_burst", 0.5)),
        delay_round=float(cfg_raw.get("delay_round", 8.0)),
        delay_delete=float(cfg_raw.get("delay_delete", 3.0)),
        jitter=float(cfg_raw.get("jitter", 2.0)),
        provision_timeout=int(cfg_raw.get("provision_timeout", 60)),
        provision_poll=float(cfg_raw.get("provision_poll", 2.0)),
        proxy_required=bool(cfg_raw.get("proxy_required", False)),
        log_level=cfg_raw.get("log_level", "info"),
        telegram_bot_token=cfg_raw.get("telegram_bot_token", ""),
        telegram_chat_id=cfg_raw.get("telegram_chat_id", ""),
        mode=cfg_raw.get("mode", "random"),
    )

    account = CloudAdvAccount(
        account_id=data.get("account_id", 0),
        label=data.get("label", "cloud-adv"),
        key_id=data["key_id"],
        key_secret=data["key_secret"],
        project_id=data["project_id"],
        proxy=data.get("proxy"),
        key_token=data.get("key_token"),
        whitelist_cidrs=data.get("whitelist_cidrs", []),
        panel_base_url=data.get("panel_base_url", ""),
    )

    return CloudAdvWorker(account, config)


def main() -> int:
    parser = argparse.ArgumentParser(description="Cloud.ru (Advanced) IP Hunter Worker")
    parser.add_argument(
        "--single-account",
        action="store_true",
        help="Single account mode (reads JSON from stdin)",
    )
    parser.parse_args()

    data = parse_stdin()
    try:
        worker = build_worker(data)
    except KeyError as e:
        print(f"INVALID_CONFIG: missing key {e}", flush=True)
        emit_event({
            "type": "error",
            "account_id": data.get("account_id", 0),
            "error_type": "INVALID_CONFIG",
            "message": f"missing key {e}",
        })
        emit_event({
            "type": "account_finished",
            "account_id": data.get("account_id", 0),
            "reason": "INVALID_CONFIG",
            "attempts": 0,
            "caught": 0,
        })
        return 1

    def signal_handler(_signum, _frame):
        worker.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, signal_handler)
        except Exception:
            pass

    try:
        worker.run()
    except KeyboardInterrupt:
        worker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
