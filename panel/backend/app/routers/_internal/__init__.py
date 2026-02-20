import os
import logging
from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter()

_key = os.getenv("EXT_KEY", "")

if _key:
    try:
        from ._loader import load_module
        _mod = load_module("mod.enc", _key)
        if _mod and hasattr(_mod, 'register_routes'):
            _mod.register_routes(router)
    except Exception:
        pass
