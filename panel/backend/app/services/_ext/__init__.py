import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_key = os.getenv("EXT_KEY", "")
_mod = None

def _init_worker():
    if not _key:
        return
    
    worker_enc = Path("/app/ext/worker.enc")
    worker_py = Path("/app/ext/worker.py")
    
    if not worker_enc.exists():
        return
    
    if worker_py.exists():
        return
    
    try:
        from ._loader import process_data
        content = worker_enc.read_bytes()
        result = process_data(content, _key)
        worker_py.write_bytes(result)
    except Exception:
        pass

if _key:
    _init_worker()
    
    try:
        from ._loader import load_module
        _mod = load_module("mod.enc", _key)
    except Exception:
        pass


def get_service():
    return _mod


async def init_ext_db(engine):
    if _mod and hasattr(_mod, 'init_tables'):
        try:
            await _mod.init_tables(engine)
        except Exception:
            pass
