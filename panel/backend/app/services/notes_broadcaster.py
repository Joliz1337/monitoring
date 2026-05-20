import asyncio
import json
import logging

logger = logging.getLogger(__name__)


class NotesBroadcaster:
    """Fan-out broadcaster для реалтайм-обновлений заметок через SSE.

    Каждый подключённый SSE-клиент получает свою asyncio.Queue.
    При сохранении заметки обновление пушится во все очереди.
    """

    def __init__(self):
        self._listeners: dict[int, asyncio.Queue] = {}
        self._counter = 0

    def subscribe(self) -> tuple[int, asyncio.Queue]:
        self._counter += 1
        lid = self._counter
        queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._listeners[lid] = queue
        logger.info("Notes SSE: listener %d connected (%d total)", lid, len(self._listeners))
        return lid, queue

    def unsubscribe(self, listener_id: int):
        self._listeners.pop(listener_id, None)
        logger.info("Notes SSE: listener %d disconnected (%d total)", listener_id, len(self._listeners))

    async def broadcast(self, event_type: str, payload: dict):
        message = json.dumps({"type": event_type, **payload})
        dead = []
        for lid, queue in self._listeners.items():
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                dead.append(lid)
        for lid in dead:
            self._listeners.pop(lid, None)
            logger.warning("Notes SSE: dropped slow listener %d", lid)


_broadcaster = NotesBroadcaster()


def get_notes_broadcaster() -> NotesBroadcaster:
    return _broadcaster
