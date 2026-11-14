"""Anti-DDoS manager (panel side).

Background loops:
  - whitelist push: hourly, sends the incoming part of the panel's shared allowlist
    (node addresses, panel IP, manual entries, URL lists) plus the DDoS-Guard GRE
    endpoints to every node's antiddos_allow set, so the node has a fresh list on disk
    before an attack. Anti-DDoS has no whitelist of its own; a change of the shared
    allowlist asks for an extra push right away (request_whitelist_push).
  - status poll: reads each node's emergency state into Server.antiddos_*, and
    fires a Telegram alert when a node auto-enters emergency mode. The same pass
    installs or updates the watchdog where its version differs from the one on
    GitHub — checked before it is sent, and rolled out as a canary (WatchdogRollout).

Also exposes on-demand actions used by the API: install watchdog, toggle
emergency per-node / on all nodes, toggle the watchdog. A fleet-wide action
runs in the background and leaves its per-node outcome in `last_action`.
"""

import asyncio
import hashlib
import html
import ipaddress
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Awaitable, Callable, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.database import async_session
from app.models import Server, AntiDdosSettings, AlertSettings, AlertHistory, BlocklistRule, GreNode
from app.services.blocklist_manager import get_blocklist_manager
from app.services.gre.render import whitelist_entries as gre_whitelist_entries
from app.services.haproxy_profile_sync import is_server_online
from app.services.http_client import get_node_client, get_node_apply_client, get_external_client, node_auth_headers
from app.services.metrics_collector import get_collector
from app.services.node_capabilities import Capability, denied_message, server_allows, server_allows_path
from app.services.node_sync_queue import KIND_ANTIDDOS_WHITELIST, enqueue
from app.services import update_channel

logger = logging.getLogger(__name__)

FILES_CACHE_TTL = 300   # seconds
# GitHub не ответил — повтор не раньше: иначе при рассылке на парк каждая нода
# по очереди ждала бы свой таймаут скачивания
RELEASE_RETRY_INTERVAL = 30
BASH_CHECK_TIMEOUT = 15.0
# Дольше ждать нечего: nginx ноды обрывает этот запрос через те же 30 секунд
WATCHDOG_INSTALL_PATH = "/api/antiddos/install"
WHITELIST_SYNC_TIMEOUT = 30.0
NODE_CONNECT_TIMEOUT = 3.0
WHITELIST_PUSH_DEBOUNCE = 3.0  # пачка правок общего белого списка — одна рассылка
MOVED_RULE_COMMENT = "из белого списка анти-DDoS"
# Строка сервера без изменений переписывается не чаще: иначе опрос статуса
# каждую минуту обновлял бы всю таблицу servers
STATUS_REFRESH_INTERVAL = timedelta(minutes=5)
MESSAGE_LIMIT = 300
FAILED_NODES_SHOWN = 200

# Новая версия сторожа сначала уходит горстке нод; остальным — когда опрос
# статуса подтвердил её на нескольких из них
CANARY_INSTALLS = 5
CANARY_CONFIRMATIONS = 3
ROLLOUT_FAILURE_LIMIT = 3
# После канарейки три отказа на парк в тысячи нод — обычный фон (полный диск,
# зависший агент): раскатку останавливает только заметная доля отказов
ROLLOUT_FAILURE_SHARE = 0.05
# Нода, принявшая версию и замолчавшая, — отказ: сторож правит iptables и мог её отрезать
CONFIRM_TIMEOUT = 600.0

_VERSION_LINE_RE = re.compile(r'^WATCHDOG_VERSION="([0-9][0-9.]*)"\s*$', re.MULTILINE)


@dataclass(frozen=True)
class WatchdogRelease:
    script: str
    service: str
    version: str


def watchdog_version(script: str) -> Optional[str]:
    match = _VERSION_LINE_RE.search(script)
    return match.group(1) if match else None


def release_problem(script: str, service: str) -> Optional[str]:
    """Чем скачанный сторож не годится для рассылки, либо None.

    С GitHub вместо скрипта приходят и пустой ответ, и страница ошибки, и файл,
    оборванный посередине; нода запустила бы это от root."""
    if not script.strip():
        return "script is empty"
    if not script.startswith("#!"):
        return "script does not start with a shebang"
    if watchdog_version(script) is None:
        return 'script has no WATCHDOG_VERSION="x.y.z" line'
    if "ExecStart=" not in service:
        return "service file is not a systemd unit"
    return None


async def bash_syntax_problem(script: str) -> Optional[str]:
    """Синтаксическая ошибка скрипта по `bash -n`, либо None.

    Без bash в образе пропускается только эта проверка: остальные от него не
    зависят, а нода перед заменой проверяет скрипт ещё раз."""
    try:
        process = await asyncio.create_subprocess_exec(
            "bash", "-n",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        logger.warning("bash is not available: watchdog script syntax is not checked on the panel")
        return None
    except OSError as e:
        return f"bash -n could not run: {e}"

    try:
        _, stderr = await asyncio.wait_for(process.communicate(script.encode()), BASH_CHECK_TIMEOUT)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return "bash -n timed out"
    if process.returncode == 0:
        return None
    return f"bash -n: {stderr.decode(errors='replace').strip()[:MESSAGE_LIMIT] or process.returncode}"


@dataclass
class WatchdogRollout:
    """Раскатка одной версии сторожа по парку.

    Пока версию не подтвердили CANARY_CONFIRMATIONS нод, её получают не больше
    CANARY_INSTALLS. Подтверждает версию опрос статуса, пришедший после
    установки, а не ответ самой установки: скрипт, падающий через минуту, в нём
    ещё жив. Останавливают раскатку ROLLOUT_FAILURE_LIMIT неудачных установок,
    если отказавших нод при этом больше ROLLOUT_FAILURE_SHARE от всех, кто
    версию получил.

    Живёт в памяти: рестарт панели начинает канарейку заново — она проходит
    сразу, если версия уже стоит хотя бы на трёх нодах."""
    version: str
    started: set[int] = field(default_factory=set)
    # server_id → когда нода приняла версию; ждёт подтверждения опросом статуса
    awaiting: dict[int, float] = field(default_factory=dict)
    confirmed: set[int] = field(default_factory=set)
    failed: dict[int, dict] = field(default_factory=dict)
    # Неудачные установки считаются вместе с повторами: на парке из двух нод
    # трёх разных отказавших не набралось бы никогда
    failures: int = 0
    halted: bool = False

    @property
    def proven(self) -> bool:
        return len(self.confirmed) >= CANARY_CONFIRMATIONS

    def may_install(self, server_id: int) -> bool:
        if self.halted:
            return False
        return self.proven or server_id in self.started or len(self.started) < CANARY_INSTALLS

    def installed(self, server_id: int, at: float) -> None:
        self.failed.pop(server_id, None)
        self.awaiting[server_id] = at

    def confirm(self, server_id: int) -> None:
        self.awaiting.pop(server_id, None)
        self.failed.pop(server_id, None)
        self.confirmed.add(server_id)

    def fail(self, server: Server, message: str) -> bool:
        """True — этим отказом раскатка остановлена."""
        self.awaiting.pop(server.id, None)
        self.confirmed.discard(server.id)
        self.failed[server.id] = {
            "server_id": server.id, "server_name": server.name, "message": message[:MESSAGE_LIMIT],
        }
        self.failures += 1
        if self.halted or self.failures < ROLLOUT_FAILURE_LIMIT:
            return False
        received = len(self.confirmed) + len(self.awaiting) + len(self.failed)
        if len(self.failed) <= ROLLOUT_FAILURE_SHARE * received:
            return False
        self.halted = True
        return True

    def overdue(self, now: float) -> list[int]:
        return [server_id for server_id, at in self.awaiting.items() if now - at > CONFIRM_TIMEOUT]

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "confirmed": len(self.confirmed),
            "pending": len(self.awaiting),
            "failed": len(self.failed),
            "halted": self.halted,
            "failed_nodes": list(self.failed.values())[:FAILED_NODES_SHOWN],
        }


class FleetActionKind(str, Enum):
    EMERGENCY = "emergency"
    WATCHDOG = "watchdog"
    INSTALL = "install"


@dataclass
class FleetAction:
    """Ход и итог одной команды на весь парк. Хранится в памяти только последняя."""
    kind: FleetActionKind
    enabled: Optional[bool]
    total: int
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None
    ok: int = 0
    failed: list[dict] = field(default_factory=list)

    def record(self, server: Server, ok: bool, message: str) -> None:
        if ok:
            self.ok += 1
            return
        self.failed.append({
            "server_id": server.id, "server_name": server.name, "message": message[:MESSAGE_LIMIT],
        })

    def to_dict(self) -> dict:
        return {
            "action": self.kind.value,
            "enabled": self.enabled,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "total": self.total,
            "ok": self.ok,
            "failed_count": len(self.failed),
            "failed": self.failed[:FAILED_NODES_SHOWN],
        }


@dataclass(frozen=True)
class NodeState:
    """Состояние сторожа в том виде, в каком оно лежит в колонках servers.antiddos_*."""
    emergency: bool
    source: Optional[str]
    reason: Optional[str]
    watchdog: bool
    since: Optional[datetime]

    @classmethod
    def reported(cls, status: dict) -> "NodeState":
        since_epoch = int(status.get("since", 0) or 0)
        return cls(
            emergency=status.get("mode") == "on",
            source=status.get("source", "none"),
            reason=(status.get("reason") or "")[:200],
            watchdog=status.get("watchdog", "on") == "on",
            since=datetime.fromtimestamp(since_epoch, tz=timezone.utc) if since_epoch else None,
        )

    @classmethod
    def stored(cls, server: Server) -> "NodeState":
        return cls(
            emergency=bool(server.antiddos_emergency_mode),
            source=server.antiddos_source,
            reason=server.antiddos_reason,
            watchdog=bool(server.antiddos_watchdog),
            since=_as_utc(server.antiddos_since),
        )


def _as_utc(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=timezone.utc)


def _synced_recently(server: Server, now: datetime) -> bool:
    last_sync = _as_utc(server.antiddos_last_sync_at)
    return last_sync is not None and now - last_sync < STATUS_REFRESH_INTERVAL


class _OncePerPass:
    """Whitelist на один проход опроса: собирается при первой установке и
    достаётся всем остальным — сборка на каждую ноду держала бы event loop."""

    def __init__(self, build: Callable[[], Awaitable[list[str]]]):
        self._build = build
        self._task: Optional[asyncio.Task] = None

    async def get(self) -> list[str]:
        if self._task is None:
            self._task = asyncio.ensure_future(self._build())
        return await self._task


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
        # (до какого момента верить, проверенный сторож либо None — рассылать нечего)
        self._release_cache: Optional[tuple[float, Optional[WatchdogRelease]]] = None
        self._release_lock = asyncio.Lock()
        self._rollout: Optional[WatchdogRollout] = None
        self._last_action: Optional[FleetAction] = None
        self._fleet_tasks: set[asyncio.Task] = set()
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
        for task in (self._whitelist_task, self._status_task, self._requested_push_task, *self._fleet_tasks):
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
                data = resp.json()
                ok = bool(data.get("success"))
                return ok, "ok" if ok else str(data.get("message") or "node refused")
            return False, f"HTTP {resp.status_code}"
        except Exception as e:
            return False, str(e)

    async def _watchdog_release(self) -> Optional[WatchdogRelease]:
        """Сторож со своего канала, прошедший проверку. None — рассылать нечего:
        GitHub не ответил либо отдал негодный скрипт."""
        # Под замком: рассылка на парк иначе скачивала бы и проверяла скрипт
        # столько раз, сколько нод дошло до установки одновременно
        async with self._release_lock:
            now = asyncio.get_running_loop().time()
            if self._release_cache and now < self._release_cache[0]:
                return self._release_cache[1]

            files = await self._download_watchdog()
            if files is None:
                self._release_cache = (now + RELEASE_RETRY_INTERVAL, None)
                return None

            script, service = files
            problem = release_problem(script, service) or await bash_syntax_problem(script)
            release = None if problem else WatchdogRelease(script, service, watchdog_version(script))
            if problem:
                logger.error(f"Watchdog from GitHub is rejected, nothing goes to the nodes: {problem}")
            self._release_cache = (now + FILES_CACHE_TTL, release)
            return release

    async def _download_watchdog(self) -> Optional[tuple[str, str]]:
        try:
            client = get_external_client()
            configs_base = update_channel.github_configs_base()
            sh_resp, svc_resp = await asyncio.gather(
                client.get(f"{configs_base}/ddos-watchdog.sh", timeout=20.0),
                client.get(f"{configs_base}/ddos-watchdog.service", timeout=20.0),
            )
        except Exception as e:
            logger.error(f"Failed to fetch watchdog files: {e}")
            return None
        if sh_resp.status_code != 200 or svc_resp.status_code != 200:
            return None
        return sh_resp.text, svc_resp.text

    async def install_to_node(self, server: Server, timeout: float = 60.0) -> tuple[bool, str]:
        # Скрипт сторожа нода исполняет от root: кроме своего раздела ей нужен exec
        allowed, denied, _ = server_allows_path(server, WATCHDOG_INSTALL_PATH, "POST")
        if not allowed:
            return False, denied_message(denied, True)
        release = await self._watchdog_release()
        if release is None:
            return False, "no valid watchdog script from GitHub"
        try:
            async with async_session() as db:
                settings = await self.get_or_create_settings(db)
                watchdog_default = settings.watchdog_default_enabled
            client = get_node_apply_client(server)
            resp = await client.post(
                f"{server.url}{WATCHDOG_INSTALL_PATH}",
                headers=node_auth_headers(server),
                json={
                    "script_content": release.script,
                    "service_content": release.service,
                    "watchdog_enabled": watchdog_default,
                },
                timeout=timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    return True, "installed"
                return False, str(data.get("message") or "node failed to install the watchdog")
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
    def _fleet_targets(kind: FleetActionKind, servers: list[Server]) -> list[Server]:
        """Закрывшие раздел не считаются ни в «сколько нод», ни в «сколько
        удалось»; установке сторожа кроме раздела нужен ещё exec."""
        if kind is FleetActionKind.INSTALL:
            return [s for s in servers if server_allows_path(s, WATCHDOG_INSTALL_PATH, "POST")[0]]
        return [s for s in servers if server_allows(s, Capability.ANTIDDOS, write=True)]

    async def start_fleet_action(self, kind: FleetActionKind, enabled: Optional[bool] = None) -> dict:
        """Запустить команду на весь парк в фоне. Обход занимает до минут, и
        запрос его не ждёт: страница следит за ходом по записи о команде.

        Мастер-переключатель — команда WATCHDOG: он управляет только автодетектом
        и аварийный режим, закреплённый админом, не трогает."""
        return self._launch(kind, enabled, self._fleet_targets(kind, await self._active_servers()))

    async def retry_failed(self) -> Optional[dict]:
        """Повторить последнюю команду на нодах, где она не прошла. None —
        повторять нечего: команды не было, она ещё идёт или прошла везде."""
        last = self._last_action
        if last is None or last.finished_at is None or not last.failed:
            return None
        failed_ids = {entry["server_id"] for entry in last.failed}
        servers = [s for s in self._fleet_targets(last.kind, await self._active_servers()) if s.id in failed_ids]
        return self._launch(last.kind, last.enabled, servers)

    def last_action(self) -> Optional[dict]:
        return self._last_action.to_dict() if self._last_action else None

    def rollout_state(self) -> Optional[dict]:
        return self._rollout.to_dict() if self._rollout else None

    def _launch(self, kind: FleetActionKind, enabled: Optional[bool], servers: list[Server]) -> dict:
        action = FleetAction(kind, enabled, total=len(servers))
        self._last_action = action
        # Ссылка держит задачу: на фоновую без ссылок вправе прийти сборщик мусора
        task = asyncio.create_task(self.run_bg(self._run_fleet_action(action, servers)))
        self._fleet_tasks.add(task)
        task.add_done_callback(self._fleet_tasks.discard)
        return action.to_dict()

    async def _run_fleet_action(self, action: FleetAction, servers: list[Server]) -> None:
        async def _one(srv: Server) -> None:
            async with self._http_sem:
                try:
                    ok, message = await self._apply_to_node(action, srv)
                except Exception as e:  # noqa: BLE001 — граница обхода: сбой одной ноды не обрывает остальные
                    ok, message = False, str(e) or type(e).__name__
            action.record(srv, ok, message)

        try:
            await asyncio.gather(*[_one(s) for s in servers])
        finally:
            action.finished_at = datetime.now(timezone.utc)
        if action.failed:
            first = ", ".join(f"{entry['server_name']} ({entry['message']})" for entry in action.failed[:5])
            logger.warning(
                f"Anti-DDoS fleet action {action.kind.value} (enabled={action.enabled}): "
                f"{len(action.failed)} of {action.total} nodes failed, first: {first}"
            )

    async def _apply_to_node(self, action: FleetAction, server: Server) -> tuple[bool, str]:
        if action.kind is FleetActionKind.EMERGENCY:
            ok, message, status = await self.set_node_emergency(server, bool(action.enabled))
            if ok and status:
                await self._store_status(server.id, status)
            return ok, message
        if action.kind is FleetActionKind.WATCHDOG:
            return await self.set_node_watchdog(server, bool(action.enabled))
        return await self.install_to_node(server)

    # ── status persistence + alerting ──────────────────────────────────────

    async def _store_status(
        self, server_id: int, status: dict, alert: bool = False, known: Optional[Server] = None,
    ):
        """known — строка сервера, прочитанная в начале прохода опроса: по ней
        видно, что писать нечего, без запроса к базе."""
        reported = NodeState.reported(status)
        now = datetime.now(timezone.utc)
        if known is not None and NodeState.stored(known) == reported and _synced_recently(known, now):
            return

        async with async_session() as db:
            srv = (await db.execute(select(Server).where(Server.id == server_id))).scalar_one_or_none()
            if srv is None:
                return
            if NodeState.stored(srv) == reported and _synced_recently(srv, now):
                return
            was_on = bool(srv.antiddos_emergency_mode)
            srv.antiddos_emergency_mode = reported.emergency
            srv.antiddos_source = reported.source
            srv.antiddos_reason = reported.reason
            srv.antiddos_watchdog = reported.watchdog
            srv.antiddos_since = reported.since
            srv.antiddos_last_sync_at = now
            server_name = srv.name
            await db.commit()

        # alert only on an off→on transition that the node enabled itself
        if alert and reported.emergency and not was_on and reported.source == "auto":
            await self._send_alert(server_id, server_name, reported.reason)

    async def _notify(self, text: str) -> bool:
        """True — сообщение ушло в Telegram сейчас; не ушедшее из-за сети уйдёт позже само."""
        async with async_session() as db:
            alert_settings = (await db.execute(select(AlertSettings).limit(1))).scalar_one_or_none()
        token = getattr(alert_settings, "telegram_bot_token", None)
        chat_id = getattr(alert_settings, "telegram_chat_id", None)
        if not (token and chat_id):
            return False
        from app.services.telegram_bot import get_telegram_bot_service
        return await get_telegram_bot_service().send_message(token, chat_id, text, retry_later=True)

    async def _send_alert(self, server_id: int, server_name: str, reason: str):
        try:
            notified = await self._notify(
                f"\U0001f6e1️ <b>Anti-DDoS: аварийный режим</b>\n\n"
                f"Нода <b>{server_name}</b> автоматически включила защиту.\n"
                f"Причина: {reason or 'signal detected'}"
            )

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

    def _rollout_of(self, version: str) -> WatchdogRollout:
        if self._rollout is None or self._rollout.version != version:
            self._rollout = WatchdogRollout(version)
            # Отказ прежней версии не должен откладывать попытку новой
            self._install_cooldown.clear()
        return self._rollout

    async def _rollout_failure(self, rollout: WatchdogRollout, server: Server, message: str) -> None:
        logger.warning(f"Watchdog {rollout.version} on {server.name} failed: {message}")
        if not rollout.fail(server, message):
            return
        failed = list(rollout.failed.values())
        logger.error(
            f"Watchdog {rollout.version} rollout is halted: {len(failed)} nodes failed, "
            f"first: {failed[0]['server_name']} ({failed[0]['message']})"
        )
        lines = "\n".join(
            f"• <b>{html.escape(entry['server_name'])}</b>: {html.escape(entry['message'])}" for entry in failed[:5]
        )
        try:
            await self._notify(
                f"\U0001f6e1️ <b>Anti-DDoS: обновление сторожа остановлено</b>\n\n"
                f"Версия {html.escape(rollout.version)} не встала на {len(failed)} нодах, "
                f"на остальные она больше не раскатывается.\n{lines}"
            )
        except Exception as e:
            logger.error(f"Failed to send the watchdog rollout alert: {e}")

    async def _fail_unconfirmed(self, rollout: WatchdogRollout, servers: list[Server]) -> None:
        by_id = {server.id: server for server in servers}
        for server_id in rollout.overdue(asyncio.get_running_loop().time()):
            server = by_id.get(server_id)
            if server is None:
                rollout.awaiting.pop(server_id)  # сервер удалён или выключен — не отказ версии
                continue
            await self._rollout_failure(rollout, server, "no status from the node after the install")

    async def _maybe_auto_install(
        self,
        server: Server,
        status: dict,
        release: Optional[WatchdogRelease],
        whitelist: Callable[[], Awaitable[list[str]]],
    ) -> dict:
        """Zero-touch rollout: if a node has the new agent (/api/antiddos exists,
        so status came back) but the watchdog isn't installed — or it's an older
        version — the panel installs/updates it itself. No admin action, no per-node
        clicking. A per-node cooldown keeps a failing install from retrying every poll.

        Версия идёт канарейкой (см. WatchdogRollout): негодный скрипт успевает
        попасть на горстку нод, а не на весь парк.
        """
        # Проверка до cooldown: закрытая нода иначе получала бы попытку установки
        # каждые десять минут и до конца времён
        if release is None or not server_allows_path(server, WATCHDOG_INSTALL_PATH, "POST")[0]:
            return status

        rollout = self._rollout_of(release.version)
        installed = status.get("installed", False)
        version = status.get("version") or ""
        if installed and version == release.version:
            if status.get("watchdog_active", True):
                rollout.confirm(server.id)
            elif server.id in rollout.awaiting:
                await self._rollout_failure(rollout, server, "watchdog service stopped after the install")
            return status
        # Сторож стоит, но версию не назвал (сбой его `status`): менять вслепую нечего
        if installed and not version:
            return status
        if server.id in rollout.awaiting:
            await self._rollout_failure(rollout, server, f"node reports version {version or 'none'} after the install")

        now = asyncio.get_running_loop().time()
        last_attempt = self._install_cooldown.get(server.id)
        if last_attempt is not None and now - last_attempt < self.INSTALL_RETRY_INTERVAL:
            return status
        if not rollout.may_install(server.id):
            return status
        self._install_cooldown[server.id] = now
        rollout.started.add(server.id)

        ok, msg = await self.install_to_node(server)
        if not ok:
            await self._rollout_failure(rollout, server, msg)
            return status

        rollout.installed(server.id, asyncio.get_running_loop().time())
        logger.info(f"Auto-installed watchdog {release.version} on {server.name}")
        # give the freshly-installed node its whitelist immediately
        try:
            await self.sync_whitelist_to_node(server, await whitelist())
        except Exception as e:
            logger.warning(f"Post-install whitelist push to {server.name} failed: {e}")

        fresh = await self.get_node_status(server)
        return fresh if fresh is not None else status

    async def _load_whitelist(self) -> list[str]:
        async with self._db_sem:
            async with async_session() as db:
                return await self.build_whitelist(db)

    async def poll_status_all(self):
        servers = await self._active_servers()
        release = await self._watchdog_release()
        if release is not None:
            await self._fail_unconfirmed(self._rollout_of(release.version), servers)

        # Молчавшая на опросе метрик нода и здесь отдала бы только таймаут
        collector = get_collector()
        answering = [s for s in servers if collector._answered_last_poll(s.id)]
        whitelist = _OncePerPass(self._load_whitelist)

        async def _one(srv: Server):
            async with self._http_sem:
                status = await self.get_node_status(srv)
                if status is None:
                    return  # unreachable, or old agent without /api/antiddos
                status = await self._maybe_auto_install(srv, status, release, whitelist.get)
                await self._store_status(srv.id, status, alert=True, known=srv)

        results = await asyncio.gather(*[_one(s) for s in answering], return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            logger.warning(
                f"Anti-DDoS status poll: {len(errors)} of {len(answering)} nodes raised, first: {errors[0]!r}"
            )

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
