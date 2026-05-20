"""PKI для канала panel ↔ node: CA + клиентский/серверный сертификаты + NODE_SECRET."""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from sqlalchemy import select

logger = logging.getLogger(__name__)

NODE_SECRET_VERSION = 1
_CURVE = ec.SECP256R1()
_SIGN_HASH = hashes.SHA256()


@dataclass(frozen=True)
class PKIKeygenData:
    """In-memory снапшот CA, клиентского сертификата панели и shared cert ноды."""
    ca_cert: str
    ca_key: str
    client_cert: str
    client_key: str
    shared_node_cert: str
    shared_node_key: str


def _load_cert(pem: str) -> x509.Certificate:
    return x509.load_pem_x509_certificate(pem.encode())


def _load_private_key(pem: str) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("Expected EC private key")
    return key


def _serialize_cert(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _serialize_key(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def generate_ca(
    common_name: str = "Monitoring Panel CA",
    validity_days: int = 3650,
) -> tuple[str, str]:
    """Создать самоподписанный корневой CA (ECDSA P-256)."""
    key = ec.generate_private_key(_CURVE)
    now = datetime.now(timezone.utc)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Monitoring"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=validity_days))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .sign(key, _SIGN_HASH)
    )
    return _serialize_cert(cert), _serialize_key(key)


def _sign_leaf(
    ca_cert_pem: str,
    ca_key_pem: str,
    common_name: str,
    validity_days: int,
    extended_key_usage: list[x509.ObjectIdentifier],
    san: x509.SubjectAlternativeName | None = None,
) -> tuple[str, str]:
    ca_cert = _load_cert(ca_cert_pem)
    ca_key = _load_private_key(ca_key_pem)
    leaf_key = ec.generate_private_key(_CURVE)
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=validity_days))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage(extended_key_usage),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()),
            critical=False,
        )
    )
    if san is not None:
        builder = builder.add_extension(san, critical=False)
    cert = builder.sign(ca_key, _SIGN_HASH)
    return _serialize_cert(cert), _serialize_key(leaf_key)


def generate_client_cert(
    ca_cert_pem: str,
    ca_key_pem: str,
    common_name: str = "panel-client",
    validity_days: int = 3650,
) -> tuple[str, str]:
    """Клиентский сертификат панели для mTLS (ExtendedKeyUsage=clientAuth)."""
    return _sign_leaf(
        ca_cert_pem,
        ca_key_pem,
        common_name,
        validity_days,
        [ExtendedKeyUsageOID.CLIENT_AUTH],
    )


_DNS_LABEL_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


def _is_valid_dns_name(name: str) -> bool:
    """DNSName для x509 требует A-label (ASCII). Отсекаем кириллицу, пробелы, эмодзи."""
    if not name or len(name) > 253:
        return False
    try:
        name.encode("ascii")
    except UnicodeEncodeError:
        return False
    labels = name.rstrip(".").split(".")
    if not labels:
        return False
    return all(_DNS_LABEL_RE.match(label) for label in labels)


def _build_san(node_name: str, san_hosts: list[str] | None) -> x509.SubjectAlternativeName:
    entries: list[x509.GeneralName] = []
    seen_dns: set[str] = set()
    seen_ip: set[str] = set()

    def add_dns(name: str) -> None:
        if not _is_valid_dns_name(name):
            return
        key = name.lower()
        if key not in seen_dns:
            entries.append(x509.DNSName(name))
            seen_dns.add(key)

    def add_ip(raw: str) -> None:
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return
        key = addr.compressed
        if key not in seen_ip:
            entries.append(x509.IPAddress(addr))
            seen_ip.add(key)

    add_dns(node_name)
    for host in san_hosts or []:
        if not host:
            continue
        try:
            ipaddress.ip_address(host)
        except ValueError:
            add_dns(host)
        else:
            add_ip(host)
    add_dns("localhost")
    add_ip("127.0.0.1")
    return x509.SubjectAlternativeName(entries)


def generate_node_cert(
    ca_cert_pem: str,
    ca_key_pem: str,
    node_name: str,
    san_hosts: list[str] | None = None,
    validity_days: int = 1095,
) -> tuple[str, str]:
    """Серверный сертификат ноды (serverAuth + SAN с IP/DNS)."""
    san = _build_san(node_name, san_hosts)
    return _sign_leaf(
        ca_cert_pem,
        ca_key_pem,
        node_name,
        validity_days,
        [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH],
        san=san,
    )


def pack_node_secret(
    ca_cert_pem: str,
    node_cert_pem: str,
    node_key_pem: str,
    panel_ip: str | None = None,
) -> str:
    """Упаковать shared-payload для установки на любой ноде."""
    payload: dict = {
        "v": NODE_SECRET_VERSION,
        "ca": ca_cert_pem,
        "crt": node_cert_pem,
        "key": node_key_pem,
    }
    if panel_ip:
        payload["panel_ip"] = panel_ip
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def build_installer_token(keygen: PKIKeygenData, panel_ip: str | None = None) -> str:
    """Собрать NODE_SECRET, который оператор копирует на ноду."""
    return pack_node_secret(
        keygen.ca_cert,
        keygen.shared_node_cert,
        keygen.shared_node_key,
        panel_ip=panel_ip,
    )


def unpack_node_secret(secret: str) -> dict:
    """Распаковать и провалидировать NODE_SECRET."""
    padding = "=" * (-len(secret) % 4)
    try:
        raw = base64.urlsafe_b64decode(secret + padding)
    except Exception as exc:
        raise ValueError(f"NODE_SECRET is not valid base64: {exc}") from exc
    try:
        data = json.loads(raw.decode())
    except Exception as exc:
        raise ValueError(f"NODE_SECRET is not valid JSON: {exc}") from exc
    if data.get("v") != NODE_SECRET_VERSION:
        raise ValueError(f"Unsupported NODE_SECRET version: {data.get('v')}")
    for field in ("ca", "crt", "key"):
        if not data.get(field):
            raise ValueError(f"NODE_SECRET missing field: {field}")
    return data


def fingerprint_sha256(cert_pem: str) -> str:
    """SHA-256 fingerprint в формате AA:BB:CC:..."""
    cert = _load_cert(cert_pem)
    digest = cert.fingerprint(hashes.SHA256())
    return ":".join(f"{b:02X}" for b in digest)


SHARED_NODE_CN = "shared-node"


async def load_or_create_keygen(session_factory) -> PKIKeygenData:
    """Загрузить singleton-запись PKI из БД или создать её при первом запуске."""
    from app.models import PKIKeygen

    async with session_factory() as db:
        result = await db.execute(select(PKIKeygen).where(PKIKeygen.id == 1))
        row = result.scalar_one_or_none()
        if row is None:
            logger.info("Generating new PKI keygen (CA + panel client cert + shared node cert)")
            ca_cert_pem, ca_key_pem = generate_ca()
            client_cert_pem, client_key_pem = generate_client_cert(ca_cert_pem, ca_key_pem)
            shared_cert_pem, shared_key_pem = generate_node_cert(
                ca_cert_pem, ca_key_pem, SHARED_NODE_CN, san_hosts=None
            )

            row = PKIKeygen(
                id=1,
                ca_cert_pem=ca_cert_pem,
                ca_key_pem=ca_key_pem,
                client_cert_pem=client_cert_pem,
                client_key_pem=client_key_pem,
                shared_node_cert_pem=shared_cert_pem,
                shared_node_key_pem=shared_key_pem,
            )
            db.add(row)
            await db.commit()
            logger.info(
                "PKI keygen created: CA fingerprint %s",
                fingerprint_sha256(ca_cert_pem),
            )
            return PKIKeygenData(
                ca_cert=ca_cert_pem,
                ca_key=ca_key_pem,
                client_cert=client_cert_pem,
                client_key=client_key_pem,
                shared_node_cert=shared_cert_pem,
                shared_node_key=shared_key_pem,
            )

        if not row.shared_node_cert_pem or not row.shared_node_key_pem:
            logger.info("Backfilling shared node cert in existing keygen")
            shared_cert_pem, shared_key_pem = generate_node_cert(
                row.ca_cert_pem, row.ca_key_pem, SHARED_NODE_CN, san_hosts=None
            )
            row.shared_node_cert_pem = shared_cert_pem
            row.shared_node_key_pem = shared_key_pem
            await db.commit()
            logger.info(
                "Shared node cert created: fingerprint %s",
                fingerprint_sha256(shared_cert_pem),
            )

        logger.info("PKI keygen loaded from DB")
        return PKIKeygenData(
            ca_cert=row.ca_cert_pem,
            ca_key=row.ca_key_pem,
            client_cert=row.client_cert_pem,
            client_key=row.client_key_pem,
            shared_node_cert=row.shared_node_cert_pem,
            shared_node_key=row.shared_node_key_pem,
        )
