#!/usr/bin/env python3
"""Собрать тома бэкапа панели из Telegram и расшифровать в .dump.

Автобэкап панели уходит в Telegram-канал набором зашифрованных томов
(`panel-backup_<дата>.enc.001`, `.002`, …). Этот скрипт собирает их по порядку,
расшифровывает паролем архива и пишет обычный `.dump`, который загружается для
восстановления в панели: Настройки → Бэкап → Восстановить.

Использование:
  python3 tg-restore.py -o restored.dump part.enc.001 part.enc.002 ...
  python3 tg-restore.py -o restored.dump ./volumes-dir/

Нужен пакет `cryptography` (`pip install cryptography`).
"""
import argparse
import sys
from getpass import getpass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

MAGIC = b"MTGB1\n"
SALT_LEN = 16
NONCE_LEN = 12
ITERS = 200_000


def collect_parts(args: list[str]) -> list[Path]:
    files: list[Path] = []
    for arg in args:
        path = Path(arg)
        if path.is_dir():
            found = sorted(path.glob("*.enc.*")) or sorted(p for p in path.iterdir() if p.is_file())
            files.extend(found)
        else:
            files.append(path)
    # По имени: .001 < .002 < … — порядок томов
    return sorted(files, key=lambda p: p.name)


def main() -> None:
    ap = argparse.ArgumentParser(description="Расшифровать тома бэкапа панели из Telegram в .dump")
    ap.add_argument("-o", "--output", required=True, help="Куда записать .dump")
    ap.add_argument("parts", nargs="+", help="Файлы томов или каталог с ними")
    args = ap.parse_args()

    files = collect_parts(args.parts)
    if not files:
        sys.exit("Не найдено ни одного тома")
    print("Тома по порядку:")
    for f in files:
        print(f"  {f.name}")

    blob = b"".join(f.read_bytes() for f in files)
    if not blob.startswith(MAGIC):
        sys.exit("Это не набор томов бэкапа (нет сигнатуры) или тома перепутаны")

    body = blob[len(MAGIC):]
    salt = body[:SALT_LEN]
    nonce = body[SALT_LEN:SALT_LEN + NONCE_LEN]
    ciphertext = body[SALT_LEN + NONCE_LEN:]

    password = getpass("Пароль архива: ")
    key = PBKDF2HMAC(algorithm=SHA256(), length=32, salt=salt, iterations=ITERS).derive(password.encode())
    try:
        data = AESGCM(key).decrypt(nonce, ciphertext, None)
    except Exception:
        sys.exit("Не удалось расшифровать — неверный пароль или повреждённые/неполные тома")

    Path(args.output).write_bytes(data)
    print(f"\nГотово: {args.output} ({len(data)} байт).")
    print("Загрузите его в панели: Настройки → Бэкап → Восстановить.")


if __name__ == "__main__":
    main()
