"""Тесты разбора вывода ядра: из простыни лога — короткая причина отказа.

Голый unittest, без сети. Строки взяты из живого вывода Xray 26.3.27.

Смысл в том, что оператору бесполезно «трафик через прокси не проходит»: ему
нужно знать, сервер отказал, сертификат не тот или параметры REALITY не
совпали. Ядро это печатает, но внутри цепочки обёрток и только на уровне info.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.xray_test.core_output import (  # noqa: E402
    clean_line,
    detect_hint,
    extract_reason,
    is_noise,
)

CERT_MISMATCH = (
    "2026/08/24 20:12:56.776502 [Info] [658930597] app/proxyman/outbound: "
    "app/proxyman/outbound: failed to process outbound traffic > "
    "proxy/vless/outbound: failed to find an available destination > common/retry: "
    "[x509: certificate is valid for cloudflare-dns.com, *.cloudflare-dns.com, "
    "one.one.one.one, not google.com] > common/retry: all retry attempts failed"
)
REFUSED = (
    "2026/08/24 20:13:00.154334 [Info] [3435388851] app/proxyman/outbound: "
    "failed to process outbound traffic > proxy/vless/outbound: failed to find an "
    "available destination > common/retry: [dial tcp 127.0.0.1:9: connectex: No "
    "connection could be made because the target machine actively refused it.] > "
    "common/retry: all retry attempts failed"
)
STARTUP = """Xray 26.3.27 (Xray, Penetrates Everything.) d2758a0
A unified platform for anti-censorship.
2026/08/24 20:12:16 [Info] infra/conf/serial: Reading config: &{Name:/tmp/cfg.json}
2026/08/24 20:12:16 [Warning] core: Xray 26.3.27 started
2026/08/24 20:12:17 from tcp:127.0.0.1:51470 accepted tcp:cp.cloudflare.com:443"""


class NoiseTest(unittest.TestCase):
    def test_startup_lines_are_noise(self):
        for line in STARTUP.splitlines():
            self.assertTrue(is_noise(line), line)

    def test_failure_line_is_not_noise(self):
        self.assertFalse(is_noise(CERT_MISMATCH))

    def test_clean_line_strips_prefixes(self):
        cleaned = clean_line("2026/08/24 20:12:56.776502 [Info] [658930597] boom")
        self.assertEqual(cleaned, "boom")


class ExtractReasonTest(unittest.TestCase):
    def test_certificate_mismatch(self):
        detail, hint = extract_reason(CERT_MISMATCH)

        self.assertIn("certificate is valid for", detail)
        self.assertIn("not google.com", detail)
        self.assertNotIn("app/proxyman", detail)
        self.assertNotIn("all retry attempts failed", detail)
        self.assertEqual(hint, "CERT_MISMATCH")

    def test_connection_refused(self):
        detail, hint = extract_reason(REFUSED)

        self.assertIn("actively refused", detail)
        self.assertEqual(hint, "CONN_REFUSED")


    def test_truncated_bracket_still_parsed(self):
        """Хвост лога режется по длине, и закрывающая скобка часто не доезжает."""
        detail, hint = extract_reason(
            "app/proxyman/outbound: failed to process outbound traffic > "
            "proxy/vless/outbound: failed to find an available destination > common/retry: "
            "[transport/internet/grpc: failed to dial gRPC > transport/internet/grpc: "
            "Cannot dial gRPC > rpc error: code = Unavailable desc = connection"
        )
        self.assertIn("Unavailable", detail)
        self.assertNotIn("app/proxyman", detail)
        self.assertEqual(hint, "GRPC_UNAVAILABLE")

    def test_nested_wrappers_reduced_to_last_segment(self):
        """Обёртки живут и внутри скобок — показываем последний сегмент."""
        detail, _ = extract_reason(
            "failed to process outbound traffic > common/retry: "
            "[transport/internet/grpc: failed to dial gRPC > rpc error: code = Unavailable "
            'desc = connection error: desc = "context deadline exceeded"] > '
            "common/retry: all retry attempts failed"
        )
        self.assertTrue(detail.startswith("rpc error"))
        self.assertNotIn("transport/internet/grpc: failed to dial", detail)

    def test_retry_summary_never_becomes_reason(self):
        detail, _ = extract_reason(
            "failed to dial > common/retry: [dial tcp: i/o timeout] > "
            "common/retry: all retry attempts failed"
        )
        self.assertNotIn("all retry attempts", detail)
        self.assertIn("i/o timeout", detail)

    def test_startup_only_gives_nothing(self):
        """Рабочий вывод без ошибок не должен выдаваться за причину отказа."""
        detail, hint = extract_reason(STARTUP)
        self.assertEqual(detail, "")
        self.assertIsNone(hint)

    def test_empty_output(self):
        self.assertEqual(extract_reason(""), ("", None))

    def test_last_failure_wins(self):
        detail, _ = extract_reason(REFUSED + "\n" + CERT_MISMATCH)
        self.assertIn("certificate", detail)

    def test_detail_is_trimmed(self):
        long_line = "failed: [" + "x" * 900 + "]"
        detail, _ = extract_reason(long_line)
        self.assertLessEqual(len(detail), 300)

    def test_line_without_brackets_kept_whole(self):
        detail, _ = extract_reason("2026/01/01 00:00:00 [Info] failed to dial: broken pipe")
        self.assertIn("broken pipe", detail)


class DetectHintTest(unittest.TestCase):
    def test_known_patterns(self):
        cases = {
            "x509: certificate is valid for a.com, not b.com": "CERT_MISMATCH",
            "certificate signed by unknown authority": "CERT_UNTRUSTED",
            "dial tcp: connection refused": "CONN_REFUSED",
            "read: connection reset by peer": "CONN_RESET",
            "dial tcp: i/o timeout": "IO_TIMEOUT",
            "lookup example.com: no such host": "DNS_FAIL",
            "invalid request user": "AUTH_FAILED",
            "REALITY: processed invalid connection": "REALITY_REJECTED",
            "[SSL: WRONG_VERSION_NUMBER] wrong version number": "PROTOCOL_MISMATCH",
            "connect: network is unreachable": "NO_ROUTE",
        }
        for text, expected in cases.items():
            self.assertEqual(detect_hint(text), expected, text)

    def test_unknown_text_has_no_hint(self):
        self.assertIsNone(detect_hint("что-то совсем неожиданное"))

    def test_certificate_mismatch_wins_over_generic_x509(self):
        """Частный шаблон должен побеждать общий, иначе подсказка будет размытой."""
        self.assertEqual(
            detect_hint("x509: certificate is valid for a.com, not b.com"), "CERT_MISMATCH"
        )


if __name__ == "__main__":
    unittest.main()
