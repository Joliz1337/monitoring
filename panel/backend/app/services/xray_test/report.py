"""Отдача результата наружу по мере готовности.

Проверки идут пачками, но ждать всю пачку нельзя: таблица заполнялась бы
рывками, а на медленной точке — вообще молчала бы минутами. Раннеры зовут этот
приёмник сразу, как только вердикт по ячейке готов.
"""
from __future__ import annotations

from typing import Callable, Optional

from app.services.xray_test.models import CellResult

ResultSink = Optional[Callable[[CellResult], None]]


def report(result: CellResult, sink: ResultSink) -> CellResult:
    """Отдать результат приёмнику и вернуть его же — удобно писать в одну строку."""
    if sink is not None:
        sink(result)
    return result
