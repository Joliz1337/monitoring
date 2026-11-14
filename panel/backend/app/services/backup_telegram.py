"""Отправка бэкапа панели в Telegram: шифрование паролем → разбивка на тома → заливка.

Архив самодостаточен (несёт вшитый в .dump ключ шифрования секретов), поэтому
защищён паролем: AES-256-GCM, ключ из пароля через PBKDF2. Тома < 50 МБ — лимит
Telegram Bot API. Восстановление — через обычное окно восстановления панели:
пользователь выбирает все тома и вводит пароль, роутер собирает их по порядку и
расшифровывает (decrypt_volumes).

Архив — «сигнатура, соль, nonce, шифртекст, тег GCM» одним потоком байтов,
нарезанным на тома. Шифрование и расшифровка идут потоком с диска на диск:
бэкап в сотни мегабайт целиком в память не попадает.
"""
import asyncio
import io
import logging
import os
import re
from collections.abc import Iterator, Sequence
from typing import BinaryIO

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

_MAGIC = b"MTGB1\n"  # Monitoring Telegram Backup v1
_SALT_LEN = 16
_NONCE_LEN = 12
_TAG_LEN = 16
_HEADER_LEN = len(_MAGIC) + _SALT_LEN + _NONCE_LEN
_PBKDF2_ITERS = 200_000
_READ_CHUNK_BYTES = 1024 * 1024
SEND_RETRIES = 3
SEND_RETRY_DELAY = 5
_VOLUME_SUFFIX = re.compile(r"\.(\d{3})$")


def is_encrypted_set(first_volume: BinaryIO) -> bool:
    """Первый том набора из Telegram начинается с сигнатуры; сырой pg_dump — с «PGDMP»."""
    first_volume.seek(0)
    signature = first_volume.read(len(_MAGIC))
    first_volume.seek(0)
    return signature == _MAGIC


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


class EncryptedVolumes:
    """Тома архива, которые шифруются по мере чтения файла бэкапа.

    AES-GCM — потоковый шифр: шифртекст той же длины, что открытый текст, тег
    подлинности дописывается в конец. Байты выходят те же, что при шифровании
    одним вызовом, но в памяти лежит только том, который уходит сейчас, а
    число томов известно заранее по размеру файла.
    """

    def __init__(self, source: BinaryIO, password: str, volume_bytes: int) -> None:
        if not password:
            raise ValueError("Пароль архива обязателен")
        if volume_bytes < 1024:
            raise ValueError("Размер тома слишком мал")
        self._source = source
        self._password = password
        self._volume_bytes = volume_bytes
        self._salt = os.urandom(_SALT_LEN)
        self._nonce = os.urandom(_NONCE_LEN)
        self._archive_len = _HEADER_LEN + source.seek(0, os.SEEK_END) + _TAG_LEN

    def __len__(self) -> int:
        return -(-self._archive_len // self._volume_bytes)

    def __iter__(self) -> Iterator[bytes]:
        key = _derive_key(self._password, self._salt)
        encryptor = Cipher(algorithms.AES(key), modes.GCM(self._nonce)).encryptor()
        pieces = [_MAGIC + self._salt + self._nonce]
        room = self._volume_bytes - _HEADER_LEN
        self._source.seek(0)
        while chunk := self._source.read(min(_READ_CHUNK_BYTES, room)):
            encrypted = encryptor.update(chunk)
            pieces.append(encrypted)
            room -= len(encrypted)
            if room == 0:
                room = self._volume_bytes
                yield _joined(pieces)
        # Тег может не поместиться в последний том и уйти в следующий
        pieces.append(encryptor.finalize() + encryptor.tag)
        tail = _joined(pieces)
        if len(tail) > self._volume_bytes:
            yield tail[:self._volume_bytes]
            tail = tail[self._volume_bytes:]
        yield tail


def _joined(pieces: list[bytes]) -> bytes:
    """Склеить куски тома и отпустить их: пока том заливается, второй его копии в памяти нет."""
    volume = b"".join(pieces)
    pieces.clear()
    return volume


def encrypt_file(source: BinaryIO, password: str, target: BinaryIO) -> None:
    """Зашифровать файл паролем в один файл — тот же архив, что набор томов, не разрезанный на части.

    Поэтому его читают и окно восстановления панели, и tg-restore.py.
    """
    for piece in EncryptedVolumes(source, password, _READ_CHUNK_BYTES):
        target.write(piece)


def decrypt_volumes(volumes: Sequence[BinaryIO], password: str, target: BinaryIO) -> None:
    """Собрать тома по порядку и расшифровать паролем в target.

    Открытый текст пишется раньше, чем проверена подлинность набора: после
    InvalidTag (неверный пароль, перепутанные или неполные тома) записанному в
    target верить нельзя.
    """
    header = volumes[0].read(_HEADER_LEN)
    if len(header) < _HEADER_LEN or not header.startswith(_MAGIC):
        raise ValueError("Не набор томов бэкапа (нет сигнатуры) или тома перепутаны")
    salt = header[len(_MAGIC):-_NONCE_LEN]
    nonce = header[-_NONCE_LEN:]
    decryptor = Cipher(algorithms.AES(_derive_key(password, salt)), modes.GCM(nonce)).decryptor()
    # Последние байты набора — тег, а где кончается шифртекст, видно только в конце
    held = b""
    for volume in volumes:
        while chunk := volume.read(_READ_CHUNK_BYTES):
            data = held + chunk
            target.write(decryptor.update(data[:-_TAG_LEN]))
            held = data[-_TAG_LEN:]
    target.write(decryptor.finalize_with_tag(held))


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
    token: str, chat_id: str, volumes: EncryptedVolumes, filename_base: str, caption: str
) -> None:
    """Залить тома в канал по порядку с ретраями. Кидает RuntimeError при провале."""
    total = len(volumes)
    pending = iter(volumes)
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    async with httpx.AsyncClient(timeout=600) as client:
        for idx in range(1, total + 1):
            data = {"chat_id": chat_id}
            if idx == 1 and caption:
                data["caption"] = caption
            if not await _send_next_volume(client, url, data, f"{filename_base}.{idx:03d}", pending):
                raise RuntimeError(f"Telegram: том {idx}/{total} не отправлен после {SEND_RETRIES} попыток")


async def _send_next_volume(
    client: httpx.AsyncClient, url: str, data: dict[str, str], filename: str, pending: Iterator[bytes]
) -> bool:
    """Взять следующий том и залить его с ретраями; False — все попытки провалились.

    Том живёт только внутри этого вызова: когда собирается следующий,
    предыдущего в памяти уже нет.
    """
    # Чтение с диска и шифрование десятков мегабайт — в потоке, не в цикле событий.
    # Значение по умолчанию — потому что StopIteration через Future не проходит.
    volume = await asyncio.to_thread(next, pending, b"")
    for attempt in range(1, SEND_RETRIES + 1):
        # Файлом, а не bytes: запрос httpx остаётся в памяти до сборщика мусора
        # (у ответа циклическая ссылка) и держал бы том; закрытый файл — нет
        with io.BytesIO(volume) as document:
            try:
                resp = await client.post(url, data=data, files={"document": (filename, document)})
                if resp.status_code == 200 and resp.json().get("ok"):
                    return True
            except httpx.HTTPError as e:
                logger.warning(f"Telegram sendDocument {filename} попытка {attempt}: {e}")
        if attempt < SEND_RETRIES:
            await asyncio.sleep(SEND_RETRY_DELAY)
    return False
