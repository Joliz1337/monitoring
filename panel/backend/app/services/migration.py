"""Миграция нод на общий shared TLS-сертификат.

Per-server mTLS ноды переводим автоматически через `/api/system/replace-node-cert`
на ноде (zero-downtime). Legacy api_key ноды нельзя переключить программно — у них
nginx работает без mTLS и требует переустановки с новым NODE_SECRET.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from app.models import Server
from app.services.http_client import get_node_client, node_auth_headers
from app.services.pki import PKIKeygenData

logger = logging.getLogger(__name__)


class LegacyMigrationRequired(Exception):
    """Поднимается для legacy-нод — миграция возможна только через переустановку."""


async def push_shared_cert_to_node(server: Server, keygen: PKIKeygenData) -> None:
    """Установить shared cert на ноду через её API. Только для pki_enabled нод."""
    if not server.pki_enabled:
        raise LegacyMigrationRequired(server.name)

    payload = {
        "cert_pem": keygen.shared_node_cert,
        "key_pem": keygen.shared_node_key,
    }
    client = get_node_client(server)
    base_url = server.url.rstrip("/")

    response = await client.post(
        f"{base_url}/api/system/replace-node-cert",
        json=payload,
        headers=node_auth_headers(server),
        timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0),
    )
    response.raise_for_status()

    # Дать nginx время на reload и убедиться что нода доступна с новым cert.
    await asyncio.sleep(1.5)
    health = await client.get(
        f"{base_url}/api/version",
        headers=node_auth_headers(server),
        timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
    )
    health.raise_for_status()


def classify_server(server: Server) -> str:
    """shared / per_server / legacy — для отображения в баннере миграции."""
    if server.uses_shared_cert:
        return "shared"
    if server.pki_enabled:
        return "per_server"
    return "legacy"
