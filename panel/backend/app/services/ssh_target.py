"""SSH-доступ панели к уже добавленному серверу (креды хранятся на записи Server).

Им пользуются доставка образа ноды (по кнопке и как запасной путь, когда нода не
смогла обновиться сама) и установка Remnawave, когда сервер за ТСПУ качает всё
через панель.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Optional, Sequence
from urllib.parse import urlparse

import asyncssh
from python_socks import ProxyError as SocksProxyError
from python_socks import ProxyType
from python_socks.async_.asyncio import Proxy as SocksProxy
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from app.database import async_session_maker
from app.models import Server
from app.services.http_client import parse_proxy_input, sanitize_proxy

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 30
DEFAULT_SSH_PORT = 22
DEFAULT_SSH_USER = "root"


class ProxyMode(str, Enum):
    FALLBACK = "fallback"  # основной путь, прокси — если он не подключился
    ALWAYS = "always"
    OFF = "off"  # отказ от прокси, заданного уровнем выше


class AccessLevel(str, Enum):
    SERVER = "server"
    FOLDER = "folder"
    GLOBAL = "global"


@dataclass(frozen=True)
class LevelAccess:
    """Что задано на одном уровне: у сервера, у папки или общее."""
    user: Optional[str] = None
    port: Optional[int] = None
    password: Optional[str] = None
    private_key: Optional[str] = None
    passphrase: Optional[str] = None
    proxy: Optional[str] = None
    proxy_mode: Optional[str] = None

    @property
    def has_login(self) -> bool:
        return bool(self.password or self.private_key)

    @property
    def has_proxy(self) -> bool:
        return bool(self.proxy) or self.proxy_mode == ProxyMode.OFF


@dataclass(frozen=True)
class SshAccessRules:
    """Общий доступ и доступ папок. Свой доступ сервера лежит в его записи."""
    shared: Optional[LevelAccess] = None
    folders: Mapping[str, LevelAccess] = field(default_factory=dict)


def level_from(row) -> LevelAccess:
    """Уровень из записи сервера или строки ssh_access_profiles: колонки ssh_* у них одноимённые."""
    return LevelAccess(
        user=row.ssh_user,
        port=row.ssh_port,
        password=row.ssh_password,
        private_key=row.ssh_private_key,
        passphrase=row.ssh_passphrase,
        proxy=row.ssh_proxy,
        proxy_mode=row.ssh_proxy_mode,
    )


@dataclass
class SSHTarget:
    host: str
    port: int = 22
    user: str = "root"
    password: Optional[str] = None
    private_key: Optional[str] = None
    passphrase: Optional[str] = None
    # Пути до сервера по порядку: None — напрямую, строка — SOCKS5-прокси
    # ("ip:port[@login:pass]"). Следующий путь берётся, когда предыдущий не подключился
    routes: tuple[Optional[str], ...] = (None,)
    # С какого уровня взят вход; None — из запроса
    login_level: Optional[AccessLevel] = None
    # Запись сервера и закреплённый за ней ключ хоста (формат OpenSSH): с ключом
    # сервер сверяется до аутентификации, без него первое подключение его запоминает
    server_id: Optional[int] = None
    host_key: Optional[str] = None


class SSHTargetError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason  # no_host | not_root | no_creds


class HostKeyChangedError(Exception):
    """Сервер предъявил не тот ключ хоста, что закреплён за ним. Сверка идёт до
    аутентификации, поэтому пароль и ключ панели такому серверу не уходят."""

    def __init__(self, host: str):
        super().__init__(
            f"SSH: ключ хоста {host} не совпадает с запомненным при первом подключении — "
            "соединение прервано до входа, пароль и ключ не отправлены. Если сервер "
            "переустановлен, откройте «SSH-доступ» на странице «Обновления», запустите "
            "проверку и примите новый ключ хоста у этого сервера"
        )


def host_from_url(url: str) -> str:
    return urlparse(url).hostname or ""


def has_stored_creds(server: "Server") -> bool:
    return bool(server.ssh_password or server.ssh_private_key)


def login_level(rules: SshAccessRules, folder: Optional[str], has_own_login: bool) -> Optional[AccessLevel]:
    """С какого уровня серверу достаётся вход. Секреты сервера для ответа не нужны —
    сводкам на весь парк хватает признака «свой вход есть»."""
    if has_own_login:
        return AccessLevel.SERVER
    folder_level = rules.folders.get(folder) if folder else None
    if folder_level is not None and folder_level.has_login:
        return AccessLevel.FOLDER
    if rules.shared is not None and rules.shared.has_login:
        return AccessLevel.GLOBAL
    return None


def ssh_routes(primary: Optional[str], proxy: Optional[str], mode: Optional[str]) -> tuple[Optional[str], ...]:
    """Пути до сервера: primary — как панель ходит к нему обычно (напрямую или через
    прокси из его карточки), proxy — прокси только для SSH и его режим."""
    if not proxy or mode == ProxyMode.OFF:
        return (primary,)
    if mode == ProxyMode.ALWAYS:
        return (proxy,)
    return (primary,) if proxy == primary else (primary, proxy)


@dataclass(frozen=True)
class SavedAccess:
    """Доступ сервера по уровням — без разовых кредов из запроса. Вход, порт и
    прокси берутся каждый со своего ближайшего уровня, где заданы."""
    login_level: Optional[AccessLevel]
    login: LevelAccess  # уровень, давший вход; пустой, если входа нет нигде
    port: int
    proxy: LevelAccess  # уровень, давший прокси; пустой, если прокси не задан

    @property
    def user(self) -> str:
        return (self.login.user or DEFAULT_SSH_USER).strip()


def saved_access(server: "Server", rules: SshAccessRules) -> SavedAccess:
    levels = [(AccessLevel.SERVER, level_from(server))]
    folder_level = rules.folders.get(server.folder) if server.folder else None
    if folder_level is not None:
        levels.append((AccessLevel.FOLDER, folder_level))
    if rules.shared is not None:
        levels.append((AccessLevel.GLOBAL, rules.shared))

    source, login = next(((name, level) for name, level in levels if level.has_login), (None, LevelAccess()))
    return SavedAccess(
        login_level=source,
        login=login,
        port=next((level.port for _, level in levels if level.port), DEFAULT_SSH_PORT),
        proxy=next((level for _, level in levels if level.has_proxy), LevelAccess()),
    )


def resolve_ssh_target(
    server: "Server",
    rules: SshAccessRules,
    *,
    ssh_host: Optional[str] = None,
    ssh_port: Optional[int] = None,
    ssh_user: Optional[str] = None,
    ssh_password: Optional[str] = None,
    ssh_private_key: Optional[str] = None,
    ssh_passphrase: Optional[str] = None,
) -> SSHTarget:
    """SSH-цель сервера: доступ по уровням, поля запроса — поверх него."""
    saved = saved_access(server, rules)
    if ssh_password or ssh_private_key:
        # Введённый пароль или ключ заменяет вход целиком: сохранённый ключ рядом
        # с введённым паролем при подключении молча победил бы
        source, login = None, LevelAccess(password=ssh_password or None, private_key=ssh_private_key or None)
    else:
        source, login = saved.login_level, saved.login

    host = (ssh_host or server.ssh_host or host_from_url(server.url)).strip()
    user = (ssh_user or login.user or DEFAULT_SSH_USER).strip()

    if not host:
        raise SSHTargetError("no_host", "Не удалось определить SSH-хост ноды")
    if user != DEFAULT_SSH_USER:
        raise SSHTargetError("not_root", "Нужен root-доступ по SSH")
    if not login.has_login:
        raise SSHTargetError(
            "no_creds", "Нет SSH-доступа: задайте его серверу, его папке или общий — либо укажите в запросе",
        )

    return SSHTarget(
        host=host,
        port=ssh_port or saved.port,
        user=user,
        password=login.password,
        private_key=login.private_key,
        passphrase=ssh_passphrase if ssh_passphrase is not None else login.passphrase,
        routes=ssh_routes(server.proxy_url or None, saved.proxy.proxy, saved.proxy.proxy_mode),
        server_id=server.id,
        host_key=server.ssh_host_key,
        login_level=source,
    )


def _trust_only(host_key: str) -> tuple:
    """known_hosts для asyncssh: доверенные ключи хоста, доверенные CA, отозванные ключи."""
    return ([asyncssh.import_public_key(host_key)], [], [])


def presented_host_key(conn: asyncssh.SSHClientConnection) -> Optional[str]:
    """Ключ хоста, которым сервер подтвердил это подключение, в формате OpenSSH."""
    key = conn.get_server_host_key()
    return key.export_public_key("openssh").decode().strip() if key else None


def ssh_connect_kwargs(t: SSHTarget) -> dict:
    kwargs: dict = {
        "host": t.host,
        "port": t.port,
        "username": t.user,
        "known_hosts": _trust_only(t.host_key) if t.host_key else None,
        "connect_timeout": CONNECT_TIMEOUT,
        # Долгая заливка образа или установка по медленному/throttled-каналу:
        # keepalive держит SSH-сессию живой в паузах между шагами.
        "keepalive_interval": 15,
        "keepalive_count_max": 8,
    }
    if t.private_key:
        kwargs["client_keys"] = [asyncssh.import_private_key(t.private_key, t.passphrase or None)]
    elif t.password:
        kwargs["password"] = t.password
    return kwargs


async def open_ssh_connection(
    connect_kwargs: dict, socks5_proxy: Optional[str], server_id: Optional[int] = None,
) -> asyncssh.SSHClientConnection:
    """SSH-подключение напрямую или через SOCKS5-прокси панель→сервер.

    Без закреплённого ключа хоста (known_hosts=None) подключение доверяет
    предъявленному и закрепляет его: в connect_kwargs — следующие подключения
    той же задачи сверяются уже с ним — и в записи сервера server_id.
    """
    pinned = connect_kwargs["known_hosts"] is not None
    try:
        conn = await _connect(connect_kwargs, socks5_proxy)
    except asyncssh.HostKeyNotVerifiable as exc:
        if not pinned:
            raise
        raise HostKeyChangedError(connect_kwargs["host"]) from exc
    if pinned:
        return conn
    try:
        await _pin_host_key(conn, connect_kwargs, server_id)
    except BaseException:
        # Задачу отменили, пока ключ писался в базу: подключение открыто, а отдать его некому
        conn.close()
        raise
    return conn


@dataclass
class RouteConnection:
    conn: asyncssh.SSHClientConnection
    route: Optional[str]  # None — напрямую
    # Пути, которыми не вышло, и причина каждого — для лога задачи
    skipped: list[tuple[Optional[str], str]]


class SshRoutesFailed(OSError):
    """Не подключились ни одним путём."""

    def __init__(self, attempts: list[tuple[Optional[str], str]]):
        super().__init__("; ".join(f"{route_label(route)}: {reason}" for route, reason in attempts))


def route_label(route: Optional[str]) -> str:
    return f"через SOCKS5 {sanitize_proxy(route)}" if route else "напрямую"


def prefer_route(routes: tuple[Optional[str], ...], working: Optional[str]) -> tuple[Optional[str], ...]:
    """Рабочий путь первым: переподключения той же задачи идут сразу им."""
    return (working, *(route for route in routes if route != working))


def connect_error_text(connect_kwargs: dict, exc: BaseException) -> str:
    # asyncssh обрывает подключение по connect_timeout через asyncio.wait_for —
    # исключение приходит без текста
    if isinstance(exc, asyncio.TimeoutError):
        return (
            f"{connect_kwargs['host']}:{connect_kwargs['port']} не ответил "
            f"за {connect_kwargs['connect_timeout']} с"
        )
    return str(exc) or type(exc).__name__


async def open_ssh_routes(
    connect_kwargs: dict, routes: Sequence[Optional[str]], server_id: Optional[int] = None,
) -> RouteConnection:
    """Подключиться первым путём, которым получится.

    На следующий путь переводит только сетевая ошибка. Неверный пароль и чужой
    ключ хоста вторым путём не повторяются: на сервере копились бы неудачные
    входы, а подменённому серверу ушёл бы пароль. Единственный путь отдаёт своё
    исключение как есть.
    """
    skipped: list[tuple[Optional[str], str]] = []
    for route in routes:
        try:
            conn = await open_ssh_connection(connect_kwargs, route, server_id)
        except asyncssh.PermissionDenied:
            raise
        except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
            if len(routes) == 1:
                raise
            skipped.append((route, connect_error_text(connect_kwargs, exc)))
            continue
        return RouteConnection(conn, route, skipped)
    raise SshRoutesFailed(skipped)


async def _pin_host_key(
    conn: asyncssh.SSHClientConnection, connect_kwargs: dict, server_id: Optional[int],
) -> None:
    host_key = presented_host_key(conn)
    if not host_key:
        return
    connect_kwargs["known_hosts"] = _trust_only(host_key)
    if server_id is not None:
        await _remember_host_key(server_id, host_key)


async def _remember_host_key(server_id: int, host_key: str) -> None:
    try:
        async with async_session_maker() as db:
            # IS NULL: параллельное первое подключение могло закрепить ключ раньше —
            # эталоном остаётся записанный первым
            result = await db.execute(
                update(Server)
                .where(Server.id == server_id, Server.ssh_host_key.is_(None))
                .values(ssh_host_key=host_key)
            )
            await db.commit()
    except (SQLAlchemyError, OSError) as exc:
        # Само подключение состоялось; ключ закрепит следующее
        logger.warning(f"SSH host key of server {server_id} was not saved: {exc}")
        return
    if result.rowcount:
        logger.info(f"SSH host key pinned for server {server_id}")


async def _connect(connect_kwargs: dict, socks5_proxy: Optional[str]) -> asyncssh.SSHClientConnection:
    """С прокси сокет открывает python-socks, asyncssh получает его через sock=.
    Ошибки прокси приходят как OSError с адресом прокси без логина и пароля —
    их ловят те же обработчики, что и сетевые ошибки прямого подключения.
    """
    if not socks5_proxy:
        return await asyncssh.connect(**connect_kwargs)

    host, port, login, password = parse_proxy_input(socks5_proxy)
    proxy = SocksProxy(ProxyType.SOCKS5, host, port, username=login, password=password)
    timeout = connect_kwargs.get("connect_timeout", CONNECT_TIMEOUT)
    proxy_address = sanitize_proxy(socks5_proxy)
    try:
        sock = await proxy.connect(
            dest_host=connect_kwargs["host"], dest_port=connect_kwargs["port"], timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        raise OSError(f"SOCKS5-прокси {proxy_address} не ответил за {timeout} с") from exc
    except (SocksProxyError, OSError) as exc:
        raise OSError(f"SOCKS5-прокси {proxy_address}: {exc}") from exc
    return await asyncssh.connect(**connect_kwargs, sock=sock)
