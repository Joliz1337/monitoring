"""Тесты разбора ответа агента ноды на выполнение команды.

Голый unittest, без сети — ответ ноды подменяется моком.

Появились после реальной поломки: панель читала из ответа поле `output`,
которого у агента нет (он отдаёт `stdout`/`stderr`). Установка исполнителя при
этом проходила, но панель считала её провалом и показывала ошибку с пустым
текстом. Контракт зеркалится вручную, поэтому и проверяется отдельно.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.xray_test import node_runner  # noqa: E402
from app.services.xray_test.node_runner import NodeExecError, _run_command  # noqa: E402


class _Server:
    id = 1
    name = "node-1"
    url = "https://node.example:9100"
    pki_enabled = True
    api_key = None


def _response(status=200, payload=None, text=""):
    response = mock.Mock()
    response.status_code = status
    response.json = mock.Mock(return_value=payload or {})
    response.text = text
    return response


def _patched(response):
    client = mock.Mock(post=mock.AsyncMock(return_value=response))
    return (
        mock.patch.object(node_runner, "get_node_client", return_value=client),
        mock.patch.object(node_runner, "node_auth_headers", return_value={}),
        mock.patch.object(node_runner, "learn_from_denial", new=mock.AsyncMock()),
    )


class RunCommandTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, response):
        patches = _patched(response)
        for patch in patches:
            patch.start()
        try:
            return await _run_command(_Server(), "echo hi")
        finally:
            for patch in patches:
                patch.stop()

    async def test_stdout_is_returned(self):
        """Агент отдаёт stdout — не output: на этом раздел и сломался."""
        result = await self._run(_response(payload={
            "success": True, "exit_code": 0,
            "stdout": "1.0.0\n", "stderr": "", "execution_time_ms": 5,
        }))
        self.assertEqual(result, "1.0.0")

    async def test_failure_reports_stderr(self):
        with self.assertRaises(NodeExecError) as ctx:
            await self._run(_response(payload={
                "success": False, "exit_code": 127,
                "stdout": "", "stderr": "bash: line 1: base64: not found",
                "execution_time_ms": 3,
            }))
        message = str(ctx.exception)
        self.assertIn("127", message)
        self.assertIn("base64: not found", message)

    async def test_failure_without_stderr_is_still_readable(self):
        """Пустая ошибка в интерфейсе — то, ради чего этот тест и написан."""
        with self.assertRaises(NodeExecError) as ctx:
            await self._run(_response(payload={
                "success": False, "exit_code": 1, "stdout": "", "stderr": "",
            }))
        self.assertTrue(str(ctx.exception).strip())
        self.assertIn("не дала вывода", str(ctx.exception))

    async def test_error_field_used_when_present(self):
        with self.assertRaises(NodeExecError) as ctx:
            await self._run(_response(payload={
                "success": False, "exit_code": -1, "stdout": "", "stderr": "",
                "error": "timeout after 60s",
            }))
        self.assertIn("timeout after 60s", str(ctx.exception))

    async def test_http_error_includes_body(self):
        with self.assertRaises(NodeExecError) as ctx:
            await self._run(_response(status=403, text='{"error":"capability_denied"}'))
        message = str(ctx.exception)
        self.assertIn("403", message)
        self.assertIn("capability_denied", message)


if __name__ == "__main__":
    unittest.main()
