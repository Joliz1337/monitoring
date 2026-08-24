"""Тесты отделения блокировки по пути от неподходящих параметров ключа.

Голый unittest, без сети: TLS-проба подменяется моком.

У REALITY есть запасной ход — «неправильному» клиенту сервер отдаёт настоящий
сайт-маскировку. Значит живой и достижимый сервер обязан ответить на обычное
TLS-рукопожатие со своим SNI. Если при живом TCP-порте молчит даже оно, дело не
в ключе: соединение душат по пути. Проверено на реальных серверах — рукопожатие
не проходило при TCP-пинге в 36-77 мс.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.xray_test import probes, runner  # noqa: E402
from app.services.xray_test.matrix import build_matrix  # noqa: E402
from app.services.xray_test.models import (  # noqa: E402
    CellResult,
    FailReason,
    ProbeTimings,
    TlsInfo,
    Verdict,
)
from app.services.xray_test.parsers import parse_link  # noqa: E402

UUID = "11111111-2222-3333-4444-555555555555"
PBK = "p0by5Raay70X-2hllYoFctRFOd6ONT7y9RPWz2KAHUU"


def _cell(link: str = ""):
    return build_matrix([parse_link(
        link or f"vless://{UUID}@h.io:443?security=reality&pbk={PBK}&sni=eh.vk.com#node"
    )])[0]


def _failed(tcp_ms=40.0, tls=None):
    return CellResult(
        index=0, remark="", protocol="vless", address="h.io", port=443,
        sni="eh.vk.com", transport="tcp", security="reality",
        verdict=Verdict.FAIL, reason=FailReason.PROXY_HANDSHAKE_FAILED,
        timings=ProbeTimings(tcp_min_ms=tcp_ms), tls_info=tls,
    )


class ExplainTest(unittest.IsolatedAsyncioTestCase):
    async def _explain(self, result, tls: TlsInfo, cell=None):
        with mock.patch.object(probes, "inspect_tls", new=mock.AsyncMock(return_value=tls)):
            return await runner.LocalCoreRunner()._explain(cell or _cell(), result)

    async def test_silent_masking_site_means_block(self):
        result = await self._explain(_failed(), TlsInfo(reachable=False, error="таймаут"))

        self.assertIs(result.reason, FailReason.DPI_BLOCK)
        self.assertEqual(result.hint, "DPI_BLOCK")

    async def test_answering_masking_site_means_key_params(self):
        """Сервер ответил маскировкой — он жив, значит не подходит ключ."""
        result = await self._explain(_failed(), TlsInfo(reachable=True, issuer="WE1"))

        self.assertIs(result.reason, FailReason.PROXY_HANDSHAKE_FAILED)
        self.assertEqual(result.hint, "KEY_PARAMS")

    async def test_dead_port_is_not_a_block(self):
        """Без живого TCP это обычная недоступность, а не удушение."""
        result = await self._explain(_failed(tcp_ms=None), TlsInfo(reachable=False))
        self.assertIsNot(result.reason, FailReason.DPI_BLOCK)

    async def test_working_cell_untouched(self):
        ok = _failed()
        ok.verdict = Verdict.OK
        ok.reason = None
        result = await self._explain(ok, TlsInfo(reachable=False))

        self.assertIs(result.verdict, Verdict.OK)
        self.assertIsNone(result.reason)

    async def test_plain_config_without_tls_skipped(self):
        """Без TLS запасного хода нет — проверять нечего."""
        cell = _cell(f"vless://{UUID}@h.io:443?type=tcp#plain")
        result = await self._explain(_failed(), TlsInfo(reachable=False), cell=cell)
        self.assertIsNot(result.reason, FailReason.DPI_BLOCK)

    async def test_existing_tls_probe_reused(self):
        """Повторно дёргать сеть незачем, если проба уже сделана."""
        probe = mock.AsyncMock(return_value=TlsInfo(reachable=True))
        with mock.patch.object(probes, "inspect_tls", new=probe):
            await runner.LocalCoreRunner()._explain(
                _cell(), _failed(tls=TlsInfo(reachable=True, issuer="WE1"))
            )
        probe.assert_not_awaited()

    async def test_error_text_kept_as_detail(self):
        result = await self._explain(
            _failed(), TlsInfo(reachable=False, error="таймаут TLS-рукопожатия")
        )
        self.assertIn("таймаут", result.detail)


if __name__ == "__main__":
    unittest.main()
