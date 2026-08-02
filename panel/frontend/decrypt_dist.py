#!/usr/bin/env python3

import sys
import hashlib
import base64
import tarfile
import io

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def process_and_extract(enc_path: str, output_dir: str, key: str):
    key_bytes = hashlib.sha256(key.encode()).digest()
    aesgcm = AESGCM(key_bytes)

    with open(enc_path, 'rb') as f:
        raw = f.read()

    encrypted = base64.b64decode(raw)
    nonce = encrypted[:12]
    ciphertext = encrypted[12:]
    decrypted = aesgcm.decrypt(nonce, ciphertext, None)

    tar = tarfile.open(fileobj=io.BytesIO(decrypted), mode='r:gz')
    tar.extractall(output_dir)
    tar.close()


if __name__ == '__main__':
    if len(sys.argv) != 4:
        sys.exit(1)

    try:
        process_and_extract(sys.argv[1], sys.argv[2], sys.argv[3])
    except Exception:
        sys.exit(1)
