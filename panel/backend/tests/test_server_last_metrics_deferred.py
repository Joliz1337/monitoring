"""Копия ответа ноды в `servers.last_metrics` не загружается обычным запросом серверов.

Голый unittest, без PostgreSQL: проверяется текст запросов.

Колонка весит ~15 КБ на сервер. Каждый `select(Server)` по парку из 2000 нод тянул
из базы 30 МБ, хотя читает её только прогрев хранилища ответов при старте панели —
и тот отдельным запросом по имени колонки.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402

from app.models import Server  # noqa: E402

APP_DIR = Path(__file__).resolve().parent.parent / "app"
COLUMN_MENTION = re.compile(r"\b(\w+)\.last_metrics\b")
# Колонка модели в запросе и имя таблицы в комментариях — не чтение с объекта
NOT_AN_INSTANCE = {"Server", "servers"}


def sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


class LastMetricsColumnTest(unittest.TestCase):
    def test_plain_server_query_leaves_the_saved_answer_in_the_database(self):
        self.assertNotIn("last_metrics", sql(select(Server)))

    def test_every_other_column_is_still_loaded(self):
        query = sql(select(Server))

        for column in Server.__table__.columns:
            if column.name != "last_metrics":
                self.assertIn(f"servers.{column.name}", query)

    def test_saved_answer_is_readable_when_asked_for_by_name(self):
        self.assertIn("servers.last_metrics", sql(select(Server.id, Server.last_metrics)))

    def test_saved_answer_can_be_set_on_a_new_server(self):
        saved = Server(name="nl-1", url="https://10.0.0.1:9100", last_metrics='{"cpu": {}}')

        self.assertEqual(saved.last_metrics, '{"cpu": {}}')

    def test_no_code_reads_the_saved_answer_from_a_loaded_server(self):
        # У загруженного сервера колонки нет: обращение к ней в async-сессии — это
        # ленивый запрос и MissingGreenlet в рантайме. Читать можно только запросом
        # по имени колонки (Server.last_metrics).
        offenders = [
            f"{path.relative_to(APP_DIR)}:{number}: {line.strip()}"
            for path in sorted(APP_DIR.rglob("*.py"))
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            for owner in COLUMN_MENTION.findall(line)
            if owner not in NOT_AN_INSTANCE
        ]

        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
