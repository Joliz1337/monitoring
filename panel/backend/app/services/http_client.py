"""HTTP-клиенты к нодам: mTLS для новых, verify=False для legacy."""
from __future__ import annotations

import logging
import re
import shutil
import ssl
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from app.models import Server
    from app.services.pki import PKIKeygenData

logger = logging.getLogger(__name__)

_NODE_LIMITS = httpx.Limits(
    max_connections=500,
    max_keepalive_connections=100,
    keepalive_expiry=30,
)

_EXTERNAL_LIMITS = httpx.Limits(
    max_connections=20,
    max_keepalive_connections=10,
    keepalive_expiry=60,
)

_NODE_TIMEOUT = httpx.Timeout(connect=2.0, read=10.0, write=2.0, pool=2.0)
_EXTERNAL_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)

_APPLY_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)
_APPLY_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30)
# Через один SOCKS-прокси обычно ходит 1-2 ноды — пул скромнее общего (500)
_PROXY_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30)

_node_client_mtls: httpx.AsyncClient | None = None
_node_client_legacy: httpx.AsyncClient | None = None
# Отдельные клиенты для долгих apply-запросов: read=300s и свой пул соединений,
# чтобы тяжёлые операции не конкурировали с потоком метрик за keepalive-слоты.
_node_apply_client_mtls: httpx.AsyncClient | None = None
_node_apply_client_legacy: httpx.AsyncClient | None = None
_external_client: httpx.AsyncClient | None = None
_cert_tmpdir: Path | None = None
_mtls_ctx: ssl.SSLContext | None = None
# Клиенты через SOCKS5-прокси создаются лениво по мере обращения к нодам с proxy_url.
# Ключ: (raw-строка прокси, mtls, apply). Старые записи живут до shutdown — рост ограничен
# числом уникальных прокси, keepalive-соединения гаснут через 30с.
_proxy_clients: dict[tuple[str, bool, bool], httpx.AsyncClient] = {}

# Формат ввода прокси: "ip:port" или "ip:port@login:pass".
# Логин без ':' и '@'; пароль — любые непробельные символы (включая ':' и '@').
_PROXY_INPUT_RE = re.compile(
    r'^(?P<host>[^\s:@/]+):(?P<port>\d{1,5})'
    r'(?:@(?P<login>[^\s:@/]+):(?P<password>\S+))?$'
)


def validate_proxy_input(v: str | None) -> str | None:
    """''/None → None (прокси выключен); невалидный формат → ValueError."""
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    m = _PROXY_INPUT_RE.match(v)
    if not m or not (1 <= int(m.group("port")) <= 65535):
        raise ValueError('Proxy must be "ip:port" or "ip:port@login:pass"')
    return v


def sanitize_proxy(raw: str) -> str:
    """Для логов: host:port без креденшалов."""
    return raw.partition("@")[0]


def parse_proxy_input(raw: str) -> tuple[str, int, str | None, str | None]:
    """'ip:port[@login:pass]' → (host, port, login, password).

    Первый '@' отделяет host:port от креденшалов, первый ':' в креденшалах —
    логин от пароля: пароль может содержать '@' и ':', логин — нет.
    """
    hostport, _, creds = raw.partition("@")
    host, _, port = hostport.partition(":")
    if not creds:
        return host, int(port), None, None
    login, _, password = creds.partition(":")
    return host, int(port), login, password


def _proxy_raw_to_url(raw: str) -> str:
    """'ip:port@login:pass' → 'socks5://login:pass@ip:port'.

    Логин/пароль квотируются: httpx делает unquote userinfo при разборе URL,
    поэтому спецсимволы (@ : / %) в пароле проходят без потерь.
    """
    host, port, login, password = parse_proxy_input(raw)
    if login is None:
        return f"socks5://{host}:{port}"
    return f"socks5://{quote(login, safe='')}:{quote(password or '', safe='')}@{host}:{port}"


def _get_proxy_client(raw: str, *, mtls: bool, apply: bool) -> httpx.AsyncClient:
    """Клиент через SOCKS5-прокси. Синхронная функция без await между get/set —
    в одном event loop гонки на кэше невозможны."""
    key = (raw, mtls, apply)
    client = _proxy_clients.get(key)
    if client is not None:
        return client

    if mtls and _mtls_ctx is None:
        raise RuntimeError("mTLS context is not initialized — PKI keygen missing at startup")

    client = httpx.AsyncClient(
        proxy=_proxy_raw_to_url(raw),
        verify=_mtls_ctx if mtls else False,
        timeout=_APPLY_TIMEOUT if apply else _NODE_TIMEOUT,
        limits=_APPLY_LIMITS if apply else _PROXY_LIMITS,
        follow_redirects=False,
        http2=False,
        trust_env=False,
    )
    _proxy_clients[key] = client
    logger.info(
        "SOCKS5 client created via %s (mtls=%s, apply=%s)", sanitize_proxy(raw), mtls, apply
    )
    return client


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
    global _node_apply_client_mtls, _node_apply_client_legacy, _mtls_ctx

    _node_client_legacy = httpx.AsyncClient(
        verify=False,
        timeout=_NODE_TIMEOUT,
        limits=_NODE_LIMITS,
        follow_redirects=False,
        http2=False,
        trust_env=False,
    )

    # HTTP/1.1 клиент для длительных apply-запросов (firewall/haproxy profile apply).
    # Свой пул limits=20 чтобы не блокировать основной канал коротких запросов.
    _node_apply_client_legacy = httpx.AsyncClient(
        verify=False,
        timeout=_APPLY_TIMEOUT,
        limits=_APPLY_LIMITS,
        follow_redirects=False,
        http2=False,
        trust_env=False,
    )

    if keygen is not None:
        ctx, tmpdir = _build_mtls_context(keygen)
        _cert_tmpdir = tmpdir
        _mtls_ctx = ctx
        _node_client_mtls = httpx.AsyncClient(
            verify=ctx,
            timeout=_NODE_TIMEOUT,
            limits=_NODE_LIMITS,
            follow_redirects=False,
            http2=False,
            trust_env=False,
        )
        _node_apply_client_mtls = httpx.AsyncClient(
            verify=ctx,
            timeout=_APPLY_TIMEOUT,
            limits=_APPLY_LIMITS,
            follow_redirects=False,
            http2=False,
            trust_env=False,
        )
        logger.info("mTLS http clients initialized (http2=False on all node clients)")
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
    global _node_apply_client_mtls, _node_apply_client_legacy, _mtls_ctx
    # Прокси-клиенты держат ссылку на mTLS-контекст — закрываем до его сброса
    # (restore бэкапа делает close+init, старый контекст не должен пережить рестарт клиентов)
    for client in _proxy_clients.values():
        await client.aclose()
    _proxy_clients.clear()
    _mtls_ctx = None
    if _node_client_mtls:
        await _node_client_mtls.aclose()
        _node_client_mtls = None
    if _node_client_legacy:
        await _node_client_legacy.aclose()
        _node_client_legacy = None
    if _node_apply_client_mtls:
        await _node_apply_client_mtls.aclose()
        _node_apply_client_mtls = None
    if _node_apply_client_legacy:
        await _node_apply_client_legacy.aclose()
        _node_apply_client_legacy = None
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
    proxy_raw = getattr(server, "proxy_url", None) if server is not None else None
    if proxy_raw:
        return _get_proxy_client(
            proxy_raw, mtls=bool(getattr(server, "pki_enabled", False)), apply=False
        )
    if server is not None and getattr(server, "pki_enabled", False):
        if _node_client_mtls is None:
            raise RuntimeError(
                "mTLS http client is not initialized — PKI keygen missing at startup"
            )
        return _node_client_mtls
    if _node_client_legacy is None:
        raise RuntimeError("Legacy http client not initialized — call init_http_clients() first")
    return _node_client_legacy


def get_node_apply_client(server: "Server | None" = None) -> httpx.AsyncClient:
    """Клиент для долгих apply-запросов (firewall/haproxy profile apply) с read=300s
    и отдельным пулом, чтобы не конкурировать с потоком коротких запросов метрик."""
    proxy_raw = getattr(server, "proxy_url", None) if server is not None else None
    if proxy_raw:
        return _get_proxy_client(
            proxy_raw, mtls=bool(getattr(server, "pki_enabled", False)), apply=True
        )
    if server is not None and getattr(server, "pki_enabled", False):
        if _node_apply_client_mtls is None:
            raise RuntimeError(
                "mTLS apply client is not initialized — PKI keygen missing at startup"
            )
        return _node_apply_client_mtls
    if _node_apply_client_legacy is None:
        raise RuntimeError("Legacy apply client not initialized — call init_http_clients() first")
    return _node_apply_client_legacy


def node_auth_headers(server: "Server") -> dict[str, str]:
    """Заголовок X-API-Key только для legacy-нод; mTLS уже аутентифицирован TLS-слоем."""
    if getattr(server, "pki_enabled", False):
        return {}
    return {"X-API-Key": server.api_key or ""}


def get_external_client() -> httpx.AsyncClient:
    if _external_client is None:
        raise RuntimeError("HTTP clients not initialized — call init_http_clients() first")
    return _external_client
