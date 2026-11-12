#!/usr/bin/env python3
"""
Cloud.ru (Evolution edition) IP Hunter Worker.

Subprocess-воркер: читает JSON из stdin, в бесконечном цикле создаёт
floating-IP пачками до квоты, ждёт provisioning, классифицирует по whitelist
CIDR (HIT — оставляем, MISS — удаляем). Все попадания репортятся обратно в
панель через /cloud/evo/internal/caught-ip, плюс Telegram; все выданные за
раунд адреса — через /cloud/evo/internal/received-ips для статистики.

Логика воркера повторяет eip_hunt.py и адаптирована под subprocess-протокол
панели (EVENT'ы через stdout, SIGTERM — доделать текущий раунд и выйти).
"""

import argparse
import ipaddress
import json
import random
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ═══════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════

CLOUD_EVO_IAM_URL = "https://iam.api.cloud.ru/api/v1/auth/token"
CLOUD_EVO_BASE = "https://compute.api.cloud.ru/api/v1"
CLOUD_EVO_RESOURCE = "floating-ips"

BUSY_STATUSES = {
    "CREATING", "PENDING", "INIT", "INITIALIZING",
    "DELETING", "UPDATING", "ATTACHING", "DETACHING",
}

REPORT_TIMEOUT = 10.0
TELEGRAM_TIMEOUT = 10.0


# ═══════════════════════════════════════════════════════════════════════
# PROXY PARSER (зеркало cloud_evo_backend_service)
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


def is_quota_error(msg: str) -> bool:
    s = msg.lower()
    return any(k in s for k in ("quota", "limit", "exceeded", "too many", "429"))


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
# DATACLASSES
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class CloudEvoConfig:
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


@dataclass
class CloudEvoAccount:
    account_id: int
    label: str
    key_id: str
    key_secret: str
    project_id: str
    proxy: Optional[str] = None
    zones: list = field(default_factory=list)
    whitelist_cidrs: list = field(default_factory=list)
    panel_base_url: str = ""


# ═══════════════════════════════════════════════════════════════════════
# CLOUD.RU API CLIENT
# ═══════════════════════════════════════════════════════════════════════

class CloudEvoAPI:

    def __init__(self, key_id: str, key_secret: str, project_id: str, proxy_str: Optional[str] = None):
        self.key_id = key_id
        self.key_secret = key_secret
        self.project_id = project_id

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

        self.headers: dict = {}

    def auth(self) -> None:
        r = self.session.post(
            CLOUD_EVO_IAM_URL,
            json={"keyId": self.key_id, "secret": self.key_secret},
            timeout=30,
        )
        r.raise_for_status()
        token = r.json().get("access_token")
        if not token:
            raise RuntimeError("access_token отсутствует в ответе")
        self.headers = {"Authorization": f"Bearer {token}"}

    def _req(self, method: str, url: str, **kw) -> requests.Response:
        kw.setdefault("timeout", 60)
        r = self.session.request(method, url, headers=self.headers, **kw)
        if r.status_code == 401:
            self.auth()
            r = self.session.request(method, url, headers=self.headers, **kw)
        return r

    def list_eips(self) -> list:
        r = self._req(
            "GET",
            f"{CLOUD_EVO_BASE}/{CLOUD_EVO_RESOURCE}",
            params={"project_id": self.project_id},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"GET {CLOUD_EVO_RESOURCE} {r.status_code}: {r.text[:500]}")
        data = r.json()
        return data.get("items", data) if isinstance(data, dict) else data

    def create_eip(self, name: str, zone: str) -> dict:
        body = {
            "project_id": self.project_id,
            "name": name,
            "description": "",
            "availability_zone_name": zone,
        }
        r = self._req("POST", f"{CLOUD_EVO_BASE}/{CLOUD_EVO_RESOURCE}", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"POST {CLOUD_EVO_RESOURCE} {r.status_code}: {r.text[:500]}")
        return r.json()

    def get_eip(self, eip_id: str, allow_404: bool = False) -> Optional[dict]:
        r = self._req("GET", f"{CLOUD_EVO_BASE}/{CLOUD_EVO_RESOURCE}/{eip_id}")
        if r.status_code == 404 and allow_404:
            return None
        if r.status_code >= 400:
            raise RuntimeError(f"GET {CLOUD_EVO_RESOURCE}/{eip_id} {r.status_code}: {r.text[:500]}")
        return r.json()

    def delete_eip(self, eip_id: str) -> tuple:
        r = self._req("DELETE", f"{CLOUD_EVO_BASE}/{CLOUD_EVO_RESOURCE}/{eip_id}")
        if r.status_code < 300 or r.status_code == 404:
            return True, ""
        return False, f"{r.status_code}: {r.text[:300]}"

    def wait_ready(self, eip_id: str, timeout: float, poll: float) -> tuple:
        deadline = time.time() + timeout
        last_ip, last_status = None, ""
        while time.time() < deadline:
            try:
                obj = self.get_eip(eip_id, allow_404=True)
            except Exception as e:
                log(f"  wait_ready get_eip error: {e}")
                time.sleep(poll)
                continue
            if obj is None:
                time.sleep(poll)
                continue
            last_ip = obj.get("ip_address") or last_ip
            last_status = (obj.get("status") or "").upper()
            if last_status in ("ERROR", "FAILED"):
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

class CloudEvoWorker:

    def __init__(self, account: CloudEvoAccount, config: CloudEvoConfig):
        self.account = account
        self.config = config

        self.networks = []
        for cidr in account.whitelist_cidrs or []:
            try:
                self.networks.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                log(f"BAD CIDR in whitelist (ignored): {cidr}")

        if not account.zones:
            self.zones = ["ru.AZ-1"]
        else:
            self.zones = list(account.zones)

        self.api = CloudEvoAPI(
            key_id=account.key_id,
            key_secret=account.key_secret,
            project_id=account.project_id,
            proxy_str=account.proxy,
        )

        self.preexisting_ids: set = set()
        self.created_in_session: set = set()

        self.attempts: int = 0
        self.caught: int = 0
        self.round_no: int = 0
        self._stop_flag: bool = False

        self._report_session = requests.Session()
        self._report_session.headers.update({"Content-Type": "application/json"})

    def stop(self) -> None:
        self._stop_flag = True

    # ──────────────────────── reporting ────────────────────────

    def report_caught(self, ip: str, eip_id: str, zone: str, subnet: str) -> None:
        self.caught += 1

        emit_event({
            "type": "ip_caught",
            "account_id": self.account.account_id,
            "ip": ip,
            "eip_id": eip_id,
            "zone": zone,
            "subnet": subnet,
            "total_caught": self.caught,
            "total_attempts": self.attempts,
        })

        if self.account.panel_base_url:
            try:
                self._report_session.post(
                    f"{self.account.panel_base_url.rstrip('/')}/cloud/evo/internal/caught-ip",
                    params={
                        "account_id": self.account.account_id,
                        "ip_address": ip,
                        "eip_id": eip_id,
                        "zone": zone,
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
                            f"<b>Cloud.ru IP CAUGHT</b>\n\n"
                            f"IP: <b>{ip}</b>\n"
                            f"Subnet: {subnet}\n"
                            f"Zone: {zone}\n"
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
            {"ip": ip, "zone": zone, "caught": in_whitelist(ip, self.networks) is not None}
            for _eip_id, ip, _status, zone in ready
            if ip
        ]
        if not items or not self.account.panel_base_url:
            return
        try:
            self._report_session.post(
                f"{self.account.panel_base_url.rstrip('/')}/cloud/evo/internal/received-ips",
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
            log(f"  SKIP DELETE: id={eip_id} get_eip error {e}")
            return False
        if cur is None:
            log(f"  SKIP DELETE: id={eip_id} не виден")
            return False
        vm = cur.get("vm") or {}
        if vm.get("id"):
            log(f"  SKIP DELETE: id={eip_id} привязан к VM {vm.get('name')}")
            return False
        return True

    def _check_proxy_requirement(self) -> bool:
        if self.config.proxy_required and not self.account.proxy:
            log("NO_PROXY — proxy_required=True, выход")
            emit_event({
                "type": "error",
                "account_id": self.account.account_id,
                "error_type": "NO_PROXY",
                "message": "Proxy required but not configured",
            })
            emit_event({
                "type": "account_finished",
                "account_id": self.account.account_id,
                "reason": "NO_PROXY",
                "attempts": 0,
                "caught": 0,
            })
            return False
        return True

    def _initial_auth_and_snapshot(self) -> bool:
        try:
            self.api.auth()
            log("auth ok")
        except Exception as e:
            log(f"AUTH failed: {e}")
            emit_event({
                "type": "error",
                "account_id": self.account.account_id,
                "error_type": "AUTH_FAILED",
                "message": str(e)[:200],
            })
            emit_event({
                "type": "account_finished",
                "account_id": self.account.account_id,
                "reason": "AUTH_FAILED",
                "attempts": 0,
                "caught": 0,
            })
            return False

        try:
            existing = self.api.list_eips()
            self.preexisting_ids = {e.get("id") for e in existing if e.get("id")}
            log(f"в проекте уже {len(self.preexisting_ids)} floating-ip (под защитой)")
            for e in existing:
                log(
                    f"  PRE-EXISTING ip={e.get('ip_address')} id={e.get('id')} "
                    f"vm={(e.get('vm') or {}).get('name')}"
                )
        except Exception as e:
            log(f"не получить список existing IP: {e}")
            emit_event({
                "type": "error",
                "account_id": self.account.account_id,
                "error_type": "LIST_FAILED",
                "message": str(e)[:200],
            })
            emit_event({
                "type": "account_finished",
                "account_id": self.account.account_id,
                "reason": "LIST_FAILED",
                "attempts": 0,
                "caught": 0,
            })
            return False

        return True

    def run(self) -> None:
        log(
            f"START account={self.account.account_id} ({self.account.label}) "
            f"zones={self.zones} whitelist={len(self.networks)} CIDR"
        )
        log(
            f"delays: burst={self.config.delay_burst}s round={self.config.delay_round}s "
            f"delete={self.config.delay_delete}s ±{self.config.jitter}"
        )

        if not self._check_proxy_requirement():
            return

        if not self._initial_auth_and_snapshot():
            return

        try:
            self._loop()
        except Exception as e:
            log(f"FATAL_CRASH: {type(e).__name__}: {e}")
            emit_event({
                "type": "error",
                "account_id": self.account.account_id,
                "error_type": "FATAL_CRASH",
                "message": f"{type(e).__name__}: {str(e)[:200]}",
            })
            emit_event({
                "type": "account_finished",
                "account_id": self.account.account_id,
                "reason": "FATAL_CRASH",
                "attempts": self.attempts,
                "caught": self.caught,
            })
            return

        log(f"FINISHED attempts={self.attempts} caught={self.caught}")
        emit_event({
            "type": "account_finished",
            "account_id": self.account.account_id,
            "reason": "STOPPED",
            "attempts": self.attempts,
            "caught": self.caught,
        })

    def _loop(self) -> None:
        while not self._stop_flag:
            self.round_no += 1
            emit_event({
                "type": "round_start",
                "account_id": self.account.account_id,
                "round_no": self.round_no,
            })

            log(f"=== раунд {self.round_no}: жмём POST до квоты ===")
            batch = self._fill_until_quota()

            if batch:
                self._finish_round(batch)

            if self._stop_flag:
                break

            pause = jittered(self.config.delay_round, self.config.jitter)
            if batch:
                log(f"раунд {self.round_no} закончен, пауза {pause:.1f}s")
            else:
                log(f"раунд {self.round_no}: ничего не создали, пауза {pause:.1f}s")
            self._sleep_interruptible(pause)

    def _finish_round(self, batch: list) -> None:
        # Остановка посреди раунда всё равно доводит его до конца: созданные
        # промахи иначе остались бы висеть в проекте и копить счёт, а попадание
        # без дождавшегося provision адреса не отличить от промаха
        if self._stop_flag:
            log(f"раунд {self.round_no}: остановка — доделываю раунд, создано {len(batch)} IP")

        log(f"раунд {self.round_no}: ждём provision {len(batch)} IP")
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
        """Создаёт IP пачкой до получения ошибки квоты. Возвращает [(eip_id, zone), ...]."""
        batch = []
        local_attempt = 0
        consec_errors = 0
        while not self._stop_flag:
            local_attempt += 1
            self.attempts += 1
            emit_event({"type": "ip_attempt", "account_id": self.account.account_id})

            zone = self.zones[(local_attempt - 1) % len(self.zones)]
            name = f"a{random.randint(0, 0xFFFFFF):06x}"

            try:
                obj = self.api.create_eip(name, zone)
            except Exception as e:
                msg = str(e)
                if is_quota_error(msg):
                    log(f"  квота: {msg[:200]}")
                    log(f"  создано в пачке: {len(batch)}, переход к provision")
                    emit_event({
                        "type": "quota_hit",
                        "account_id": self.account.account_id,
                        "batch_size": len(batch),
                    })
                    break
                consec_errors += 1
                log(f"  create error #{consec_errors}: {msg[:200]}")
                if consec_errors >= 5:
                    log("  слишком много подряд create-ошибок, выход из пачки")
                    emit_event({
                        "type": "error",
                        "account_id": self.account.account_id,
                        "error_type": "CREATE_BURST_FAILED",
                        "message": msg[:200],
                    })
                    break
                self._sleep_interruptible(jittered(self.config.delay_burst, self.config.jitter))
                continue

            consec_errors = 0
            eip_id = obj.get("id")
            if not eip_id:
                log(f"  нет id в ответе: {obj}")
                continue
            self.created_in_session.add(eip_id)
            batch.append((eip_id, zone))
            log(f"  +created #{len(batch)} id={eip_id} zone={zone}")

            self._sleep_interruptible(jittered(self.config.delay_burst, self.config.jitter))

        return batch

    def _wait_batch_ready(self, batch: list) -> list:
        """Возвращает [(eip_id, ip, status, zone), ...]."""
        ready = []
        for eip_id, zone in batch:
            try:
                ip, status = self.api.wait_ready(
                    eip_id,
                    timeout=self.config.provision_timeout,
                    poll=self.config.provision_poll,
                )
            except Exception as e:
                log(f"  wait_ready {eip_id}: {e}")
                ip, status = None, "?"
            ready.append((eip_id, ip, status, zone))
            log(f"  ready id={eip_id} zone={zone} ip={ip} status={status}")
        return ready

    def _classify(self, ready: list) -> tuple:
        kept = 0
        to_delete = []
        for eip_id, ip, _status, zone in ready:
            match = in_whitelist(ip, self.networks)
            if match:
                kept += 1
                log(f"  HIT: {ip} → {match} → ОСТАВЛЯЮ id={eip_id}")
                self.report_caught(ip, eip_id, zone, str(match))
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


def build_worker(data: dict) -> CloudEvoWorker:
    cfg_raw = data.get("config", {})
    config = CloudEvoConfig(
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
    )

    account = CloudEvoAccount(
        account_id=data.get("account_id", 0),
        label=data.get("label", "cloud-evo"),
        key_id=data["key_id"],
        key_secret=data["key_secret"],
        project_id=data["project_id"],
        proxy=data.get("proxy"),
        zones=data.get("zones", ["ru.AZ-1", "ru.AZ-2", "ru.AZ-3"]),
        whitelist_cidrs=data.get("whitelist_cidrs", []),
        panel_base_url=data.get("panel_base_url", ""),
    )

    return CloudEvoWorker(account, config)


def main() -> int:
    parser = argparse.ArgumentParser(description="Cloud.ru (Evolution) IP Hunter Worker")
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
