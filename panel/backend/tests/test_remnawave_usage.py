"""Разбор ответа Remnawave о расходе трафика пользователя.

Цифра решает, отправлять ли уведомление об аномалии IP, поэтому важно различать
«трафика нет» (ноль, уведомление подавляется) и «данных нет» (None, уведомление уходит).
Формы ответа у разных версий эндпоинта bandwidth-stats разные.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # локальный прогон без установленного рантайма панели
    from app.services.remnawave_api import sum_usage_bytes  # noqa: E402
except ImportError as e:  # pragma: no cover
    raise unittest.SkipTest(f"remnawave client requires the panel runtime: {e}")


class SumUsageBytesTest(unittest.TestCase):
    def test_legacy_flat_list(self):
        payload = {"response": [
            {"nodeName": "de-1", "total": 1000, "date": "2026-08-08"},
            {"nodeName": "nl-1", "total": 2500, "date": "2026-08-09"},
        ]}
        self.assertEqual(sum_usage_bytes(payload), 3500)

    def test_series_object(self):
        payload = {"response": {
            "categories": ["2026-08-08"],
            "sparklineData": [1, 2],
            "topNodes": [{"name": "de-1", "total": 999}],
            "series": [{"name": "de-1", "total": 4000, "data": [1, 2]}],
        }}
        self.assertEqual(sum_usage_bytes(payload), 4000)

    def test_top_nodes_when_series_empty(self):
        payload = {"response": {"series": [], "topNodes": [{"name": "de-1", "total": 700}]}}
        self.assertEqual(sum_usage_bytes(payload), 700)

    def test_empty_list_is_zero_traffic(self):
        self.assertEqual(sum_usage_bytes({"response": []}), 0)

    def test_float_and_broken_totals_are_tolerated(self):
        payload = {"response": [
            {"total": 1024.7},
            {"total": None},
            {"total": "nope"},
            "not a dict",
        ]}
        self.assertEqual(sum_usage_bytes(payload), 1024)

    def test_unknown_shape_is_no_data(self):
        self.assertIsNone(sum_usage_bytes({"response": {"message": "not found"}}))
        self.assertIsNone(sum_usage_bytes({}))
        self.assertIsNone(sum_usage_bytes(None))


if __name__ == "__main__":
    unittest.main()
