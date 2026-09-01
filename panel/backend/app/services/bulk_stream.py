"""NDJSON-стрим массовых операций по нодам: start → result по мере готовности → done.

Общий транспорт для bulk-разделов панели (SSH Security, Wildcard SSL): держит
одно HTTP-соединение вместо синхронного gather, поэтому фронт видит живой
прогресс и не упирается в клиентские таймауты на больших флотах.
"""
import asyncio
import json
import logging

from fastapi.responses import StreamingResponse

from app.models import Server

logger = logging.getLogger(__name__)


def ndjson_line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


def stream_ndjson(servers: list[Server], worker, log_action: str | None = None) -> StreamingResponse:
    """Стримит NDJSON: start → result по каждой ноде (по мере готовности) → done."""
    async def safe_worker(server):
        # Граница стрима: необработанное исключение одной ноды не должно
        # обрывать соединение — иначе остальные строки навсегда зависают в «загрузке»
        try:
            return await worker(server)
        except Exception as e:
            logger.exception("bulk_stream_worker_failed", extra={"server_id": server.id})
            return {
                "server_id": server.id,
                "server_name": server.name,
                "success": False,
                "reachable": False,
                "error": str(e) or e.__class__.__name__,
            }

    async def generate():
        yield ndjson_line({
            "type": "start",
            "total": len(servers),
            "servers": [{"server_id": s.id, "server_name": s.name} for s in servers],
        })
        tasks = [asyncio.create_task(safe_worker(s)) for s in servers]
        results: list[dict] = []
        try:
            for completed in asyncio.as_completed(tasks):
                result = await completed
                results.append(result)
                yield ndjson_line({"type": "result", **result})
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            raise
        if log_action:
            log_bulk_summary(log_action, results)
        ok = sum(1 for r in results if r.get("success", r.get("reachable", False)))
        yield ndjson_line({"type": "done", "total": len(servers), "ok": ok, "failed": len(servers) - ok})

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def log_bulk_summary(action: str, results: list[dict]) -> None:
    total = len(results)
    ok = sum(1 for r in results if r.get("success"))
    failed = total - ok
    failed_names = [r["server_name"] for r in results if not r.get("success")]

    if failed == 0:
        logger.info(
            "bulk_stream_summary",
            extra={"action": action, "total": total, "ok": ok, "failed": 0},
        )
    else:
        logger.warning(
            "bulk_stream_summary",
            extra={
                "action": action,
                "total": total,
                "ok": ok,
                "failed": failed,
                "failed_servers": failed_names,
            },
        )
