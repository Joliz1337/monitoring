"""Итог обновления ноды через её агента и запасной путь через SSH.

Агент обновляет себя сам и в фоне, а за ТСПУ может не скачать ни код с GitHub,
ни образ из GHCR: обновление проваливается, нода остаётся на старой версии, и
оператор об этом не узнаёт. Панель запоминает каждый запуск (NodeUpdateAttempt)
и опрашивает ноду, пока не узнает итог. При провале панель сама доставляет образ
по SSH, а если SSH-доступа нет или доставка тоже не удалась — пишет уведомление
в Telegram и в историю алертов.
"""

import asyncio
import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import and_, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import AlertHistory, AlertSettings, NodeUpdateAttempt, Server
from app.services import update_channel
from app.services.http_client import get_node_client, node_auth_headers
from app.services.node_capabilities import server_allows_path
from app.services.node_image_delivery import get_image_delivery_manager
from app.services.ssh_target import SSHTargetError, resolve_ssh_target
from app.services.telegram_bot import get_telegram_bot_service

logger = logging.getLogger(__name__)

UPDATE_STATUS_PATH = "/api/system/update/status"
POLL_INTERVAL = 30
START_DELAY = 20
CHECK_CONCURRENCY = 20
NODE_REQUEST_TIMEOUT = 15.0
# Потолок поверх httpx-таймаута: рукопожатие SOCKS5 им не покрыто, и повисший
# прокси одной ноды подвесил бы весь проход
NODE_REQUEST_HARD_TIMEOUT = 25.0

# Агент сам сдаётся через 2 ч (UPDATER_WAIT_TIMEOUT ноды) — даём ему отчитаться
ATTEMPT_DEADLINE = timedelta(hours=2, minutes=15)
# Пока качаются образы, нода работает на старой версии и отвечает, а рестарт на
# новую занимает секунды. Молчание дольше — агент после обновления не поднялся
SILENCE_LIMIT = timedelta(minutes=15)

STAGE_AGENT = "agent"
STAGE_SSH = "ssh"

ALERT_TYPE = "node_update_failed"
ATTEMPT_ID_MAX_LEN = 64
ERROR_SUMMARY_LINES = 2
ERROR_SUMMARY_CHARS = 300
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Общие строки «скрипт упал с кодом N» причину не называют
_GENERIC_ERROR_LINE = re.compile(r"exit code", re.IGNORECASE)


class Verdict(str, Enum):
    WAIT = "wait"
    SUCCESS = "success"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class FailureReason(str, Enum):
    REPORTED = "reported"
    IMAGE_UNAVAILABLE = "image_unavailable"
    SILENT = "silent"
    TIMEOUT = "timeout"
    VERSION_UNCHANGED = "version_unchanged"


class FallbackProblem(str, Enum):
    NO_CREDS = "no_creds"
    NOT_ROOT = "not_root"
    NO_HOST = "no_host"
    SSH_FAILED = "ssh_failed"
    SSH_INTERRUPTED = "ssh_interrupted"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: Optional[FailureReason] = None
    detail: Optional[str] = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def summarize_error(text: Optional[str]) -> str:
    """Пара строк из хвоста лога апдейтера, по которым видна причина."""
    if not text:
        return ""
    lines = [line.strip() for line in _ANSI_ESCAPE.sub("", text).splitlines() if line.strip()]
    errors = [line for line in lines if "ERROR" in line and not _GENERIC_ERROR_LINE.search(line)]
    return " / ".join((errors or lines)[-ERROR_SUMMARY_LINES:])[:ERROR_SUMMARY_CHARS]


def _wait_until_deadline(attempt: NodeUpdateAttempt, now: datetime) -> Decision:
    if now - attempt.started_at >= ATTEMPT_DEADLINE:
        return Decision(Verdict.FAILED, FailureReason.TIMEOUT)
    return Decision(Verdict.WAIT)


def _reported_failure(status: dict) -> Decision:
    if status.get("reason") == "image_unavailable":
        return Decision(Verdict.FAILED, FailureReason.IMAGE_UNAVAILABLE)
    return Decision(Verdict.FAILED, FailureReason.REPORTED, summarize_error(status.get("last_error")))


def _judge_reported(attempt: NodeUpdateAttempt, status: dict, now: datetime) -> Decision:
    result = status.get("last_result")
    if result == "success":
        return Decision(Verdict.SUCCESS)
    if result == "failed":
        return _reported_failure(status)
    return _wait_until_deadline(attempt, now)


def _judge_legacy(
    attempt: NodeUpdateAttempt, status: dict, node_version: Optional[str], now: datetime,
) -> Decision:
    if status.get("in_progress"):
        return _wait_until_deadline(attempt, now)
    if status.get("last_result") == "failed":
        return _reported_failure(status)
    # Успешное обновление перезапускает процесс старого агента, и итог он не
    # помнит — остаётся сверить версию. На dev-канале она между пушами может не
    # меняться, там признак успеха — только отсутствие сообщения о провале
    if attempt.target_ref == update_channel.DEV_BRANCH or not attempt.from_version:
        return Decision(Verdict.SUCCESS)
    if node_version is None:
        return _wait_until_deadline(attempt, now)
    if node_version != attempt.from_version:
        return Decision(Verdict.SUCCESS)
    return Decision(Verdict.FAILED, FailureReason.VERSION_UNCHANGED, node_version)


def decide(
    attempt: NodeUpdateAttempt, status: Optional[dict], node_version: Optional[str], now: datetime,
) -> Decision:
    """Итог попытки по ответу ноды на /api/system/update/status (None — не ответила)."""
    if status is None:
        silent_since = attempt.last_contact_at or attempt.started_at
        if now - silent_since >= SILENCE_LIMIT:
            minutes = int(SILENCE_LIMIT.total_seconds() // 60)
            return Decision(Verdict.FAILED, FailureReason.SILENT, str(minutes))
        return _wait_until_deadline(attempt, now)

    node_attempt = status.get("attempt_id")
    if attempt.attempt_id:
        if node_attempt == attempt.attempt_id:
            return _judge_reported(attempt, status, now)
        if node_attempt:
            # После нашего запуска обновление запустили снова — итог у той попытки
            return Decision(Verdict.SUPERSEDED)
    return _judge_legacy(attempt, status, node_version, now)


def _reason_text(lang: str, reason: FailureReason, detail: Optional[str]) -> str:
    deadline_min = int(ATTEMPT_DEADLINE.total_seconds() // 60)
    if lang == "ru":
        texts = {
            FailureReason.REPORTED: f"агент сообщил об ошибке: {detail}" if detail else "агент сообщил об ошибке",
            FailureReason.IMAGE_UNAVAILABLE: "образ недоступен из реестра",
            FailureReason.SILENT: f"нода не отвечает больше {detail} мин после запуска обновления",
            FailureReason.TIMEOUT: f"обновление не завершилось за {deadline_min} мин",
            FailureReason.VERSION_UNCHANGED: f"версия агента осталась {detail}",
        }
    else:
        texts = {
            FailureReason.REPORTED: f"the agent reported an error: {detail}" if detail else "the agent reported an error",
            FailureReason.IMAGE_UNAVAILABLE: "the image is unavailable from the registry",
            FailureReason.SILENT: f"the node has not responded for over {detail} min since the update started",
            FailureReason.TIMEOUT: f"the update did not finish within {deadline_min} min",
            FailureReason.VERSION_UNCHANGED: f"the agent version is still {detail}",
        }
    return texts[reason]


def _problem_text(lang: str, problem: FallbackProblem, detail: Optional[str]) -> str:
    if lang == "ru":
        texts = {
            FallbackProblem.NO_CREDS: "Обновить по SSH не получится: у сервера не сохранён SSH-доступ. "
                                      "Сохраните его на странице «Обновления» или обновите ноду вручную.",
            FallbackProblem.NOT_ROOT: "Обновить по SSH не получится: сохранённый SSH-доступ не root, а нужен root.",
            FallbackProblem.NO_HOST: "Обновить по SSH не получится: не удалось определить SSH-адрес сервера.",
            FallbackProblem.SSH_FAILED: f"Обновление по SSH тоже не удалось: {detail}",
            FallbackProblem.SSH_INTERRUPTED: "Обновление по SSH прервалось перезапуском панели — "
                                             "запустите его снова на странице «Обновления».",
        }
    else:
        texts = {
            FallbackProblem.NO_CREDS: "Updating over SSH is not possible: the server has no saved SSH access. "
                                      "Save it on the Updates page or update the node manually.",
            FallbackProblem.NOT_ROOT: "Updating over SSH is not possible: the saved SSH access is not root, "
                                      "and root is required.",
            FallbackProblem.NO_HOST: "Updating over SSH is not possible: the server's SSH address could not be determined.",
            FallbackProblem.SSH_FAILED: f"Updating over SSH failed too: {detail}",
            FallbackProblem.SSH_INTERRUPTED: "The SSH update was interrupted by a panel restart — "
                                             "start it again on the Updates page.",
        }
    return texts[problem]


def failure_message(
    lang: str,
    server_name: str,
    reason: FailureReason,
    detail: Optional[str],
    problem: FallbackProblem,
    problem_detail: Optional[str] = None,
) -> str:
    reason_text = _reason_text(lang, reason, detail)
    problem_text = _problem_text(lang, problem, problem_detail)
    if lang == "ru":
        return f"Сервер {server_name} не обновился: {reason_text}.\n{problem_text}"
    return f"Server {server_name} failed to update: {reason_text}.\n{problem_text}"


def _parse_reason(value: Optional[str]) -> FailureReason:
    try:
        return FailureReason(value)
    except ValueError:
        return FailureReason.REPORTED


async def track(db: AsyncSession, server: Server, target_ref: str, attempt_id: object) -> None:
    """Запомнить запуск обновления. Только на ветку канала: при откате на тег или
    коммит запасной путь довёз бы образ канала, а не запрошенную версию."""
    if target_ref not in update_channel.ALLOWED_BRANCHES:
        return
    now = _utcnow()
    values = {
        "attempt_id": attempt_id if isinstance(attempt_id, str) and len(attempt_id) <= ATTEMPT_ID_MAX_LEN else None,
        "target_ref": target_ref,
        "from_version": server.node_version,
        "started_at": now,
        "last_contact_at": now,
        "stage": STAGE_AGENT,
        "delivery_job_id": None,
        "failure_reason": None,
        "failure_detail": None,
    }
    stmt = pg_insert(NodeUpdateAttempt).values(server_id=server.id, **values)
    try:
        await db.execute(stmt.on_conflict_do_update(index_elements=["server_id"], set_=values))
        await db.commit()
    except SQLAlchemyError as e:
        # Обновление на ноде уже запущено — ответ оператору важнее слежки за итогом
        await db.rollback()
        logger.error(f"Failed to track node update for server {server.id}: {e}")


async def _node_get(server: Server, path: str) -> Optional[dict]:
    """GET к ноде; None — не ответила или ответила не 200."""
    try:
        resp = await asyncio.wait_for(
            get_node_client(server).get(
                f"{server.url}{path}", headers=node_auth_headers(server), timeout=NODE_REQUEST_TIMEOUT,
            ),
            timeout=NODE_REQUEST_HARD_TIMEOUT,
        )
    except Exception:  # noqa: BLE001 — граница: сеть, прокси, TLS — для итога всё равно «не ответила»
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _same_attempt(attempt: NodeUpdateAttempt):
    """Условие на строку именно этой попытки: повторный запуск переписывает started_at."""
    return and_(
        NodeUpdateAttempt.server_id == attempt.server_id,
        NodeUpdateAttempt.started_at == attempt.started_at,
    )


async def _close(attempt: NodeUpdateAttempt) -> bool:
    async with async_session() as db:
        result = await db.execute(delete(NodeUpdateAttempt).where(_same_attempt(attempt)))
        await db.commit()
    return result.rowcount > 0


async def _mark_ssh(attempt: NodeUpdateAttempt, job_id: str, decision: Decision) -> None:
    async with async_session() as db:
        await db.execute(
            update(NodeUpdateAttempt).where(_same_attempt(attempt)).values(
                stage=STAGE_SSH,
                delivery_job_id=job_id,
                failure_reason=decision.reason.value,
                failure_detail=(decision.detail or "")[:500] or None,
            )
        )
        await db.commit()


async def _touch(server_ids: list[int]) -> None:
    if not server_ids:
        return
    async with async_session() as db:
        await db.execute(
            update(NodeUpdateAttempt)
            .where(NodeUpdateAttempt.server_id.in_(server_ids))
            .values(last_contact_at=_utcnow())
        )
        await db.commit()


async def _notify(
    server: Server,
    reason: FailureReason,
    detail: Optional[str],
    problem: FallbackProblem,
    problem_detail: Optional[str] = None,
) -> None:
    try:
        async with async_session() as db:
            settings = (await db.execute(select(AlertSettings).limit(1))).scalar_one_or_none()
        lang = (settings.language or "en").lower() if settings else "en"
        message = failure_message(lang, server.name, reason, detail, problem, problem_detail)

        notified = False
        if settings and settings.telegram_bot_token and settings.telegram_chat_id:
            header = "Обновление ноды" if lang == "ru" else "Node update"
            notified = await get_telegram_bot_service().send_message(
                settings.telegram_bot_token,
                settings.telegram_chat_id,
                f"\U0001f534 <b>{header}</b>\n\n{html.escape(message)}",
            )

        async with async_session() as db:
            db.add(AlertHistory(
                server_id=server.id,
                server_name=server.name,
                alert_type=ALERT_TYPE,
                severity="warning",
                message=message,
                details=json.dumps({
                    "reason": reason.value,
                    "detail": detail,
                    "fallback": problem.value,
                    "fallback_detail": problem_detail,
                }, ensure_ascii=False),
                notified=notified,
            ))
            await db.commit()
    except Exception as e:  # noqa: BLE001 — уведомление не должно ронять наблюдатель
        logger.error(f"Node update notification failed for server {server.id}: {e}")


class NodeUpdateWatcher:
    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._slots = asyncio.Semaphore(CHECK_CONCURRENCY)

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("NodeUpdateWatcher started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("NodeUpdateWatcher stopped")

    async def _loop(self):
        await asyncio.sleep(START_DELAY)
        while self._running:
            try:
                await self._check_all()
            except Exception as e:
                logger.error(f"Node update watcher pass failed: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    async def _check_all(self):
        async with async_session() as db:
            rows = (await db.execute(
                select(NodeUpdateAttempt, Server).join(Server, Server.id == NodeUpdateAttempt.server_id)
            )).all()
        if not rows:
            return
        results = await asyncio.gather(*(self._check_guarded(attempt, server) for attempt, server in rows))
        await _touch([server_id for server_id in results if server_id is not None])

    async def _check_guarded(self, attempt: NodeUpdateAttempt, server: Server) -> Optional[int]:
        """Проверить одну попытку; id сервера — если нода ответила и итог ещё не известен."""
        async with self._slots:
            try:
                if not server.is_active:
                    await _close(attempt)
                    return None
                if attempt.stage == STAGE_SSH:
                    await self._check_delivery(attempt, server)
                    return None
                return await self._check_agent(attempt, server)
            except Exception as e:  # noqa: BLE001 — одна нода не должна срывать проход по остальным
                logger.error(f"Node update check failed for server {server.id}: {e}")
                return None

    async def _check_agent(self, attempt: NodeUpdateAttempt, server: Server) -> Optional[int]:
        allowed, _, _ = server_allows_path(server, UPDATE_STATUS_PATH)
        if not allowed:
            # Владелец закрыл раздел после запуска: итог не узнать, а молчание
            # отказа не должно выглядеть как провал и звать SSH
            await _close(attempt)
            return None

        status = await _node_get(server, UPDATE_STATUS_PATH)
        node_version = None
        if status is not None:
            node_version = status.get("version")
            if "version" not in status:
                version_info = await _node_get(server, "/api/version")
                node_version = version_info.get("version") if version_info else None

        decision = decide(attempt, status, node_version, _utcnow())
        if decision.verdict is Verdict.WAIT:
            return server.id if status is not None else None
        if decision.verdict is Verdict.FAILED:
            await self._fall_back(attempt, server, decision)
            return None
        if await _close(attempt) and decision.verdict is Verdict.SUCCESS:
            logger.info(f"Node {server.name} ({server.id}) updated by its agent")
        return None

    async def _fall_back(self, attempt: NodeUpdateAttempt, server: Server, decision: Decision) -> None:
        try:
            target = resolve_ssh_target(server)
        except SSHTargetError as exc:
            logger.warning(
                f"Node {server.name} ({server.id}) failed to update ({decision.reason.value}), "
                f"SSH fallback impossible: {exc.reason}"
            )
            if await _close(attempt):
                await _notify(server, decision.reason, decision.detail, FallbackProblem(exc.reason))
            return

        job_id = get_image_delivery_manager().start(
            server.id, server.name, target, update_channel.current_image_tag(),
        )
        await _mark_ssh(attempt, job_id, decision)
        logger.warning(
            f"Node {server.name} ({server.id}) failed to update ({decision.reason.value}), "
            f"updating over SSH: job {job_id}"
        )

    async def _check_delivery(self, attempt: NodeUpdateAttempt, server: Server) -> None:
        job = get_image_delivery_manager().get(attempt.delivery_job_id) if attempt.delivery_job_id else None
        if job is not None and job.finished_at is None:
            return
        if not await _close(attempt):
            return
        if job is not None and job.status == "success":
            logger.info(f"Node {server.name} ({server.id}) updated over SSH after a failed agent update")
            return

        reason = _parse_reason(attempt.failure_reason)
        if job is None:
            # Задачи доставки живут в памяти панели — её перезапуск их обрывает
            await _notify(server, reason, attempt.failure_detail, FallbackProblem.SSH_INTERRUPTED)
        else:
            await _notify(server, reason, attempt.failure_detail, FallbackProblem.SSH_FAILED, job.error)


_watcher: Optional[NodeUpdateWatcher] = None


def get_node_update_watcher() -> NodeUpdateWatcher:
    global _watcher
    if _watcher is None:
        _watcher = NodeUpdateWatcher()
    return _watcher


async def start_node_update_watcher():
    await get_node_update_watcher().start()


async def stop_node_update_watcher():
    await get_node_update_watcher().stop()
