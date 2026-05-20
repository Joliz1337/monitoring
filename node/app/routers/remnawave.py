"""Remnawave node status API endpoint."""

import asyncio
import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/remnawave", tags=["remnawave"])

CONTAINER_NAME = "remnanode"


async def _check_container_available() -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f", "{{.State.Running}}", CONTAINER_NAME,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        return stdout.decode().strip().lower() == "true"
    except Exception:
        return False


@router.get("/status")
async def get_status():
    """Check if remnanode container is running."""
    available = await _check_container_available()
    return {"available": available}
