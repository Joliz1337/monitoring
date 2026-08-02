"""Валидация nginx-конфига Remnawave на стороне панели через `nginx -t`.

Реальные TLS-сертификаты живут на нодах (`/etc/letsencrypt/live/...`), на панели
их нет, а вместо домена в шаблоне стоит {{DOMAIN}}. Поэтому перед проверкой
плейсхолдер подменяется фиктивным доменом, а пути сертификатов — self-signed
dummy-файлами: проверяется синтаксис и структура, а не наличие сертификатов.
Авторитетную проверку с реальными сертификатами делает нода перед применением
(`docker exec remnawave-nginx nginx -t`).
"""

import asyncio
import logging
import re
import shutil
import tempfile
from pathlib import Path

from app.services.remnawave_nginx_config import DOMAIN_PLACEHOLDER

logger = logging.getLogger(__name__)

_VALIDATE_DIR = Path("/tmp/remnawave-nginx-validate")
_DUMMY_CERT = _VALIDATE_DIR / "dummy.crt"
_DUMMY_KEY = _VALIDATE_DIR / "dummy.key"
_CHECK_TIMEOUT = 30
_VALIDATE_DOMAIN = "validate.invalid"

_SSL_KEY_TOKEN = re.compile(r"(\bssl_certificate_key\s+)(\S+?)(\s*;)")
_SSL_CERT_TOKEN = re.compile(
    r"(\b(?:ssl_certificate|ssl_client_certificate|ssl_trusted_certificate)\s+)(\S+?)(\s*;)"
)

_dummy_lock = asyncio.Lock()


async def _ensure_dummy_cert() -> bool:
    if _DUMMY_CERT.exists() and _DUMMY_KEY.exists():
        return True

    async with _dummy_lock:
        if _DUMMY_CERT.exists() and _DUMMY_KEY.exists():
            return True

        _VALIDATE_DIR.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(_DUMMY_KEY), "-out", str(_DUMMY_CERT),
            "-days", "3650", "-subj", f"/CN={_VALIDATE_DOMAIN}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("Failed to generate dummy cert for nginx validation: %s",
                           stderr.decode().strip())
            return False
        return True


def _strip_temp_path(error: str, tmp_path: str) -> str:
    return error.replace(tmp_path, "config")


async def validate_config(config_content: str) -> tuple[bool, str]:
    """Проверяет конфиг через `nginx -t`.

    Возвращает (valid, message). Если бинарь nginx недоступен — (True, ...)
    с пометкой о пропуске, чтобы не ломать панели без пересборки образа.
    """
    nginx_bin = shutil.which("nginx")
    if not nginx_bin:
        logger.warning("nginx binary not found on panel — config validation skipped")
        return True, "validation skipped (nginx not installed on panel)"

    test_config = config_content.replace(DOMAIN_PLACEHOLDER, _VALIDATE_DOMAIN)
    if await _ensure_dummy_cert():
        test_config = _SSL_KEY_TOKEN.sub(rf"\g<1>{_DUMMY_KEY}\g<3>", test_config)
        test_config = _SSL_CERT_TOKEN.sub(rf"\g<1>{_DUMMY_CERT}\g<3>", test_config)

    tmp = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
    try:
        tmp.write(test_config)
        tmp.close()

        proc = await asyncio.create_subprocess_exec(
            nginx_bin, "-t", "-q", "-c", tmp.name,
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
