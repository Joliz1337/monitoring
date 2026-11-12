"""HTTP-клиенты к нодам: mTLS для новых, verify=False для legacy."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import ssl
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

import httpx

if TYPE_CHECKING:
    from app.models import Server
    from app.services.pki import PKIKeygenData

logger = logging.getLogger(__name__)

# У каждой ноды свой клиент, и лимиты ниже — на одну ноду. Общий на весь парк пул
# httpx закрывает простаивающие соединения, как только всех соединений больше
# max_keepalive_connections, и на каждый запрос дважды перебирает весь пул: дальше
# сотни нод каждый опрос шёл через новое mTLS-рукопожатие, а сам перебор стоил
# миллисекунд процессора на запрос. keepalive_expiry короче keepalive_timeout nginx
# ноды (65 с) — иначе запрос уходит в соединение, которое нода уже закрыла.
_NODE_LIMITS = httpx.Limits(max_connections=16, max_keepalive_connections=2, keepalive_expiry=45)
_NODE_APPLY_LIMITS = httpx.Limits(max_connections=4, max_keepalive_connections=1, keepalive_expiry=30)

_EXTERNAL_LIMITS = httpx.Limits(
    max_connections=20,
    max_keepalive_connections=10,
    keepalive_expiry=60,
)

_NODE_TIMEOUT = httpx.Timeout(connect=2.0, read=10.0, write=2.0, pool=2.0)
_EXTERNAL_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)

# Долгие apply-запросы (firewall/haproxy profile apply) идут своим клиентом с read=300s,
# чтобы не занимать соединения, по которым опрашиваются метрики.
_APPLY_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)
# Через один SOCKS-прокси обычно ходит 1-2 ноды, пул у них общий на прокси
_PROXY_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30)
_PROXY_APPLY_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30)

# Клиент ноды, к которой давно не ходили (удалена, сменила адрес), закрывается
NODE_CLIENT_IDLE_TTL = 900.0
PRUNE_INTERVAL = 60.0

# Keep-alive соединение на каждую ноду — это дескриптор на ноду, а мягкий лимит
# в контейнере бывает 1024
OPEN_FILES_TARGET = 65536
OPEN_FILES_COMFORTABLE = 8192

_monotonic = time.monotonic


@dataclass
class _NodeClient:
    client: httpx.AsyncClient
    last_used: float


# Ключ: (адрес ноды, mTLS, apply)
_node_clients: dict[tuple[str, bool, bool], _NodeClient] = {}
_last_prune_at: float = 0.0
_closing_tasks: set[asyncio.Task] = set()
_external_client: httpx.AsyncClient | None = None
_cert_tmpdir: Path | None = None
_mtls_ctx: ssl.SSLContext | None = None
_legacy_ctx: ssl.SSLContext | None = None
# Клиенты через SOCKS5-прокси создаются лениво по мере обращения к нодам с proxy_url.
# Ключ: (raw-строка прокси, mtls, apply). Старые записи живут до shutdown — рост ограничен
# числом уникальных прокси, keepalive-соединения гаснут через 30с.
_proxy_clients: dict[tuple[str, bool, bool], httpx.AsyncClient] = {}
# Внешние запросы через SOCKS5 (облачные биллинги с прокси у проекта). Ключ — raw-строка прокси
_external_proxy_clients: dict[str, httpx.AsyncClient] = {}

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
        limits=_PROXY_APPLY_LIMITS if apply else _PROXY_LIMITS,
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


def _raise_open_files_limit() -> None:
    """Поднять мягкий лимит открытых файлов процесса до жёсткого."""
    try:
        import resource
    except ImportError:  # не Linux: локальный запуск и тесты
        return

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = OPEN_FILES_TARGET if hard == resource.RLIM_INFINITY else min(hard, OPEN_FILES_TARGET)
    if soft != resource.RLIM_INFINITY and soft < wanted:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
            soft = wanted
        except (ValueError, OSError) as e:
            logger.warning("Could not raise the open files limit from %d to %d: %s", soft, wanted, e)

    if soft != resource.RLIM_INFINITY and soft < OPEN_FILES_COMFORTABLE:
        logger.warning(
            "Open files limit is %d: every node keeps its own connection, "
            "a fleet of thousands of nodes needs at least %d", soft, OPEN_FILES_COMFORTABLE,
        )


def _node_origin(url: str) -> str:
    """Адрес ноды без пути: по нему ноде выдаётся её клиент."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return url
    if not parts.hostname:
        return url
    return f"{parts.scheme}://{parts.hostname}:{port or ''}"


def _new_node_client(ctx: ssl.SSLContext, *, apply: bool) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        verify=ctx,
        timeout=_APPLY_TIMEOUT if apply else _NODE_TIMEOUT,
        limits=_NODE_APPLY_LIMITS if apply else _NODE_LIMITS,
        follow_redirects=False,
        http2=False,
        trust_env=False,
    )


async def _close_clients(clients: list[httpx.AsyncClient]) -> None:
    for client in clients:
        try:
            await client.aclose()
        except Exception as e:  # noqa: BLE001 — закрытие чужого сокета не должно ронять остальные
            logger.debug("Closing a node http client failed: %s", e)


def _prune_idle_clients(now: float) -> None:
    global _last_prune_at
    if now - _last_prune_at < PRUNE_INTERVAL:
        return
    _last_prune_at = now

    stale = [key for key, entry in _node_clients.items() if now - entry.last_used > NODE_CLIENT_IDLE_TTL]
    if not stale:
        return
    clients = [_node_clients.pop(key).client for key in stale]
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # вне event loop у клиента не могло появиться соединений
    task = loop.create_task(_close_clients(clients))
    _closing_tasks.add(task)
    task.add_done_callback(_closing_tasks.discard)


def _direct_node_client(server: "Server", *, apply: bool) -> httpx.AsyncClient:
    mtls = bool(getattr(server, "pki_enabled", False))
    ctx = _mtls_ctx if mtls else _legacy_ctx
    if ctx is None:
        raise RuntimeError(
            "mTLS http client is not initialized — PKI keygen missing at startup" if mtls
            else "Legacy http client not initialized — call init_http_clients() first"
        )

    now = _monotonic()
    _prune_idle_clients(now)
    key = (_node_origin(server.url), mtls, apply)
    entry = _node_clients.get(key)
    if entry is None:
        entry = _NodeClient(_new_node_client(ctx, apply=apply), now)
        _node_clients[key] = entry
    entry.last_used = now
    return entry.client


async def init_http_clients(keygen: "PKIKeygenData | None" = None) -> None:
    global _external_client, _cert_tmpdir, _mtls_ctx, _legacy_ctx, _last_prune_at

    _raise_open_files_limit()
    _last_prune_at = 0.0
    # Один контекст на все legacy-ноды: verify=False у каждого клиента создавал бы свой
    _legacy_ctx = httpx.create_ssl_context(verify=False)

    if keygen is not None:
        _mtls_ctx, _cert_tmpdir = _build_mtls_context(keygen)
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
    global _external_client, _cert_tmpdir, _mtls_ctx, _legacy_ctx
    # Клиенты держат ссылку на mTLS-контекст — закрываем до его сброса
    # (restore бэкапа делает close+init, старый контекст не должен пережить рестарт клиентов)
    node_clients = [entry.client for entry in _node_clients.values()]
    _node_clients.clear()
    await _close_clients([*node_clients, *_proxy_clients.values(), *_external_proxy_clients.values()])
    _proxy_clients.clear()
    _external_proxy_clients.clear()
    _mtls_ctx = None
    _legacy_ctx = None
    if _external_client:
        await _external_client.aclose()
        _external_client = None
    if _cert_tmpdir and _cert_tmpdir.exists():
        shutil.rmtree(_cert_tmpdir, ignore_errors=True)
        _cert_tmpdir = None


def get_node_client(server: "Server") -> httpx.AsyncClient:
    """Клиент коротких запросов к ноде: свой у каждой ноды, общий у нод за одним прокси."""
    proxy_raw = getattr(server, "proxy_url", None)
    if proxy_raw:
        return _get_proxy_client(
            proxy_raw, mtls=bool(getattr(server, "pki_enabled", False)), apply=False
        )
    return _direct_node_client(server, apply=False)


def get_node_apply_client(server: "Server") -> httpx.AsyncClient:
    """Клиент для долгих apply-запросов (firewall/haproxy profile apply) с read=300s
    и отдельными соединениями, чтобы не мешать потоку коротких запросов метрик."""
    proxy_raw = getattr(server, "proxy_url", None)
    if proxy_raw:
        return _get_proxy_client(
            proxy_raw, mtls=bool(getattr(server, "pki_enabled", False)), apply=True
        )
    return _direct_node_client(server, apply=True)


def new_node_probe_client(server: "Server") -> httpx.AsyncClient:
    """Одноразовый клиент без keepalive для проверки связи после смены адресов
    на ноде: соединение из пула могло пережить изменение и «доказать» ложное,
    проба обязана открыть TCP заново. Использовать через `async with`."""
    mtls = bool(getattr(server, "pki_enabled", False))
    if mtls and _mtls_ctx is None:
        raise RuntimeError("mTLS context is not initialized — PKI keygen missing at startup")
    proxy_raw = getattr(server, "proxy_url", None)
    return httpx.AsyncClient(
        proxy=_proxy_raw_to_url(proxy_raw) if proxy_raw else None,
        verify=_mtls_ctx if mtls else False,
        timeout=_NODE_TIMEOUT,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        follow_redirects=False,
        http2=False,
        trust_env=False,
    )


def node_auth_headers(server: "Server") -> dict[str, str]:
    """Заголовок X-API-Key только для legacy-нод; mTLS уже аутентифицирован TLS-слоем."""
    if getattr(server, "pki_enabled", False):
        return {}
    return {"X-API-Key": server.api_key or ""}


def get_external_client(proxy: str | None = None) -> httpx.AsyncClient:
    """Клиент для внешних API; с proxy ('ip:port[@login:pass]') — через SOCKS5."""
    if proxy:
        return _get_external_proxy_client(proxy)
    if _external_client is None:
        raise RuntimeError("HTTP clients not initialized — call init_http_clients() first")
    return _external_client


def _get_external_proxy_client(raw: str) -> httpx.AsyncClient:
    client = _external_proxy_clients.get(raw)
    if client is not None:
        return client

    client = httpx.AsyncClient(
        proxy=_proxy_raw_to_url(raw),
        timeout=_EXTERNAL_TIMEOUT,
        limits=_EXTERNAL_LIMITS,
        follow_redirects=True,
        http2=True,
        trust_env=False,
    )
    _external_proxy_clients[raw] = client
    logger.info("External SOCKS5 client created via %s", sanitize_proxy(raw))
    return client
