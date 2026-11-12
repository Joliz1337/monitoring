"""Тесты хранилища последних ответов нод (app/services/latest_metrics.py).

Голый unittest. Последний ответ ноды нужен десятку мест панели, а в базу он
переписывался у всего парка каждый цикл сбора — это основной объём журнала WAL.
Читатели берут его из памяти процесса, база хранит копию только для старта.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.latest_metrics import LatestMetrics  # noqa: E402


class LatestMetricsTest(unittest.TestCase):
    def test_latest_answer_replaces_the_previous_one(self):
        store = LatestMetrics()
        store.put(1, {"cpu": {"usage_percent": 10}})
        store.put(1, {"cpu": {"usage_percent": 70}})
        self.assertEqual(store.get(1), {"cpu": {"usage_percent": 70}})

    def test_unknown_node_has_no_metrics(self):
        self.assertIsNone(LatestMetrics().get(404))

    def test_several_nodes_are_read_at_once_without_the_unknown_ones(self):
        store = LatestMetrics()
        store.put(1, {"a": 1})
        store.put(2, {"a": 2})
        self.assertEqual(store.get_many([1, 2, 3]), {1: {"a": 1}, 2: {"a": 2}})

    def test_deleted_node_is_forgotten(self):
        store = LatestMetrics()
        store.put(1, {"a": 1})
        store.forget(1)
        self.assertIsNone(store.get(1))
        self.assertEqual(len(store), 0)

    def test_warm_start_takes_saved_answers_and_skips_broken_ones(self):
        store = LatestMetrics()
        store.load([(1, json.dumps({"a": 1})), (2, None), (3, "{broken"), (4, "[]")])
        self.assertEqual(store.get_many([1, 2, 3, 4]), {1: {"a": 1}})

    def test_warm_start_does_not_overwrite_a_fresh_answer(self):
        store = LatestMetrics()
        store.put(1, {"fresh": True})
        store.load([(1, json.dumps({"fresh": False}))])
        self.assertEqual(store.get(1), {"fresh": True})


if __name__ == "__main__":
    unittest.main()
