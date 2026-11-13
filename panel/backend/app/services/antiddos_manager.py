"""Anti-DDoS manager (panel side).

Background loops:
  - whitelist push: hourly, sends the incoming part of the panel's shared allowlist
    (node addresses, panel IP, manual entries, URL lists) plus the DDoS-Guard GRE
    endpoints to every node's antiddos_allow set, so the node has a fresh list on disk
    before an attack. Anti-DDoS has no whitelist of its own; a change of the shared
    allowlist asks for an extra push right away (request_whitelist_push).
  - status poll: reads each node's emergency state into Server.antiddos_*, and
    fires a Telegram alert when a node auto-enters emergency mode.

Also exposes on-demand actions used by the API: install watchdog, toggle
emergency per-node / on all nodes, toggle the watchdog.
"""

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.database import async_session
from app.models import Server, AntiDdosSettings, AlertSettings, AlertHistory, BlocklistRule, GreNode
from app.services.blocklist_manager import get_blocklist_manager
from app.services.gre.render import whitelist_entries as gre_whitelist_entries
from app.services.haproxy_profile_sync import is_server_online
from app.services.http_client import get_node_client, get_node_apply_client, get_external_client, node_auth_headers
from app.services.node_capabilities import Capability, denied_message, server_allows
from app.services.node_sync_queue import KIND_ANTIDDOS_WHITELIST, enqueue
from app.services import update_channel

logger = logging.getLogger(__name__)

FILES_CACHE_TTL = 300   # seconds
# Дольше ждать нечего: nginx ноды обрывает этот запрос через те же 30 секунд
WHITELIST_SYNC_TIMEOUT = 30.0
NODE_CONNECT_TIMEOUT = 3.0
WHITELIST_PUSH_DEBOUNCE = 3.0  # пачка правок общего белого списка — одна рассылка
MOVED_RULE_COMMENT = "из белого списка анти-DDoS"


class AntiDdosManager:
    DB_CONCURRENCY = 10
    HTTP_CONCURRENCY = 50
    INSTALL_RETRY_INTERVAL = 600  # don't retry a failing auto-install more often

    def __init__(self):
        self._running = False
        self._whitelist_task: Optional[asyncio.Task] = None
        self._status_task: Optional[asyncio.Task] = None
        self._requested_push_task: Optional[asyncio.Task] = None
        self._db_sem = asyncio.Semaphore(self.DB_CONCURRENCY)
        self._http_sem = asyncio.Semaphore(self.HTTP_CONCURRENCY)
        self._files_cache: Optional[tuple[float, str, str]] = None
        self._install_cooldown: dict[int, float] = {}
        self._push_requested = asyncio.Event()
        self._push_lock = asyncio.Lock()
        self._pushed_digest: Optional[str] = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def start(self):
        if self._running:
            return
        self._running = True
        try:
            await self._move_manual_list_to_shared()
        except SQLAlchemyError as e:
            logger.error(f"Moving the anti-DDoS manual whitelist to the shared allowlist failed: {e}")
        get_blocklist_manager().add_allowlist_listener(self.request_whitelist_push)
        self._whitelist_task = asyncio.create_task(self._whitelist_loop())
        self._status_task = asyncio.create_task(self._status_loop())
        self._requested_push_task = asyncio.create_task(self._requested_push_loop())
        logger.info("AntiDdosManager started")

    async def stop(self):
        self._running = False
        for task in (self._whitelist_task, self._status_task, self._requested_push_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        logger.info("AntiDdosManager stopped")

    # ── settings ───────────────────────────────────────────────────────────

    async def get_or_create_settings(self, db) -> AntiDdosSettings:
        row = (await db.execute(select(AntiDdosSettings).limit(1))).scalar_one_or_none()
        if row is None:
            row = AntiDdosSettings()
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return row

    # ── whitelist assembly ─────────────────────────────────────────────────

    async def build_whitelist(self, db) -> list[str]:
        """Кого аварийный режим не ограничивает: входящая часть общего белого
        списка панели (адреса нод и панели, ручные записи, списки по URL) и точки
        с сетями проверок GRE-туннелей DDoS-Guard.

        Только IPv4: набор antiddos_allow на ноде — семейство inet."""
        policy = await get_blocklist_manager().build_allow_policy()
        ips = {cidr for cidr in policy.ips["in"] if self._is_ipv4(cidr)}
        ips.update(gre_whitelist_entries((await db.execute(select(GreNode))).scalars().all()))
        return sorted(ips)

    async def _move_manual_list_to_shared(self) -> None:
        """У анти-DDoS был свой ручной белый список; теперь список один — общий.

        Идёт при каждом старте: бэкап прежней версии, восстановленный позже,
        приносит заполненное поле обратно."""
        async with async_session() as db:
            settings = await self.get_or_create_settings(db)
            if not settings.user_cidrs:
                return
            blocklist = get_blocklist_manager()
            wanted = blocklist.deduplicate_ips(self._stored_cidrs(settings.user_cidrs))
            present = set(await blocklist.get_global_rules(db, "in", list_type="allow"))
            moved = [cidr for cidr in wanted if cidr not in present]
            db.add_all([
                BlocklistRule(
                    ip_cidr=cidr, direction="in", list_type="allow", source="manual", comment=MOVED_RULE_COMMENT
                )
                for cidr in moved
            ])
            settings.user_cidrs = None
            await db.commit()
        logger.info(f"Anti-DDoS manual whitelist moved to the shared allowlist: {len(moved)} new of {len(wanted)}")
        blocklist.request_allowlist_push()

    @staticmethod
    def _stored_cidrs(raw: str) -> list[str]:
        try:
            stored = json.loads(raw)
        except ValueError:
            return []
        return [str(item) for item in stored] if isinstance(stored, list) else []

    @staticmethod
    def _is_ipv4(value: str) -> bool:
        try:
            return ipaddress.ip_network(value, strict=False).version == 4
        except ValueError:
            return False

    # ── node calls ─────────────────────────────────────────────────────────

    @staticmethod
    def _whitelist_rejected_by_node(resp: httpx.Response) -> Optional[str]:
        """Причина отказа из тела ответа. Нода старой версии на сбой применения
        списка отвечает HTTP 200 с success=false — по одному коду этого не видно."""
        try:
            body = resp.json()
        except ValueError:
            return None
        if isinstance(body, dict) and body.get("success") is False:
            return str(body.get("message") or "node failed to apply the whitelist")
        return None

    async def sync_whitelist_to_node(self, server: Server, ips: list[str]) -> tuple[bool, str]:
        # Как и с нодой без эндпоинта: не ошибка, а «доставлять некуда».
        # Иначе push_whitelist_all запишет в очередь долг, который не закроется.
        if not server_allows(server, Capability.ANTIDDOS, write=True):
            return True, "node closed the anti-DDoS section"
        try:
            client = get_node_client(server)
            resp = await client.post(
                f"{server.url}/api/antiddos/whitelist/sync",
                headers=node_auth_headers(server),
                json={"ips": ips},
                timeout=httpx.Timeout(WHITELIST_SYNC_TIMEOUT, connect=NODE_CONNECT_TIMEOUT),
            )
            if resp.status_code == 200:
                rejected = self._whitelist_rejected_by_node(resp)
                if rejected:
                    return False, rejected
                return True, "synced"
            if resp.status_code == 404:
                return True, "node has no antiddos endpoint (old version)"
            return False, f"HTTP {resp.status_code}"
        except httpx.TimeoutException:
            return False, "timeout"
        except Exception as e:
            return False, str(e)

    async def get_node_status(self, server: Server, timeout: float = 15.0) -> Optional[dict]:
        if not server_allows(server, Capability.ANTIDDOS, write=False):
            return None
        try:
            client = get_node_client(server)
            resp = await client.get(
                f"{server.url}/api/antiddos/status",
                headers=node_auth_headers(server),
                timeout=timeout,
            )
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception:
            return None

    async def set_node_emergency(self, server: Server, enabled: bool, timeout: float = 45.0) -> tuple[bool, str, Optional[dict]]:
        if not server_allows(server, Capability.ANTIDDOS, write=True):
            return False, denied_message(Capability.ANTIDDOS, True), None
        try:
            client = get_node_apply_client(server)
            resp = await client.post(
                f"{server.url}/api/antiddos/emergency",
                headers=node_auth_headers(server),
                json={"enabled": enabled},
                timeout=timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                return bool(data.get("success")), data.get("message", ""), data.get("status")
            return False, f"HTTP {resp.status_code}", None
        except httpx.TimeoutException:
            return False, "timeout", None
        except Exception as e:
            return False, str(e), None

    async def set_node_watchdog(self, server: Server, enabled: bool, timeout: float = 20.0) -> tuple[bool, str]:
        if not server_allows(server, Capability.ANTIDDOS, write=True):
            return False, denied_message(Capability.ANTIDDOS, True)
        try:
            client = get_node_client(server)
            resp = await client.post(
                f"{server.url}/api/antiddos/watchdog",
                headers=node_auth_headers(server),
                json={"enabled": enabled},
                timeout=timeout,
            )
            if resp.status_code == 200:
                return bool(resp.json().get("success")), "ok"
            return False, f"HTTP {resp.status_code}"
        except Exception as e:
            return False, str(e)

    async def _fetch_watchdog_files(self) -> Optional[tuple[str, str]]:
        loop = asyncio.get_event_loop()
        now = loop.time()
        if self._files_cache and (now - self._files_cache[0]) < FILES_CACHE_TTL:
            return self._files_cache[1], self._files_cache[2]
        try:
            client = get_external_client()
            configs_base = update_channel.github_configs_base()
            sh_resp, svc_resp = await asyncio.gather(
                client.get(f"{configs_base}/ddos-watchdog.sh", timeout=20.0),
                client.get(f"{configs_base}/ddos-watchdog.service", timeout=20.0),
            )
            if sh_resp.status_code != 200 or svc_resp.status_code != 200:
                return None
            self._files_cache = (now, sh_resp.text, svc_resp.text)
            return sh_resp.text, svc_resp.text
        except Exception as e:
            logger.error(f"Failed to fetch watchdog files: {e}")
            return None

    async def _expected_watchdog_version(self) -> Optional[str]:
        files = await self._fetch_watchdog_files()
        if not files:
            return None
        match = re.search(r'WATCHDOG_VERSION="?([0-9][0-9.]*)"?', files[0])
        return match.group(1) if match else None

    async def install_to_node(self, server: Server, timeout: float = 60.0) -> tuple[bool, str]:
        if not server_allows(server, Capability.ANTIDDOS, write=True):
            return False, denied_message(Capability.ANTIDDOS, True)
        files = await self._fetch_watchdog_files()
        if not files:
            return False, "could not fetch watchdog files from GitHub"
        script_content, service_content = files
        try:
            async with async_session() as db:
                settings = await self.get_or_create_settings(db)
                watchdog_default = settings.watchdog_default_enabled
            client = get_node_apply_client(server)
            resp = await client.post(
                f"{server.url}/api/antiddos/install",
                headers=node_auth_headers(server),
                json={
                    "script_content": script_content,
                    "service_content": service_content,
                    "watchdog_enabled": watchdog_default,
                },
                timeout=timeout,
            )
            if resp.status_code == 200:
                return bool(resp.json().get("success")), "installed"
            return False, f"HTTP {resp.status_code}"
        except Exception as e:
            return False, str(e)

    # ── fan-out actions ────────────────────────────────────────────────────

    async def run_bg(self, coro):
        """Await a fan-out coroutine as a fire-and-forget background task,
        swallowing errors so a dropped client or a bad node can't crash it."""
        try:
            await coro
        except Exception as e:
            logger.error(f"background antiddos task failed: {e}")

    async def _active_servers(self) -> list[Server]:
        async with self._db_sem:
            async with async_session() as db:
                return list(
                    (await db.execute(select(Server).where(Server.is_active == True))).scalars().all()  # noqa: E712
                )

    async def _push_to_servers(self, servers: list[Server], ips: list[str]) -> dict[int, Optional[str]]:
        """Разослать готовый whitelist живым нодам. По ноде — None при успехе или
        текст ошибки."""
        async def _one(srv: Server) -> tuple[int, Optional[str]]:
            async with self._http_sem:
                ok, msg = await self.sync_whitelist_to_node(srv, ips)
                return srv.id, None if ok else msg

        results = await asyncio.gather(*[_one(s) for s in servers], return_exceptions=True)
        out: dict[int, Optional[str]] = {}
        for srv, result in zip(servers, results):
            out[srv.id] = str(result) if isinstance(result, BaseException) else result[1]
        return out

    def request_whitelist_push(self) -> None:
        """Попросить разослать whitelist: изменился общий белый список.

        Не ждёт рассылки. Пачка правок схлопывается в одну рассылку, а набор,
        который от правки не изменился (исходящий адрес), не рассылается вовсе."""
        self._push_requested.set()

    async def push_whitelist_all(self, only_if_changed: bool = False) -> dict:
        # Рассылки идут по одной: у параллельных на ноде остался бы набор той,
        # что дошла последней, а не той, что собрана позже
        async with self._push_lock:
            return await self._push_whitelist_all(only_if_changed)

    async def _push_whitelist_all(self, only_if_changed: bool) -> dict:
        async with self._db_sem:
            async with async_session() as db:
                ips = await self.build_whitelist(db)
                servers = list(
                    (await db.execute(select(Server).where(Server.is_active == True))).scalars().all()  # noqa: E712
                )

        digest = hashlib.sha256("\n".join(ips).encode()).hexdigest()
        if only_if_changed and digest == self._pushed_digest:
            return {"whitelist_size": len(ips), "nodes": 0, "ok": 0, "queued": 0}

        # Офлайн-ноде набор отправить некуда: запрос упрётся в таймаут, а изменение
        # пропадёт. Очередь доставит его сама, когда нода вернётся в сеть.
        # Закрывшие раздел не участвуют ни в отправке, ни в очереди
        servers = [s for s in servers if server_allows(s, Capability.ANTIDDOS, write=True)]
        online = [s for s in servers if is_server_online(s)]
        offline = [s for s in servers if not is_server_online(s)]

        errors = await self._push_to_servers(online, ips)
        ok_count = sum(1 for error in errors.values() if error is None)

        deferred = [sid for sid, error in errors.items() if error is not None] + [s.id for s in offline]
        await enqueue(deferred, KIND_ANTIDDOS_WHITELIST, "whitelist не доставлен")

        async with async_session() as db:
            settings = await self.get_or_create_settings(db)
            settings.last_push_at = datetime.now(timezone.utc)
            settings.last_push_status = "ok" if ok_count == len(servers) else "partial"
            settings.last_push_count = len(ips)
            await db.commit()

        # Не принявшие набор ноды уже в очереди — она соберёт его заново
        self._pushed_digest = digest
        return {
            "whitelist_size": len(ips),
            "nodes": len(servers),
            "ok": ok_count,
            "queued": len(deferred),
        }

    async def push_whitelist_to_servers(self, server_ids: list[int]) -> dict[int, Optional[str]]:
        """Досинхронизировать whitelist перечисленным нодам (исполнитель очереди)."""
        async with self._db_sem:
            async with async_session() as db:
                ips = await self.build_whitelist(db)
                servers = list((await db.execute(
                    select(Server).where(
                        Server.id.in_(server_ids),
                        Server.is_active == True  # noqa: E712
                    )
                )).scalars().all())

        # Удалённый или выключенный сервер — долг закрыт.
        found = {s.id for s in servers}
        results: dict[int, Optional[str]] = {sid: None for sid in server_ids if sid not in found}
        results.update(await self._push_to_servers(servers, ips))
        return results

    @staticmethod
    def _allowed_for_write(servers: list[Server]) -> list[Server]:
        return [s for s in servers if server_allows(s, Capability.ANTIDDOS, write=True)]

    async def set_emergency_all(self, enabled: bool) -> dict:
        # Закрывшие раздел не считаем ни в «сколько нод», ни в «сколько удалось»
        servers = self._allowed_for_write(await self._active_servers())

        async def _one(srv: Server):
            async with self._http_sem:
                ok, msg, status = await self.set_node_emergency(srv, enabled)
                if ok and status:
                    await self._store_status(srv.id, status)
                return {"server_id": srv.id, "success": ok, "message": msg}

        results = await asyncio.gather(*[_one(s) for s in servers], return_exceptions=True)
        ok_count = sum(1 for r in results if isinstance(r, dict) and r.get("success"))
        return {"nodes": len(servers), "ok": ok_count}

    async def set_watchdog_all(self, enabled: bool) -> dict:
        servers = self._allowed_for_write(await self._active_servers())

        async def _one(srv: Server):
            async with self._http_sem:
                ok, _ = await self.set_node_watchdog(srv, enabled)
                return ok

        results = await asyncio.gather(*[_one(s) for s in servers], return_exceptions=True)
        ok_count = sum(1 for r in results if r is True)
        return {"nodes": len(servers), "ok": ok_count}

    async def apply_master_state(self, enabled: bool):
        """The master switch controls ONLY fleet-wide auto-detection (watchdog).
        Manual emergency mode is a separate control and is never touched here, so
        turning auto-detection off never clears an emergency an admin pinned."""
        try:
            await self.set_watchdog_all(enabled)
        except Exception as e:
            logger.error(f"apply_master_state({enabled}) failed: {e}")

    async def install_all(self) -> dict:
        servers = await self._active_servers()

        async def _one(srv: Server):
            async with self._http_sem:
                ok, msg = await self.install_to_node(srv)
                return {"server_id": srv.id, "success": ok, "message": msg}

        results = await asyncio.gather(*[_one(s) for s in servers], return_exceptions=True)
        ok_count = sum(1 for r in results if isinstance(r, dict) and r.get("success"))
        return {"nodes": len(servers), "ok": ok_count}

    # ── status persistence + alerting ──────────────────────────────────────

    async def _store_status(self, server_id: int, status: dict, alert: bool = False):
        mode_on = status.get("mode") == "on"
        source = status.get("source", "none")
        reason = (status.get("reason") or "")[:200]
        watchdog_on = status.get("watchdog", "on") == "on"
        since_epoch = int(status.get("since", 0) or 0)
        since_dt = datetime.fromtimestamp(since_epoch, tz=timezone.utc) if since_epoch else None

        async with async_session() as db:
            srv = (await db.execute(select(Server).where(Server.id == server_id))).scalar_one_or_none()
            if srv is None:
                return
            was_on = bool(srv.antiddos_emergency_mode)
            srv.antiddos_emergency_mode = mode_on
            srv.antiddos_source = source
            srv.antiddos_reason = reason
            srv.antiddos_watchdog = watchdog_on
            srv.antiddos_since = since_dt
            srv.antiddos_last_sync_at = datetime.now(timezone.utc)
            server_name = srv.name
            await db.commit()

        # alert only on an off→on transition that the node enabled itself
        if alert and mode_on and not was_on and source == "auto":
            await self._send_alert(server_id, server_name, reason)

    async def _send_alert(self, server_id: int, server_name: str, reason: str):
        try:
            async with async_session() as db:
                alert_settings = (await db.execute(select(AlertSettings).limit(1))).scalar_one_or_none()
            token = getattr(alert_settings, "telegram_bot_token", None)
            chat_id = getattr(alert_settings, "telegram_chat_id", None)
            notified = False
            if token and chat_id:
                text = (
                    f"\U0001f6e1️ <b>Anti-DDoS: аварийный режим</b>\n\n"
                    f"Нода <b>{server_name}</b> автоматически включила защиту.\n"
                    f"Причина: {reason or 'signal detected'}"
                )
                from app.services.telegram_bot import get_telegram_bot_service
                notified = await get_telegram_bot_service().send_message(token, chat_id, text)

            async with async_session() as db:
                db.add(AlertHistory(
                    server_id=server_id,
                    server_name=server_name,
                    alert_type="antiddos_emergency",
                    severity="warning",
                    message=f"Auto emergency mode: {reason}",
                    details=json.dumps({"reason": reason}, ensure_ascii=False),
                    notified=notified,
                ))
                await db.commit()
        except Exception as e:
            logger.error(f"Failed to send antiddos alert: {e}")

    async def _maybe_auto_install(self, server: Server, status: dict, expected_version: Optional[str]) -> dict:
        """Zero-touch rollout: if a node has the new agent (/api/antiddos exists,
        so status came back) but the watchdog isn't installed — or it's an older
        version — the panel installs/updates it itself. No admin action, no per-node
        clicking. A per-node cooldown keeps a failing install from retrying every poll.
        """
        # Проверка до cooldown: закрытая нода иначе получала бы попытку установки
        # каждые десять минут и до конца времён
        if not server_allows(server, Capability.ANTIDDOS, write=True):
            return status

        installed = status.get("installed", False)
        version = status.get("version") or ""
        needs = (not installed) or (expected_version and version and version != expected_version)
        if not needs:
            return status

        loop = asyncio.get_event_loop()
        now = loop.time()
        if now - self._install_cooldown.get(server.id, 0.0) < self.INSTALL_RETRY_INTERVAL:
            return status
        self._install_cooldown[server.id] = now

        ok, msg = await self.install_to_node(server)
        if not ok:
            logger.warning(f"Auto-install of watchdog on {server.name} failed: {msg}")
            return status

        logger.info(f"Auto-installed watchdog on {server.name}")
        # give the freshly-installed node its whitelist immediately
        try:
            async with async_session() as db:
                ips = await self.build_whitelist(db)
            await self.sync_whitelist_to_node(server, ips)
        except Exception as e:
            logger.warning(f"Post-install whitelist push to {server.name} failed: {e}")

        fresh = await self.get_node_status(server)
        return fresh if fresh is not None else status

    async def poll_status_all(self):
        servers = await self._active_servers()
        expected_version = await self._expected_watchdog_version()

        async def _one(srv: Server):
            async with self._http_sem:
                status = await self.get_node_status(srv)
                if status is None:
                    return  # unreachable, or old agent without /api/antiddos
                status = await self._maybe_auto_install(srv, status, expected_version)
                await self._store_status(srv.id, status, alert=True)

        await asyncio.gather(*[_one(s) for s in servers], return_exceptions=True)

    # ── loops ──────────────────────────────────────────────────────────────

    async def _whitelist_loop(self):
        await asyncio.sleep(30)
        while self._running:
            interval = 3600
            try:
                async with async_session() as db:
                    settings = await self.get_or_create_settings(db)
                    enabled = settings.enabled
                    interval = max(300, settings.whitelist_push_interval or 3600)
                if enabled:
                    await self.push_whitelist_all()
            except Exception as e:
                logger.error(f"Whitelist push loop error: {e}")
            await asyncio.sleep(interval)

    async def _requested_push_loop(self):
        """Рассылка по запросу идёт и при выключенном автодетекте: аварийный
        режим включают и вручную, а набор на ноде должен быть свежим до него."""
        while self._running:
            await self._push_requested.wait()
            await asyncio.sleep(WHITELIST_PUSH_DEBOUNCE)
            self._push_requested.clear()
            try:
                await self.push_whitelist_all(only_if_changed=True)
            except Exception as e:
                logger.error(f"Requested whitelist push failed: {e}")

    async def _status_loop(self):
        await asyncio.sleep(20)
        while self._running:
            interval = 60
            try:
                async with async_session() as db:
                    settings = await self.get_or_create_settings(db)
                    enabled = settings.enabled
                    interval = max(15, settings.status_poll_interval or 60)
                if enabled:
                    await self.poll_status_all()
            except Exception as e:
                logger.error(f"Status poll loop error: {e}")
            await asyncio.sleep(interval)


_manager: Optional[AntiDdosManager] = None


def get_antiddos_manager() -> AntiDdosManager:
    global _manager
    if _manager is None:
        _manager = AntiDdosManager()
    return _manager


async def start_antiddos_manager():
    await get_antiddos_manager().start()


async def stop_antiddos_manager():
    await get_antiddos_manager().stop()
