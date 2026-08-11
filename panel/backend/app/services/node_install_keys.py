"""Ключи установки под один сервер: выпуск, хранение, сборка команды установки."""
from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NodeInstallKey
from app.services.pki import (
    PKIKeygenData,
    cert_expires_at,
    fingerprint_sha256,
    generate_dedicated_node_cert,
    pack_node_secret,
)
from app.services.update_channel import installer_url

logger = logging.getLogger(__name__)

MIN_VALIDITY_DAYS = 1
MAX_VALIDITY_DAYS = 1095
DEFAULT_VALIDITY_DAYS = 90


async def create_install_key(
    db: AsyncSession,
    keygen: PKIKeygenData,
    name: str,
    validity_days: int,
) -> NodeInstallKey:
    """Выпустить сертификат под одну выдачу и сохранить его в БД."""
    common_name, cert_pem, key_pem = generate_dedicated_node_cert(
        keygen.ca_cert,
        keygen.ca_key,
        validity_days,
    )
    key = NodeInstallKey(
        name=name,
        common_name=common_name,
        cert_pem=cert_pem,
        key_pem=key_pem,
        fingerprint=fingerprint_sha256(cert_pem),
        expires_at=cert_expires_at(cert_pem),
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)

    logger.info(
        f"Install key issued: cn={common_name}, name={name!r}, expires={key.expires_at.isoformat()}"
    )
    return key


async def list_install_keys(db: AsyncSession) -> list[NodeInstallKey]:
    result = await db.execute(
        select(NodeInstallKey).order_by(NodeInstallKey.created_at.desc())
    )
    return list(result.scalars().all())


async def delete_install_key(db: AsyncSession, key_id: int) -> bool:
    """Удалить ключ. Уже установленную ноду это не отключает — она держит свою копию."""
    result = await db.execute(
        delete(NodeInstallKey).where(NodeInstallKey.id == key_id)
    )
    await db.commit()
    if result.rowcount:
        logger.info(f"Install key deleted: id={key_id}")
    return bool(result.rowcount)


def build_key_token(
    keygen: PKIKeygenData,
    key: NodeInstallKey,
    panel_ip: str | None = None,
) -> str:
    """Собрать NODE_SECRET того же формата, что и общий, но с сертификатом этой выдачи."""
    return pack_node_secret(
        keygen.ca_cert,
        key.cert_pem,
        key.key_pem,
        panel_ip=panel_ip,
    )


def build_install_command(token: str) -> str:
    return f"bash <(curl -fsSL {installer_url()}) {token}"
