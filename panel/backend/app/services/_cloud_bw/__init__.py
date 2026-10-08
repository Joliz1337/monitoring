import os

_key = os.getenv("EXT_KEY", "")
_mod = None

if _key:
    try:
        from ._loader import load_module
        _mod = load_module("mod.enc", _key)
    except Exception:
        pass


def get_cloud_bw_service():
    return _mod
