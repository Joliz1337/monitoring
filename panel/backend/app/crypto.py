"""Шифрование секретов панели at-rest (AES-256-GCM).

Ключ живёт в env PANEL_ENC_KEY (base64 32 байта), не в БД: утёкший дамп базы
сам по себе не расшифровывается. Значение без префикса enc:v1: — легаси-открытый
текст, читается как есть (нужно для перехода и миграции существующих секретов).
"""
import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

ENC_PREFIX = "enc:v1:"
_NONCE_LEN = 12


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
