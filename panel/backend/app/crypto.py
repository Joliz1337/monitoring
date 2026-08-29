"""Шифрование секретов панели at-rest (AES-256-GCM).

Ключ живёт в env PANEL_ENC_KEY (base64 32 байта), не в БД: утёкший дамп базы
сам по себе не расшифровывается. Значение без префикса enc:v1: — легаси-открытый
текст, читается как есть (нужно для перехода и миграции существующих секретов).
"""
import base64
import binascii
import logging
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

logger = logging.getLogger(__name__)

ENC_PREFIX = "enc:v1:"
_NONCE_LEN = 12
_PANEL_ENV_PATH = Path("/opt/monitoring-panel/.env")


class EncryptionUnavailable(RuntimeError):
    """Ключ шифрования не задан, а секрет требуется записать/зашифровать."""


_key_cache: bytes | None = None
_key_loaded = False


def reload_key() -> None:
    """Сбросить кэш ключа — перечитать PANEL_ENC_KEY из окружения.

    Нужно после restore из бэкапа: восстановление может сменить ключ на тот, что
    ехал в наборе (иначе восстановленные секреты не расшифровались бы)."""
    global _key_cache, _key_loaded
    _key_cache = None
    _key_loaded = False


_reset_cache_for_tests = reload_key  # алиас для тестов


def _read_env_key(env_path: Path) -> str | None:
    try:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("PANEL_ENC_KEY="):
                    return line.split("=", 1)[1].strip() or None
    except OSError:
        pass
    return None


def _write_env_key(env_path: Path, key: str) -> bool:
    try:
        lines = env_path.read_text().splitlines() if env_path.exists() else []
        for i, line in enumerate(lines):
            if line.startswith("PANEL_ENC_KEY="):
                lines[i] = f"PANEL_ENC_KEY={key}"
                break
        else:
            lines.append(f"PANEL_ENC_KEY={key}")
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("\n".join(lines) + "\n")
        return True
    except OSError as e:
        logger.error("Could not persist PANEL_ENC_KEY to .env: %s", e)
        return False


def ensure_key(env_path: Path = _PANEL_ENV_PATH) -> None:
    """Гарантировать наличие PANEL_ENC_KEY: окружение → .env → сгенерировать.

    Вызывать на старте ДО init_db. Делает бэкенд самодостаточным: даже если
    провижининг установщиком не отработал или ключ не долетел в окружение
    контейнера, шифрование и миграция всё равно работают. Идемпотентно — существующий
    ключ переиспользуется (иначе прошлый шифртекст стал бы нечитаемым)."""
    if _load_key() is not None:
        return

    from_file = _read_env_key(env_path)
    if from_file:
        os.environ["PANEL_ENC_KEY"] = from_file
        reload_key()
        if _load_key() is not None:
            logger.info("PANEL_ENC_KEY подхвачен из .env")
            return

    new_key = base64.b64encode(os.urandom(32)).decode("ascii")
    if _write_env_key(env_path, new_key):
        logger.warning("PANEL_ENC_KEY отсутствовал — сгенерирован и записан в .env")
    os.environ["PANEL_ENC_KEY"] = new_key
    reload_key()


def _load_key() -> bytes | None:
    global _key_cache, _key_loaded
    if _key_loaded:
        return _key_cache
    raw = os.environ.get("PANEL_ENC_KEY", "").strip()
    key: bytes | None = None
    if raw:
        try:
            decoded = base64.b64decode(raw, validate=True)
            if len(decoded) == 32:
                key = decoded
        except (ValueError, binascii.Error):
            key = None
    _key_cache = key
    _key_loaded = True
    return key


def encryption_enabled() -> bool:
    return _load_key() is not None


def encrypt_secret(plaintext: str) -> str:
    key = _load_key()
    if key is None:
        raise EncryptionUnavailable("PANEL_ENC_KEY not set — cannot store secret")
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return ENC_PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def decrypt_secret(stored: str | None) -> str | None:
    if stored is None:
        return None
    if not stored.startswith(ENC_PREFIX):
        return stored  # легаси-открытый текст
    key = _load_key()
    if key is None:
        return None
    try:
        blob = base64.b64decode(stored[len(ENC_PREFIX):], validate=True)
        nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
    except (ValueError, InvalidTag, binascii.Error, UnicodeDecodeError):
        return None


class EncryptedString(TypeDecorator):
    """Прозрачно шифрует строковое поле. В БД — Text (enc:v1:… или легаси-плейнтекст)."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt_secret(value)

    def process_result_value(self, value, dialect):
        return decrypt_secret(value)
