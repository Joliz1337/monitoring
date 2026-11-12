"""Отправка бэкапа панели в Telegram: шифрование паролем → разбивка на тома → заливка.

Архив самодостаточен (несёт вшитый в .dump ключ шифрования секретов), поэтому
защищён паролем: AES-256-GCM, ключ из пароля через PBKDF2. Тома < 50 МБ — лимит
Telegram Bot API. Восстановление — через обычное окно восстановления панели:
пользователь выбирает все тома и вводит пароль, роутер собирает их по порядку и
расшифровывает (join_and_decrypt).
"""
import asyncio
import logging
import os
import re
from collections.abc import Sequence

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

_MAGIC = b"MTGB1\n"  # Monitoring Telegram Backup v1
_SALT_LEN = 16
_NONCE_LEN = 12
_PBKDF2_ITERS = 200_000
SEND_RETRIES = 3
SEND_RETRY_DELAY = 5
_VOLUME_SUFFIX = re.compile(r"\.(\d{3})$")


def is_encrypted_set(first_volume: bytes) -> bool:
    """Первый том набора из Telegram начинается с сигнатуры; сырой pg_dump — с «PGDMP»."""
    return first_volume.startswith(_MAGIC)


def missing_volume_numbers(filenames: list[str]) -> list[int]:
    """Номера томов, пропущенных в наборе (`…enc.001`, `.003` → не хватает 2).

    Пустой список — набор без дыр или имена не похожи на тома. Отсутствие
    последнего тома по именам не видно — его выявит проверка подлинности AES-GCM.
    """
    numbers = [int(m.group(1)) for name in filenames if (m := _VOLUME_SUFFIX.search(name))]
    if len(numbers) != len(filenames) or not numbers:
        return []
    present = set(numbers)
    return [n for n in range(1, max(present) + 1) if n not in present]


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=SHA256(), length=32, salt=salt, iterations=_PBKDF2_ITERS)
    return kdf.derive(password.encode("utf-8"))


class _Volumes(Sequence[bytes]):
    """Тома архива «заголовок + шифртекст», собираемые по обращению.

    Готовый список томов — вторая полная копия архива на всё время заливки, а
    склейка заголовка с шифртекстом перед нарезкой — третья. Здесь в памяти
    лежит один шифртекст и тот том, который уходит сейчас.
    """

    def __init__(self, header: bytes, ciphertext: bytes, volume_bytes: int) -> None:
        self._header = header
        self._ciphertext = memoryview(ciphertext)
        self._volume_bytes = volume_bytes
        self._starts = range(0, len(header) + len(ciphertext), volume_bytes)

    def __len__(self) -> int:
        return len(self._starts)

    def __getitem__(self, index: int | slice) -> bytes | list[bytes]:
        if isinstance(index, slice):
            return [self._volume_from(start) for start in self._starts[index]]
        return self._volume_from(self._starts[index])

    def _volume_from(self, start: int) -> bytes:
        """Том с байта start архива; заголовок короче тома и целиком лежит в первом."""
        end = start + self._volume_bytes - len(self._header)
        if start == 0:
            return b"".join((self._header, self._ciphertext[:end]))
        return bytes(self._ciphertext[start - len(self._header):end])


def encrypt_and_split(data: bytes, password: str, volume_bytes: int) -> Sequence[bytes]:
    """Зашифровать данные паролем и разбить на тома по volume_bytes байт."""
    if not password:
        raise ValueError("Пароль архива обязателен")
    if volume_bytes < 1024:
        raise ValueError("Размер тома слишком мал")
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(_derive_key(password, salt)).encrypt(nonce, data, None)
    return _Volumes(_MAGIC + salt + nonce, ciphertext, volume_bytes)


def join_and_decrypt(parts: list[bytes], password: str) -> bytes:
    """Собрать тома по порядку и расшифровать паролем."""
    blob = b"".join(parts)
    if not blob.startswith(_MAGIC):
        raise ValueError("Не набор томов бэкапа (нет сигнатуры) или тома перепутаны")
    body = blob[len(_MAGIC):]
    salt, nonce, ct = body[:_SALT_LEN], body[_SALT_LEN:_SALT_LEN + _NONCE_LEN], body[_SALT_LEN + _NONCE_LEN:]
    return AESGCM(_derive_key(password, salt)).decrypt(nonce, ct, None)


async def send_message(token: str, chat_id: str, text: str) -> bool:
    """Текстовое сообщение в канал (алерты, тест). True — доставлено."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": text},
            )
            return resp.status_code == 200 and bool(resp.json().get("ok"))
    except httpx.HTTPError as e:
        logger.warning(f"Telegram sendMessage failed: {e}")
        return False


async def send_volumes(
    token: str, chat_id: str, parts: Sequence[bytes], filename_base: str, caption: str
) -> None:
    """Залить тома в канал по порядку с ретраями. Кидает RuntimeError при провале."""
    async with httpx.AsyncClient(timeout=600) as client:
        for idx, part in enumerate(parts, 1):
            data = {"chat_id": chat_id}
            if idx == 1 and caption:
                data["caption"] = caption
            files = {"document": (f"{filename_base}.{idx:03d}", part)}
            for attempt in range(1, SEND_RETRIES + 1):
                try:
                    resp = await client.post(
                        f"https://api.telegram.org/bot{token}/sendDocument",
                        data=data, files=files,
                    )
                    if resp.status_code == 200 and resp.json().get("ok"):
                        break
                except httpx.HTTPError as e:
                    logger.warning(f"Telegram sendDocument том {idx} попытка {attempt}: {e}")
                if attempt == SEND_RETRIES:
                    raise RuntimeError(f"Telegram: том {idx}/{len(parts)} не отправлен после {SEND_RETRIES} попыток")
                await asyncio.sleep(SEND_RETRY_DELAY)
