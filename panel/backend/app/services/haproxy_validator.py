"""Валидация HAProxy-конфига на стороне панели через `haproxy -c`.

Реальные TLS-сертификаты живут на нодах (`/etc/letsencrypt/live/...`), на панели их нет.
Поэтому перед проверкой все пути `crt` подменяются self-signed dummy-сертификатом —
проверяется синтаксис и структура конфига, а не наличие конкретных сертификатов.
Авторитетную проверку с реальными сертификатами всё равно делает нода перед применением.
"""

import asyncio
import logging
import re
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_VALIDATE_DIR = Path("/tmp/haproxy-validate")
_DUMMY_CERT = _VALIDATE_DIR / "dummy.pem"
_CRT_TOKEN = re.compile(r"(\bcrt\s+)(\S+)")
_CHECK_TIMEOUT = 30

_dummy_lock = asyncio.Lock()


async def _ensure_dummy_cert() -> str | None:
    """Генерирует self-signed combined.pem (cert+key) для подмены сертификатов при проверке."""
    if _DUMMY_CERT.exists():
        return str(_DUMMY_CERT)

    async with _dummy_lock:
        if _DUMMY_CERT.exists():
            return str(_DUMMY_CERT)

        _VALIDATE_DIR.mkdir(parents=True, exist_ok=True)
        key_path = _VALIDATE_DIR / "dummy.key"
        cert_path = _VALIDATE_DIR / "dummy.crt"

        proc = await asyncio.create_subprocess_exec(
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key_path), "-out", str(cert_path),
            "-days", "3650", "-subj", "/CN=haproxy-validate",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("Failed to generate dummy cert for validation: %s", stderr.decode().strip())
            return None

        combined = cert_path.read_text() + key_path.read_text()
        _DUMMY_CERT.write_text(combined)
        return str(_DUMMY_CERT)


def _strip_temp_path(error: str, tmp_path: str) -> str:
    """Убирает путь временного файла из текста ошибки, оставляя номер строки."""
    return error.replace(tmp_path, "config")


async def validate_config(config_content: str) -> tuple[bool, str]:
    """Проверяет конфиг HAProxy через `haproxy -c`.

    Возвращает (valid, message). Если бинарь haproxy недоступен — (True, ...) с пометкой о пропуске,
    чтобы не ломать панели, которые ещё не пересобраны с установленным haproxy.
    """
    haproxy_bin = shutil.which("haproxy")
    if not haproxy_bin:
        logger.warning("haproxy binary not found on panel — config validation skipped")
        return True, "validation skipped (haproxy not installed on panel)"

    dummy = await _ensure_dummy_cert()
    test_config = _CRT_TOKEN.sub(lambda m: f"{m.group(1)}{dummy}", config_content) if dummy else config_content

    tmp = tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False)
    try:
        tmp.write(test_config)
        tmp.close()

        proc = await asyncio.create_subprocess_exec(
            haproxy_bin, "-c", "-f", tmp.name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_CHECK_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            return False, "Validation timeout"

        if proc.returncode == 0:
            return True, "Configuration valid"

        error = (stderr.decode() or stdout.decode()).strip() or "Configuration check failed"
        return False, _strip_temp_path(error, tmp.name)
    finally:
        Path(tmp.name).unlink(missing_ok=True)
