"""Исполнитель автоотключения: решения — релеям, ответы релеев — в решения.

Коллектор метрик после каждого цикла будит исполнителя. Тот считает решения
(backend_quarantine) по потерям из реестра и шлёт каждому затронутому релею
полный список его отключённых адресов: нода переводит их серверы HAProxy в
обслуживание на лету и держит ограниченный срок — замолчавшая панель никого не
оставит отключённым.

Запросы уходят только релеям, у которых есть что отключать или возвращать:
парк без проблемных адресов не получает ни одного. Пары хранятся в базе —
перезапуск панели не возвращает всё разом и не присылает уже известные
отключения заново.
"""

import asyncio
import json
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import httpx
from sqlalchemy import Delete, Select, delete, select

from app.database import async_session
from app.models import AlertHistory, AlertSettings, BackendQuarantine, Server
from app.services.backend_quarantine import (
    Event,
    Mode,
    NodeFailure,
    Pair,
    PairKey,
    PairState,
    QuarantineTracker,
    RestoreCause,
)
from app.services.backend_quarantine_messages import format_digest, history_records
from app.services.backend_quarantine_settings import QuarantineSettings, load_quarantine_settings
from app.services.haproxy_profile_sync import is_server_online
from app.services.http_client import get_node_client, node_auth_headers
from app.services.loss_registry import LossRegistry, get_loss_registry
from app.services.node_capabilities import Capability, server_allows
from app.services.reserved_ports_sync import _version_tuple
from app.services.telegram_bot import get_telegram_bot_service

logger = logging.getLogger(__name__)

MIN_NODE_VERSION_QUARANTINE = "10.36.0"
QUARANTINE_PATH = "/api/haproxy/runtime/quarantine"
RESEND_SECONDS = 60
# Срок на ноде: три пропущенные отправки — и она возвращает серверы сама
TTL_SECONDS = 3 * RESEND_SECONDS
PUSH_CONCURRENCY = 50
PUSH_TIMEOUT_SEC = 10.0
# Рукопожатие SOCKS5-прокси httpx-таймаутами не покрыто и способно повиснуть навсегда
PUSH_HARD_TIMEOUT_SEC = 15.0
NODE_MAX_TARGETS = 256
# Нода без эндпоинта спрашивается заново через этот срок: на dev-канале
# обновлённая нода сохраняет номер версии
UNSUPPORTED_RETRY_SECONDS = 600
# Таймеры пары: в базу не пишутся (иначе запись шла бы каждый цикл), после
# перезапуска панели затишье считается заново
VOLATILE_FIELDS = ("last_data_at", "calm_since")
# События копятся и уходят одной сводкой, когда самому старому исполнился этот
# срок: адреса одного сервера отключаются в соседних циклах, и без задержки это
# пачка сообщений подряд
NOTIFY_DELAY_SEC = 90.0
# Остановка панели не ждёт Telegram дольше этого
STOP_NOTIFY_TIMEOUT_SEC = 5.0

Notifier = Callable[[list[Event], QuarantineSettings], Awaitable[None]]


def pairs_select() -> Select:
    return select(BackendQuarantine)


def pairs_delete() -> Delete:
    return delete(BackendQuarantine)


class DbStorage:
    def __init__(self, session_factory=async_session):
        self._session_factory = session_factory

    async def load_servers(self, server_ids: set[int]) -> dict[int, Server]:
        async with self._session_factory() as db:
            rows = (await db.execute(
                select(Server).where(Server.id.in_(server_ids), Server.is_active == True)  # noqa: E712
            )).scalars().all()
        return {server.id: server for server in rows}

    async def load_pairs(self) -> dict[PairKey, dict]:
        async with self._session_factory() as db:
            rows = (await db.execute(pairs_select())).scalars().all()
        pairs = {}
        for row in rows:
            with suppress(ValueError):
                pairs[(row.relay_id, row.target)] = json.loads(row.payload)
        return pairs

    async def save_pairs(self, rows: dict[PairKey, dict]) -> None:
        async with self._session_factory() as db:
            await db.execute(pairs_delete())
            db.add_all([
                BackendQuarantine(relay_id=relay_id, target=target, state=row["state"], payload=json.dumps(row))
                for (relay_id, target), row in rows.items()
            ])
            await db.commit()

    async def load_settings(self) -> QuarantineSettings:
        async with self._session_factory() as db:
            return await load_quarantine_settings(db)


@dataclass(frozen=True)
class _Sent:
    targets: tuple[str, ...]
    max_share_pct: int
    drop_sessions: bool
    at: float


class QuarantineRunner:
    def __init__(
        self,
        storage=None,
        client_factory: Callable[[Server], httpx.AsyncClient] = get_node_client,
        clock: Callable[[], float] = time.time,
        registry: Optional[LossRegistry] = None,
        notifier: Optional[Notifier] = None,
        notify_delay: float = NOTIFY_DELAY_SEC,
    ):
        self._storage = storage or DbStorage()
        self._client_factory = client_factory
        self._clock = clock
        self._registry = registry or get_loss_registry()
        self._notifier = notifier
        self._notify_delay = notify_delay
        self._outbox: list[Event] = []
        self._outbox_since = 0.0
        self.tracker = QuarantineTracker()
        self._settings = QuarantineSettings()
        # Режим, в котором трекер считал в прошлый раз: смена режима возвращает всё отключённое
        self._mode = Mode.OFF
        self._sent: dict[int, _Sent] = {}
        self._forced: set[int] = set()
        # Нода, ответившая 404, не умеет отключать: версия агента и момент ответа
        self._unsupported: dict[int, tuple[Optional[str], float]] = {}
        self._failing: set[int] = set()
        # Релей, до которого не дошёл запрос, спрашивается заново не раньше этого момента
        self._retry_at: dict[int, float] = {}
        self._restored = False
        self._restore_failing = False
        self._startup_events: list[Event] = []
        self._saved: dict[PairKey, dict] = {}
        self._wake = asyncio.Event()
        # run_once и ручной возврат из API меняют один трекер
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None

    def wake(self) -> None:
        self._wake.set()

    def request_push(self, server_id: int) -> None:
        """После reload на релее все серверы снова в работе — прислать набор, не дожидаясь повтора."""
        self._forced.add(server_id)
        self._wake.set()

    def set_settings(self, settings: QuarantineSettings) -> None:
        self._settings = settings
        self._wake.set()

    async def restore(self) -> None:
        self._settings = await self._storage.load_settings()
        self._mode = self._settings.mode
        now = self._clock()
        pairs: dict[PairKey, Pair] = {}
        for key, row in (await self._storage.load_pairs()).items():
            try:
                pairs[key] = Pair.from_dict({**row, "last_data_at": now, "calm_since": None})
            except (KeyError, TypeError, ValueError):
                logger.warning("quarantine: stored pair %s is unreadable, skipped", key)
        self.tracker.pairs = pairs
        self._saved = self._storable()
        if self._mode is not Mode.ENFORCE:
            self._startup_events = self._return_everything(now)
        self._restored = True

    def _return_everything(self, now: float) -> list[Event]:
        """Режим сменили, а состояние сохранить не успели: в базе остались отключённые
        пары. В выключенном режиме возвращать их некому — без сброса релеи получали бы
        их список бессрочно. Релеи запоминаются как получившие список: первый же цикл
        пришлёт им пустой."""
        policy = self._settings.policy
        for relay_id, targets in self.tracker.desired().items():
            self._sent[relay_id] = _Sent(tuple(targets), policy.max_share_pct, policy.drop_sessions, now)
        return self.tracker.clear(now, RestoreCause.MODE)

    async def ensure_restored(self) -> bool:
        """Настройки и пары из базы; пока не поднялись, цикл ничего не решает —
        иначе панель работала бы с режимом «выключено» при включённом в базе."""
        if self._restored:
            return True
        try:
            await self.restore()
        except Exception:
            if not self._restore_failing:
                logger.exception("quarantine: state restore failed, will retry")
            self._restore_failing = True
            return False
        self._restore_failing = False
        return True

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        # Накопленное уходит сейчас: после перезапуска панели о нём никто не вспомнит
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._flush(self._settings, force=True), STOP_NOTIFY_TIMEOUT_SEC)

    async def _loop(self) -> None:
        while True:
            restored = await self.ensure_restored()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), RESEND_SECONDS)
            self._wake.clear()
            if not restored:
                continue
            try:
                await self.run_once()
            except Exception:
                logger.exception("quarantine cycle failed")

    async def run_once(self) -> None:
        async with self._lock:
            settings, now = self._settings, self._clock()
            events, self._startup_events = self._startup_events, []
            try:
                events += await self._decide(settings, now)
                events += await self._push(settings, now)
                await self._save()
            except Exception:
                # Решённое уже случилось: сбой базы или рассылки не оставляет его без сообщения
                logger.exception("quarantine cycle failed")
            self._queue(events, now)
        await self._flush(settings)

    async def release(self, server_id: int, target: str) -> bool:
        """Ручной возврат со страницы «Потери»; False — такой пары нет."""
        async with self._lock:
            pair = self.tracker.pairs.get((server_id, target))
            if pair is None or pair.state is PairState.IDLE:
                return False
            now = self._clock()
            self._queue(self.tracker.release(server_id, target, now), now)
            await self._save()
        self._wake.set()
        return True

    def _queue(self, events: list[Event], now: float) -> None:
        if events and not self._outbox:
            self._outbox_since = now
        self._outbox += events

    async def _flush(self, settings: QuarantineSettings, force: bool = False) -> None:
        now = self._clock()
        if not self._outbox or (not force and now - self._outbox_since < self._notify_delay):
            return
        batch = list(self._outbox)
        await self._notify(batch, settings)
        # Убирается после отправки: остановка панели посреди неё сводку не теряет — её дошлёт stop()
        del self._outbox[:len(batch)]
        self._outbox_since = now

    def view(self, owners: dict[str, str]) -> dict:
        settings = self._settings
        items = []
        for pair in sorted(self.tracker.pairs.values(), key=lambda p: (p.target, p.relay_name)):
            if pair.state is PairState.IDLE:
                continue
            ip, _, port = pair.target.rpartition(":")
            items.append({
                "target": pair.target,
                "ip": ip,
                "port": int(port),
                "owner": owners.get(ip),
                "server_id": pair.relay_id,
                "server_name": pair.relay_name,
                "state": pair.state.value,
                "reason": {"kind": pair.reason_kind.value, "loss_pct": pair.loss_pct, "downs": pair.downs},
                "disabled_at": pair.disabled_at,
                "until": pair.until if pair.state is PairState.DISABLED else None,
                "strikes": len(pair.strikes),
                "sessions": pair.sessions,
                "blocked_reason": pair.node.get("error") if pair.state is PairState.BLOCKED else None,
                "partial_reason": pair.node.get("partial") if pair.state is PairState.DISABLED else None,
            })
        chronic = self.tracker.chronic_targets(self._clock(), settings.policy)
        return {
            "mode": settings.mode.value,
            "items": items,
            "chronic": [self._chronic_entry(target, strikes, owners) for target, strikes in sorted(chronic.items())],
            "paused_relays": [
                {"server_id": relay_id, "name": pause.name, "since": pause.since}
                for relay_id, pause in sorted(self.tracker.paused_relays().items())
            ],
        }

    @staticmethod
    def _chronic_entry(target: str, strikes: int, owners: dict[str, str]) -> dict:
        # Адрес с постоянными проблемами показывается и когда он сейчас в работе —
        # строке страницы нужны его части и владелец
        ip, _, port = target.rpartition(":")
        return {"target": target, "ip": ip, "port": int(port), "owner": owners.get(ip), "strikes": strikes}

    def disabled_relays(self) -> dict[str, list[int]]:
        """Адрес → релеи, на которых он сейчас отключён."""
        relays: dict[str, list[int]] = {}
        for pair in list(self.tracker.pairs.values()):
            if pair.state is PairState.DISABLED:
                relays.setdefault(pair.target, []).append(pair.relay_id)
        return {target: sorted(relay_ids) for target, relay_ids in relays.items()}

    async def _decide(self, settings: QuarantineSettings, now: float) -> list[Event]:
        events: list[Event] = []
        if settings.mode is not self._mode:
            events += self.tracker.clear(now, RestoreCause.MODE)
            self._mode = settings.mode
        if settings.mode is Mode.OFF:
            return events
        # Обход замеров всего парка — десятки миллисекунд чистого Python на 2000 релеев:
        # в потоке он не останавливает цикл событий. Реестр читается здесь, а не в потоке —
        # коллектор пишет в него из цикла событий
        return events + await asyncio.to_thread(
            self.tracker.evaluate,
            self._registry.fresh(), self._registry.target_rules(), settings.exemptions(),
            settings.policy, settings.mode, now,
        )

    async def _push(self, settings: QuarantineSettings, now: float) -> list[Event]:
        # В наблюдении и после выключения трекер ничего не хочет сам: пары «отключил бы» в набор не идут
        desired = self.tracker.desired()
        # Релей без отключений остаётся в рассылке, пока не получит пустой список
        due = {
            relay_id: desired.get(relay_id, [])[:NODE_MAX_TARGETS]
            for relay_id in set(desired) | set(self._sent)
            if self._due(relay_id, desired.get(relay_id, []), settings, now)
        }
        self._forced.clear()
        if not due:
            return []
        servers = await self._storage.load_servers(set(due))
        semaphore = asyncio.Semaphore(PUSH_CONCURRENCY)

        async def deliver(relay_id: int, targets: list[str]) -> list[dict] | NodeFailure:
            async with semaphore:
                return await self._deliver(servers.get(relay_id), targets, settings, now)

        outcomes = await asyncio.gather(*(deliver(relay_id, targets) for relay_id, targets in due.items()))
        events: list[Event] = []
        for (relay_id, targets), outcome in zip(due.items(), outcomes):
            events += self._account(relay_id, targets, outcome, settings, now)
        return events

    def _due(self, relay_id: int, targets: list[str], settings: QuarantineSettings, now: float) -> bool:
        if relay_id in self._forced:
            return relay_id in self._sent or bool(targets)
        if now < self._retry_at.get(relay_id, 0.0):
            return False
        sent = self._sent.get(relay_id)
        if sent is None:
            return bool(targets)
        current = (tuple(targets[:NODE_MAX_TARGETS]), settings.policy.max_share_pct, settings.policy.drop_sessions)
        if current != (sent.targets, sent.max_share_pct, sent.drop_sessions):
            return True
        return now - sent.at >= RESEND_SECONDS

    def _account(self, relay_id: int, targets: list[str], outcome: list[dict] | NodeFailure,
                 settings: QuarantineSettings, now: float) -> list[Event]:
        events = self.tracker.apply_node_results(relay_id, outcome, settings.policy, now)
        if isinstance(outcome, NodeFailure):
            # Сбойный релей не опрашивается каждый цикл сбора метрик
            self._retry_at[relay_id] = now + RESEND_SECONDS
            sent = self._sent.get(relay_id)
            # Список не доставлен дольше срока ноды: она уже вернула серверы сама
            if sent is not None and now - sent.at >= TTL_SECONDS:
                del self._sent[relay_id]
            return events
        self._retry_at.pop(relay_id, None)
        if targets:
            self._sent[relay_id] = _Sent(
                tuple(targets), settings.policy.max_share_pct, settings.policy.drop_sessions, now,
            )
        else:
            self._sent.pop(relay_id, None)
        return events

    def _ineligible(self, server: Server, now: float) -> Optional[NodeFailure]:
        if not is_server_online(server):
            return NodeFailure.OFFLINE
        if not server_allows(server, Capability.HAPROXY, write=True):
            return NodeFailure.DENIED
        version = server.node_version
        if not version or _version_tuple(version) < _version_tuple(MIN_NODE_VERSION_QUARANTINE):
            return NodeFailure.UNSUPPORTED
        marked = self._unsupported.get(server.id)
        if marked and marked[0] == version and now - marked[1] < UNSUPPORTED_RETRY_SECONDS:
            return NodeFailure.UNSUPPORTED
        return None

    async def _deliver(self, server: Optional[Server], targets: list[str], settings: QuarantineSettings,
                       now: float) -> list[dict] | NodeFailure:
        if server is None:
            return NodeFailure.OFFLINE
        failure = self._ineligible(server, now)
        if failure is not None:
            return failure
        body = {
            "targets": [{"ip": ip, "port": int(port)} for ip, _, port in (t.rpartition(":") for t in targets)],
            "ttl_seconds": TTL_SECONDS,
            "max_share_pct": settings.policy.max_share_pct,
            "drop_sessions": settings.policy.drop_sessions,
        }
        try:
            response = await asyncio.wait_for(
                self._client_factory(server).put(
                    f"{server.url}{QUARANTINE_PATH}", json=body,
                    headers=node_auth_headers(server), timeout=PUSH_TIMEOUT_SEC,
                ),
                PUSH_HARD_TIMEOUT_SEC,
            )
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            self._warn_once(server, f"request failed: {e!r}")
            return NodeFailure.OFFLINE
        if response.status_code == 404:
            self._unsupported[server.id] = (server.node_version, now)
            logger.info("quarantine: node %s (%s) has no quarantine endpoint", server.name, server.node_version)
            return NodeFailure.UNSUPPORTED
        if response.status_code == 403:
            return NodeFailure.DENIED
        if response.status_code != 200:
            self._warn_once(server, f"HTTP {response.status_code}: {response.text[:200]}")
            return NodeFailure.ERROR
        try:
            results = response.json()["results"]
        except (ValueError, KeyError, TypeError):
            return NodeFailure.ERROR
        if not isinstance(results, list):
            return NodeFailure.ERROR
        self._failing.discard(server.id)
        return results

    def _warn_once(self, server: Server, reason: str) -> None:
        if server.id in self._failing:
            return
        self._failing.add(server.id)
        logger.warning("quarantine: push to %s failed: %s", server.name, reason)

    def _storable(self) -> dict[PairKey, dict]:
        return {
            key: {name: value for name, value in pair.to_dict().items() if name not in VOLATILE_FIELDS}
            for key, pair in self.tracker.pairs.items()
        }

    async def _save(self) -> None:
        current = self._storable()
        if current == self._saved:
            return
        try:
            await self._storage.save_pairs(current)
            self._saved = current
        except Exception:
            logger.exception("quarantine: state save failed")

    async def _notify(self, events: list[Event], settings: QuarantineSettings) -> None:
        if not events or self._notifier is None:
            return
        try:
            await self._notifier(events, settings)
        except Exception:
            logger.exception("quarantine: notification failed")


async def notify_events(events: list[Event], settings: QuarantineSettings) -> None:
    """Сводка в Telegram тем же ботом и чатом, что алерты, и строки истории алертов.
    История пишется всегда; Telegram — пока включены алерты и уведомления автоотключения."""
    owners = get_loss_registry().owners()
    async with async_session() as db:
        alerts = (await db.execute(select(AlertSettings).limit(1))).scalar_one_or_none()
    lang = ((alerts.language if alerts else None) or "en").lower()

    notified = False
    telegram_ready = alerts is not None and alerts.enabled and alerts.telegram_bot_token and alerts.telegram_chat_id
    if settings.notify and telegram_ready:
        service = get_telegram_bot_service()
        sent = [
            await service.send_message(
                alerts.telegram_bot_token, alerts.telegram_chat_id, text, retry_later=True
            )
            for text in format_digest(events, owners, lang)
        ]
        notified = all(sent)

    rows = [
        AlertHistory(
            server_id=record.server_id,
            server_name=record.server_name,
            alert_type=record.alert_type,
            severity=record.severity,
            message=record.message,
            details=json.dumps(record.details, ensure_ascii=False),
            notified=notified,
        )
        for record in history_records(events, owners, lang)
    ]
    async with async_session() as db:
        db.add_all(rows)
        await db.commit()


_runner: Optional[QuarantineRunner] = None


def get_quarantine_runner() -> QuarantineRunner:
    global _runner
    if _runner is None:
        _runner = QuarantineRunner(notifier=notify_events)
    return _runner
