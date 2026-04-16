"""HTTP-клиенты к нодам: mTLS для новых, verify=False для legacy."""
from __future__ import annotations

import logging
import shutil
import ssl
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from app.models import Server
    from app.services.pki import PKIKeygenData

logger = logging.getLogger(__name__)

_NODE_LIMITS = httpx.Limits(
    max_connections=200,
    max_keepalive_connections=50,
    keepalive_expiry=120,
)

_EXTERNAL_LIMITS = httpx.Limits(
    max_connections=20,
    max_keepalive_connections=10,
    keepalive_expiry=60,
)

_NODE_TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=2.0, pool=2.0)
_EXTERNAL_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)

_node_client_mtls: httpx.AsyncClient | None = None
_node_client_legacy: httpx.AsyncClient | None = None
_external_client: httpx.AsyncClient | None = None
_cert_tmpdir: Path | None = None


def _build_mtls_context(keygen: "PKIKeygenData") -> tuple[ssl.SSLContext, Path]:
    tmpdir = Path(tempfile.mkdtemp(prefix="monitoring-mtls-"))
    ca_path = tmpdir / "ca.pem"
    crt_path = tmpdir / "client.crt"
    key_path = tmpdir / "client.key"
    ca_path.write_text(keygen.ca_cert)
    crt_path.write_text(keygen.client_cert)
    key_path.write_text(keygen.client_key)
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_path))
    ctx.load_cert_chain(certfile=str(crt_path), keyfile=str(key_path))
    # Ноды идентифицируются по CA-подписи, а не по hostname/IP совпадению.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx, tmpdir


async def init_http_clients(keygen: "PKIKeygenData | None" = None) -> None:
    global _node_client_mtls, _node_client_legacy, _external_client, _cert_tmpdir

    _node_client_legacy = httpx.AsyncClient(
        verify=False,
        timeout=_NODE_TIMEOUT,
        limits=_NODE_LIMITS,
        follow_redirects=False,
        http2=True,
        trust_env=False,
    )

    if keygen is not None:
        ctx, tmpdir = _build_mtls_context(keygen)
        _cert_tmpdir = tmpdir
        _node_client_mtls = httpx.AsyncClient(
            verify=ctx,
            timeout=_NODE_TIMEOUT,
            limits=_NODE_LIMITS,
            follow_redirects=False,
            http2=True,
            trust_env=False,
        )
        logger.info("mTLS http client initialized (http2=True)")
    else:
        logger.warning("mTLS http client not initialized: keygen missing")

    _external_client = httpx.AsyncClient(
        timeout=_EXTERNAL_TIMEOUT,
        limits=_EXTERNAL_LIMITS,
        follow_redirects=True,
        http2=True,
    )


async def close_http_clients() -> None:
    global _node_client_mtls, _node_client_legacy, _external_client, _cert_tmpdir
    if _node_client_mtls:
        await _node_client_mtls.aclose()
        _node_client_mtls = None
    if _node_client_legacy:
        await _node_client_legacy.aclose()
        _node_client_legacy = None
    if _external_client:
        await _external_client.aclose()
        _external_client = None
    if _cert_tmpdir and _cert_tmpdir.exists():
        shutil.rmtree(_cert_tmpdir, ignore_errors=True)
        _cert_tmpdir = None


def get_node_client(server: "Server | None" = None) -> httpx.AsyncClient:
    """Вернуть клиент в зависимости от режима сервера.

    server=None → legacy (обратная совместимость со старым кодом,
    который вызывает get_node_client() без параметров).
    """
    if server is not None and getattr(server, "pki_enabled", False):
        if _node_client_mtls is None:
            raise RuntimeError(
                "mTLS http client is not initialized — PKI keygen missing at startup"
            )
        return _node_client_mtls
    if _node_client_legacy is None:
        raise RuntimeError("Legacy http client not initialized — call init_http_clients() first")
    return _node_client_legacy


def node_auth_headers(server: "Server") -> dict[str, str]:
    """Заголовок X-API-Key только для legacy-нод; mTLS уже аутентифицирован TLS-слоем."""
    if getattr(server, "pki_enabled", False):
        return {}
    return {"X-API-Key": server.api_key or ""}


def get_external_client() -> httpx.AsyncClient:
    if _external_client is None:
        raise RuntimeError("HTTP clients not initialized — call init_http_clients() first")
    return _external_client
