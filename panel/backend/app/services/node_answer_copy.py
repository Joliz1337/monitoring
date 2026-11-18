"""Копии ответов ноды для страницы сервера (таблица node_answer_copies).

Часть страницы сервера панель показывает прямо из ответа ноды: лимит полосы,
адреса интерфейсов, находки сканера доступов хостера. В базе панели этого нет,
поэтому у лежащей ноды карточки крутили загрузку до таймаута и оставались
пустыми. Каждый удачный ответ теперь оседает здесь, и молчащей ноде есть что
показать — с пометкой, когда копия снята.

Копия пишется по запросу пользователя (открыл страницу, поменял лимит), а не
фоновым обходом парка: фоновый обход гонял бы по всем нодам команды хоста ради
страниц, которые никто не открывает.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from time import monotonic
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NodeAnswerCopy

logger = logging.getLogger(__name__)


class AnswerKind(str, Enum):
    BANDWIDTH_LIMIT = "bandwidth_limit"
    NETWORK_STATE = "network_state"
    HOSTER_SCAN = "hoster_scan"
    HAPROXY_CONFIG = "haproxy_config"
    DNAT_STATE = "dnat_state"


# Когда копия писалась в последний раз (по monotonic). Нужна страницам, которые
# опрашивают ноду раз в секунды: без неё каждая переписывала бы одну и ту же
# строку базы на каждый ответ. Живёт в памяти единственного процесса бэкенда
_last_kept: dict[tuple[int, AnswerKind], float] = {}


@dataclass(frozen=True)
class AnswerCopy:
    data: dict[str, Any]
    saved_at: datetime


async def keep_copy(
    db: AsyncSession,
    server_id: int,
    kind: AnswerKind,
    answer: dict[str, Any],
    min_interval_sec: float = 0.0,
) -> None:
    """Запомнить ответ ноды. Сбой записи не должен стоить пользователю самого
    ответа: нода его уже прислала, копия — только запас на будущее.

    `min_interval_sec` — не переписывать копию чаще: ответ, пришедший раньше
    срока, пропускается, и копия отстаёт от ноды не больше чем на этот срок.
    """
    kept_at = _last_kept.get((server_id, kind))
    now = monotonic()
    if min_interval_sec and kept_at is not None and now - kept_at < min_interval_sec:
        return
    saved_at = datetime.now(timezone.utc)
    payload = json.dumps(answer, ensure_ascii=False)
    statement = pg_insert(NodeAnswerCopy).values(
        server_id=server_id, kind=kind.value, data=payload, saved_at=saved_at,
    ).on_conflict_do_update(
        index_elements=[NodeAnswerCopy.server_id, NodeAnswerCopy.kind],
        set_={"data": payload, "saved_at": saved_at},
    )
    try:
        await db.execute(statement)
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.warning(f"node_answer_copy_not_saved server_id={server_id} kind={kind.value}: {exc}")
        return
    _last_kept[(server_id, kind)] = now


def expect_change(server_id: int, kind: AnswerKind) -> None:
    """Состояние на ноде только что изменили: следующий её ответ пойдёт в копию
    сразу, не дожидаясь `min_interval_sec` — иначе копия минуту показывала бы прежнее."""
    _last_kept.pop((server_id, kind), None)


async def load_copy(db: AsyncSession, server_id: int, kind: AnswerKind) -> Optional[AnswerCopy]:
    result = await db.execute(
        select(NodeAnswerCopy.data, NodeAnswerCopy.saved_at).where(
            NodeAnswerCopy.server_id == server_id,
            NodeAnswerCopy.kind == kind.value,
        )
    )
    row = result.one_or_none()
    if row is None:
        return None
    try:
        data = json.loads(row.data)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return AnswerCopy(data=data, saved_at=row.saved_at)
