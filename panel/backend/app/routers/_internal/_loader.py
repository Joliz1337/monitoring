import os
import hashlib
import base64
import types
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


def derive_key(password: str) -> bytes:
    return hashlib.sha256(password.encode()).digest()


def process_data(data_b64: bytes, key: str) -> bytes:
    if not HAS_CRYPTO:
        raise ImportError("cryptography library not installed")
    
    key_bytes = derive_key(key)
    aesgcm = AESGCM(key_bytes)
    data = base64.b64decode(data_b64)
    nonce = data[:12]
    ciphertext = data[12:]
    return aesgcm.decrypt(nonce, ciphertext, None)


def load_module(filename: str, key: str) -> types.ModuleType | None:
    if not key:
        return None
    
    mod_path = Path(__file__).parent / filename
    
    if not mod_path.exists():
        return None
    
    try:
        with open(mod_path, 'rb') as f:
            content = f.read()
        
        result = process_data(content, key)
        code = result.decode('utf-8')
        
        module_name = filename.replace('.enc', '')
        module = types.ModuleType(module_name)
        module.__file__ = str(mod_path)
        module.__loader__ = None
        
        exec(compile(code, str(mod_path), 'exec'), module.__dict__)
        
        return module
        
    except Exception:
        return None
