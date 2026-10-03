"""Трасса от релея до адреса (кнопка «Трасса» на странице «Потери»).

Нода гоняет mtr в фоне (node services/path_trace), панель запускает её и раз в
секунду забирает накопленное — окно заполняется по ходу. Здесь же вывод: с
какого узла потери начинаются и держатся до самого адреса.

Как читается mtr: узел, который не отвечает совсем, — норма (роутер не шлёт
ICMP); потери на промежуточном узле, которые дальше пропадают, — тоже норма
(роутер ограничивает ответы). Настоящие потери — те, что начинаются на
каком-то узле и держатся до адреса назначения.
"""

import logging
from enum import Enum
from typing import Optional

import httpx

from app.models import Server
from app.services.http_client import get_node_client, node_auth_headers
from app.services.node_capabilities import learn_from_denial, server_allows_path

logger = logging.getLogger(__name__)

TRACE_PATH = "/api/loss-probe/trace"
TRACE_TIMEOUT_SEC = 15.0
# Потери на адресе ниже этого — обычный шум сети, путь считается чистым
CLEAN_LOSS_PCT = 5.0
# Узел входит в «участок с потерями», если теряет не меньше этой доли потерь адреса
CARRY_RATIO = 0.5
# Меньше раундов — вывод предварительный: несколько пакетов не дают процента
MIN_ROUNDS_FOR_VERDICT = 5


class TraceErrorCode(str, Enum):
    UNSUPPORTED = "unsupported"
    DENIED = "denied"
    UNREACHABLE = "unreachable"
    BUSY = "busy"
    NOT_FOUND = "not_found"


class TraceError(Exception):
    def __init__(self, code: TraceErrorCode, detail: str = ""):
        super().__init__(detail or code.value)
        self.code = code


class Verdict(str, Enum):
    WAITING = "waiting"
    CLEAN = "clean"
    LOSS_FROM = "loss_from"
    BROKEN_AFTER = "broken_after"
    NO_REPLIES = "no_replies"


def analyze_trace(hops: list[dict], target_ip: str, finished: bool, rounds_done: int) -> dict:
    """Вывод по трассе и номера узлов участка с потерями."""
    responding = [h for h in sorted(hops, key=lambda h: h["hop"]) if h.get("host") and h.get("received")]
    preliminary = not finished and rounds_done < MIN_ROUNDS_FOR_VERDICT
    result = {"verdict": Verdict.WAITING, "preliminary": preliminary, "start_hop": None,
              "prev_hop": None, "last_hop": None, "dest_loss": None, "problem_hops": [], "path_hidden": False}
    if not responding:
        if finished:
            result["verdict"] = Verdict.NO_REPLIES
        return result

    dest = next((h for h in reversed(responding) if h["host"] == target_ip), None)
    if dest is None:
        if finished:
            result.update(verdict=Verdict.BROKEN_AFTER, last_hop=responding[-1]["hop"])
        return result

    result["dest_loss"] = dest["loss_pct"]
    # Адрес ответил, а ни один роутер до него — нет: ответы роутеров до сервера не
    # доходят (так в облаках вроде VK Cloud), место потерь по такой трассе не найти
    result["path_hidden"] = dest["hop"] > 1 and all(h["host"] == target_ip for h in responding)
    if dest["loss_pct"] < CLEAN_LOSS_PCT:
        result["verdict"] = Verdict.CLEAN
        return result

    threshold = max(CLEAN_LOSS_PCT, dest["loss_pct"] * CARRY_RATIO)
    chain = [h for h in responding if h["hop"] <= dest["hop"]]
    start_index = len(chain) - 1
    while start_index > 0 and chain[start_index - 1]["loss_pct"] >= threshold:
        start_index -= 1
    result.update(
        verdict=Verdict.LOSS_FROM,
        start_hop=chain[start_index]["hop"],
        prev_hop=chain[start_index - 1]["hop"] if start_index > 0 else None,
        problem_hops=[h["hop"] for h in chain[start_index:]],
    )
    return result


def _node_error(response: httpx.Response) -> Optional[TraceErrorCode]:
    if response.status_code == 429:
        return TraceErrorCode.BUSY
    if response.status_code == 403:
        return TraceErrorCode.DENIED
    if response.status_code != 200:
        return TraceErrorCode.UNREACHABLE
    return None


def _json_or_none(response: httpx.Response) -> Optional[object]:
    try:
        return response.json()
    except ValueError:
        return None


async def start_trace(server: Server, ip: str, port: int) -> str:
    allowed, _, _ = server_allows_path(server, TRACE_PATH, "POST")
    if not allowed:
        raise TraceError(TraceErrorCode.DENIED)
    try:
        response = await get_node_client(server).post(
            f"{server.url}{TRACE_PATH}", headers=node_auth_headers(server),
            json={"ip": ip, "port": port}, timeout=TRACE_TIMEOUT_SEC,
        )
    except httpx.HTTPError as exc:
        raise TraceError(TraceErrorCode.UNREACHABLE, str(exc))
    if response.status_code == 404:
        raise TraceError(TraceErrorCode.UNSUPPORTED)
    error = _node_error(response)
    if error is TraceErrorCode.DENIED:
        await learn_from_denial(server.id, response.status_code, _json_or_none(response))
    body = _json_or_none(response)
    if error or not isinstance(body, dict) or "id" not in body:
        raise TraceError(error or TraceErrorCode.UNREACHABLE)
    logger.info("loss_trace_started server_id=%s target=%s:%s", server.id, ip, port)
    return str(body["id"])


async def fetch_trace(server: Server, trace_id: str) -> dict:
    try:
        response = await get_node_client(server).get(
            f"{server.url}{TRACE_PATH}/{trace_id}", headers=node_auth_headers(server), timeout=TRACE_TIMEOUT_SEC,
        )
    except httpx.HTTPError as exc:
        raise TraceError(TraceErrorCode.UNREACHABLE, str(exc))
    if response.status_code == 404:
        raise TraceError(TraceErrorCode.NOT_FOUND)
    error = _node_error(response)
    body = _json_or_none(response)
    if error or not isinstance(body, dict):
        raise TraceError(error or TraceErrorCode.UNREACHABLE)
    return body
