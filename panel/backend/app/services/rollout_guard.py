"""Серверы, по которым раскатка профиля идёт прямо сейчас.

Раскатка помечает серверы «pending» и обходит их с ограниченной параллельностью,
поэтому на большом профиле длится минуты. Цикл досинхронизации тем временем ищет
«pending ∧ online» и без этой отметки принимал бы необработанные серверы за ожившие.
"""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager


class RolloutGuard:
    def __init__(self) -> None:
        self._busy: set[int] = set()

    @contextmanager
    def hold(self, server_ids: Iterable[int]) -> Iterator[None]:
        # Снимаем только свои отметки: сервер может участвовать в чужой раскатке
        taken = set(server_ids) - self._busy
        self._busy |= taken
        try:
            yield
        finally:
            self._busy -= taken

    def idle(self, server_ids: Iterable[int]) -> list[int]:
        return [server_id for server_id in server_ids if server_id not in self._busy]
