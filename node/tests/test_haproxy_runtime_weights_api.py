"""Tests for the request model of PUT /api/haproxy/runtime/weights.

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Имена бэкенда и сервера уходят в строку команды CLI HAProxy как есть: пробел
или `;` превратили бы одну команду в несколько, поэтому шаблон имён строгий.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pydantic import ValidationError  # noqa: E402

from app.models.haproxy import RuntimeWeightsRequest  # noqa: E402


def body(backend="b", server="s", weight=1, ttl=180, count=1) -> dict:
    return {"weights": [{"backend": backend, "server": server, "weight": weight}] * count, "ttl_seconds": ttl}


class RequestModelTests(unittest.TestCase):
    def test_valid(self):
        request = RuntimeWeightsRequest(**body(backend="backend_tcp_lb", server="srv1_o2", weight=0))
        self.assertEqual((request.weights[0].server, request.weights[0].weight), ("srv1_o2", 0))

    def test_limits_accepted(self):
        RuntimeWeightsRequest(**body(weight=256, ttl=3600, count=4096))
        RuntimeWeightsRequest(**body(ttl=10))

    def test_rejects(self):
        for bad in (body(backend="a b"), body(server="s;x"), body(server="s\nshow"), body(weight=257),
                    body(weight=-1), body(ttl=5), body(ttl=3601), body(count=4097), body(server="s" * 129)):
            with self.assertRaises(ValidationError, msg=bad["weights"][0]):
                RuntimeWeightsRequest(**bad)


if __name__ == "__main__":
    unittest.main()
