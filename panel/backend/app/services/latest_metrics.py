"""Последние ответы нод — в памяти процесса.

Последний ответ ноды читает десяток мест панели: дашборд, алерты, белые списки,
веса HAProxy. Пока он жил только в `servers.last_metrics`, коллектор переписывал
эту колонку у всего парка каждый цикл (основной объём журнала WAL), а каждый
читатель поднимал её из базы и разбирал JSON заново. Бэкенд — один процесс,
поэтому источник правды для читателей здесь, а база хранит редкую копию,
с которой хранилище поднимается после перезапуска.

Словари общие для всех читателей: менять их нельзя, нужна правка — копируйте.
"""

import json
from collections.abc import Iterable
from typing import Optional


class LatestMetrics:
    def __init__(self) -> None:
        self._by_server: dict[int, dict] = {}

    def __len__(self) -> int:
        return len(self._by_server)

    def put(self, server_id: int, metrics: dict) -> None:
        self._by_server[server_id] = metrics

    def get(self, server_id: int) -> Optional[dict]:
        return self._by_server.get(server_id)

    def get_many(self, server_ids: Iterable[int]) -> dict[int, dict]:
        return {
            server_id: self._by_server[server_id]
            for server_id in server_ids
            if server_id in self._by_server
        }

    def forget(self, server_id: int) -> None:
        self._by_server.pop(server_id, None)

    def load(self, saved: Iterable[tuple[int, Optional[str]]]) -> None:
        """Тёплый старт из сохранённых в базе ответов; свежие ответы не затирает."""
        for server_id, raw in saved:
            if not raw or server_id in self._by_server:
                continue
            try:
                metrics = json.loads(raw)
            except ValueError:
                continue
            if isinstance(metrics, dict):
                self._by_server[server_id] = metrics


_latest_metrics = LatestMetrics()


def get_latest_metrics() -> LatestMetrics:
    return _latest_metrics
