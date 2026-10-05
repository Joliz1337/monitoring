import os
from pathlib import Path

_key = os.getenv("EXT_KEY", "")
_mod = None


def _init_worker():
    if not _key:
        return

    worker_enc = Path("/app/ext/cloud_adv_worker.enc")
    worker_py = Path("/app/ext/cloud_adv_worker.py")

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


def get_cloud_adv_service():
    return _mod
