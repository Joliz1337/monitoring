"""SSH-доступ панели к серверам на трёх уровнях: общий, папка, сам сервер.

Общий доступ и доступ папок — строки ssh_access_profiles, свой доступ сервера —
колонки servers.ssh_*. Какой уровень достанется серверу, решает
ssh_target.resolve_ssh_target; здесь — хранение и правка уровней.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, fields
from enum import Enum
from typing import Collection, Iterable, Optional, Sequence, Union

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Server, SshAccessProfile
from app.services.http_client import sanitize_proxy
from app.services.ssh_access_check import CheckSubject
from app.services.ssh_target import (
    AccessLevel,
    LevelAccess,
    ProxyMode,
    SshAccessRules,
    SSHTargetError,
    level_from,
    login_level,
    resolve_ssh_target,
)

# Строка общего доступа: имя папки пустым не бывает (routers/servers.clean_folder_name)
SHARED_FOLDER = ""
# Сколько серверов без входа называет сводка; их число отдаётся полностью
WITHOUT_LOGIN_LIMIT = 200

Level = Union[Server, SshAccessProfile]


class AccessScope(str, Enum):
    GLOBAL = "global"
    FOLDERS = "folders"
    SERVERS = "servers"


class AccessPartName(str, Enum):
    LOGIN = "login"
    PORT = "port"
    PROXY = "proxy"


class FolderWithoutServers(Exception):
    """Доступ задают папке, в которой есть серверы: иначе он достался бы молча
    первой же папке, которую потом назовут так же."""

    def __init__(self, folder: str):
        super().__init__(f"В папке «{folder}» нет серверов")
        self.folder = folder


_PART_COLUMNS: dict[AccessPartName, tuple[str, ...]] = {
    AccessPartName.LOGIN: ("ssh_user", "ssh_password", "ssh_private_key", "ssh_passphrase"),
    AccessPartName.PORT: ("ssh_port",),
    AccessPartName.PROXY: ("ssh_proxy", "ssh_proxy_mode"),
}


@dataclass(frozen=True)
class AccessChange:
    """Правка уровня. None — не менять; пустая строка или 0 — стереть."""
    user: Optional[str] = None
    port: Optional[int] = None
    password: Optional[str] = None
    private_key: Optional[str] = None
    passphrase: Optional[str] = None
    proxy: Optional[str] = None
    proxy_mode: Optional[ProxyMode] = None

    @property
    def is_empty(self) -> bool:
        return all(getattr(self, item.name) is None for item in fields(self))

    @property
    def carries_secret(self) -> bool:
        return any(value is not None for value in (self.password, self.private_key, self.passphrase))


def _normalized_key(private_key: str) -> str:
    # Ключ, вставленный из Windows, приходит с CRLF и без завершающего перевода строки
    return private_key.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"


def _apply_login(level: Level, change: AccessChange) -> None:
    """Вход на уровне один — пароль либо ключ: при подключении ключ главнее пароля,
    и оставшийся рядом с новым паролем старый ключ молча использовался бы вместо него."""
    if change.user is not None:
        level.ssh_user = change.user.strip() or None

    if change.password:
        level.ssh_password, level.ssh_private_key, level.ssh_passphrase = change.password, None, None
    elif change.password is not None:
        level.ssh_password = None

    private_key = change.private_key
    if private_key and not private_key.strip():
        # Одни пробелы и переводы строк — случайный ввод: не ключ и не просьба стереть
        private_key = None

    if private_key:
        level.ssh_private_key = _normalized_key(private_key)
        level.ssh_passphrase = change.passphrase or None
        level.ssh_password = None
    elif private_key is not None:
        level.ssh_private_key, level.ssh_passphrase = None, None
    elif change.passphrase is not None:
        level.ssh_passphrase = change.passphrase or None


def _apply_proxy(level: Level, change: AccessChange) -> None:
    mode = ProxyMode(change.proxy_mode) if change.proxy_mode is not None else None
    if mode is ProxyMode.OFF:
        level.ssh_proxy, level.ssh_proxy_mode = None, ProxyMode.OFF.value
        return
    if change.proxy is None:
        # Режим без адреса имеет смысл только там, где адрес уже задан
        if mode is not None and level.ssh_proxy:
            level.ssh_proxy_mode = mode.value
        return

    address = change.proxy.strip()
    if not address:
        level.ssh_proxy, level.ssh_proxy_mode = None, None
        return
    kept_mode = level.ssh_proxy_mode if level.ssh_proxy else None
    level.ssh_proxy = address
    level.ssh_proxy_mode = mode.value if mode else kept_mode or ProxyMode.FALLBACK.value


def apply_change(level: Level, change: AccessChange) -> None:
    _apply_login(level, change)
    if change.port is not None:
        level.ssh_port = change.port or None
    _apply_proxy(level, change)


def clear_parts(level: Level, parts: Collection[AccessPartName]) -> None:
    for part in parts:
        for column in _PART_COLUMNS[AccessPartName(part)]:
            setattr(level, column, None)


def is_blank(level: Level) -> bool:
    return not any(getattr(level, column) for columns in _PART_COLUMNS.values() for column in columns)


def store_server_access(server: Server, change: AccessChange, ssh_host: Optional[str] = None) -> None:
    """Правка своего доступа сервера; ssh_host — его SSH-адрес (пустой — адрес из URL).

    Новый адрес, порт, пользователь или заново введённый пароль/ключ — доступ
    сохраняют заново, в том числе после переустановки сервера: закреплённый ключ
    хоста сбрасывается, и следующее подключение запомнит тот, что предъявит сервер.
    """
    address_before = (server.ssh_host, server.ssh_port, server.ssh_user)
    if ssh_host is not None:
        server.ssh_host = ssh_host.strip() or None
    apply_change(server, change)

    if change.carries_secret or (server.ssh_host, server.ssh_port, server.ssh_user) != address_before:
        server.ssh_host_key = None


def _clean_folders(folders: Sequence[str]) -> list[str]:
    """Имена папок как в servers.folder: без пробелов по краям, без повторов и пустых."""
    return list(dict.fromkeys(name.strip() for name in folders if name.strip()))


async def _profiles_by_folder(db: AsyncSession) -> dict[str, SshAccessProfile]:
    rows = (await db.execute(select(SshAccessProfile))).scalars().all()
    return {row.folder: row for row in rows}


async def _servers(db: AsyncSession, server_ids: Sequence[int]) -> list[Server]:
    return list((await db.execute(select(Server).where(Server.id.in_(server_ids)))).scalars().all())


async def _profile_folders(
    db: AsyncSession, scope: AccessScope, folders: Sequence[str], must_have_servers: bool,
) -> list[str]:
    if scope is AccessScope.GLOBAL:
        return [SHARED_FOLDER]
    names = _clean_folders(folders)
    if must_have_servers:
        rows = (await db.execute(select(Server.folder).where(Server.folder.in_(names)).distinct())).all()
        populated = {row.folder for row in rows}
        for name in names:
            if name not in populated:
                raise FolderWithoutServers(name)
    return names


async def save_access(
    db: AsyncSession, scope: AccessScope, folders: Sequence[str], server_ids: Sequence[int], change: AccessChange,
) -> int:
    """Применить правку к уровню: общему, перечисленным папкам или перечисленным
    серверам. Общий доступ и доступ папок записи серверов не трогают. Возвращает,
    сколько уровней изменено."""
    if scope is AccessScope.SERVERS:
        servers = await _servers(db, server_ids)
        for server in servers:
            store_server_access(server, change)
        await db.commit()
        return len(servers)

    names = await _profile_folders(db, scope, folders, must_have_servers=True)
    profiles = await _profiles_by_folder(db)
    for name in names:
        profile = profiles.get(name)
        if profile is None:
            profile = SshAccessProfile(folder=name)
            apply_change(profile, change)
            if not is_blank(profile):
                db.add(profile)
            continue
        apply_change(profile, change)
        if is_blank(profile):
            await db.delete(profile)
    await db.commit()
    return len(names)


async def clear_access(
    db: AsyncSession, scope: AccessScope, folders: Sequence[str], server_ids: Sequence[int],
    parts: Collection[AccessPartName],
) -> int:
    """Убрать части доступа с уровня: дальше они берутся с уровня выше. Закреплённый
    ключ хоста сервера не сбрасывается — сервер тот же, меняется только источник доступа."""
    if scope is AccessScope.SERVERS:
        servers = await _servers(db, server_ids)
        for server in servers:
            clear_parts(server, parts)
        await db.commit()
        return len(servers)

    # Папка могла опустеть — её доступ должно быть можно убрать
    names = await _profile_folders(db, scope, folders, must_have_servers=False)
    profiles = await _profiles_by_folder(db)
    existing = [profiles[name] for name in names if name in profiles]
    for profile in existing:
        clear_parts(profile, parts)
        if is_blank(profile):
            await db.delete(profile)
    await db.commit()
    return len(existing)


async def forget_host_keys(db: AsyncSession, server_ids: Sequence[int]) -> int:
    """Сбросить закреплённые ключи хоста: следующее подключение запомнит тот, что
    предъявит сервер. Возвращает, у скольких серверов ключ был."""
    pinned = [server for server in await _servers(db, server_ids) if server.ssh_host_key]
    for server in pinned:
        server.ssh_host_key = None
    await db.commit()
    return len(pinned)


async def rename_folder(db: AsyncSession, old_name: str, new_name: str) -> None:
    """Папку переименовали — её доступ переезжает следом. При слиянии с папкой,
    у которой доступ уже есть, остаётся доступ принимающей."""
    if not old_name or old_name == new_name:
        return
    profiles = await _profiles_by_folder(db)
    moved = profiles.get(old_name)
    if moved is None:
        return
    if new_name in profiles:
        await db.delete(moved)
    else:
        moved.folder = new_name
    await db.commit()


async def forget_folder(db: AsyncSession, name: str) -> None:
    """Папки больше нет — иначе новая папка с тем же именем получила бы её доступ молча."""
    if not name:
        return
    profile = (await _profiles_by_folder(db)).get(name)
    if profile is None:
        return
    await db.delete(profile)
    await db.commit()


async def check_subjects(
    db: AsyncSession, scope: AccessScope, folders: Sequence[str], server_ids: Sequence[int],
) -> list[CheckSubject]:
    """Активные серверы области с их SSH-целью для проверки доступа. Сервер, у
    которого доступ не собрался, идёт в проверку с причиной отказа."""
    query = select(Server).where(Server.is_active == True).order_by(Server.position)  # noqa: E712
    if scope is AccessScope.FOLDERS:
        query = query.where(Server.folder.in_(_clean_folders(folders)))
    elif scope is AccessScope.SERVERS:
        query = query.where(Server.id.in_(server_ids))
    servers = (await db.execute(query)).scalars().all()
    rules = await load_rules(db)

    subjects: list[CheckSubject] = []
    for server in servers:
        try:
            subjects.append((server.id, server.name, resolve_ssh_target(server, rules)))
        except SSHTargetError as exc:
            subjects.append((server.id, server.name, exc))
    return subjects


def _auth_kind(has_password: bool, has_private_key: bool) -> Optional[str]:
    if has_private_key:
        return "key"
    return "password" if has_password else None


def _level_view(
    user: Optional[str], port: Optional[int], auth: Optional[str], proxy: Optional[str], proxy_mode: Optional[str],
) -> dict:
    """Уровень для интерфейса: секретов нет, прокси — без логина и пароля."""
    return {
        "ssh_user": user,
        "ssh_port": port,
        "auth": auth,
        "proxy": sanitize_proxy(proxy) if proxy else None,
        "proxy_mode": proxy_mode,
    }


def _profile_view(level: LevelAccess) -> dict:
    auth = _auth_kind(bool(level.password), bool(level.private_key))
    return _level_view(level.user, level.port, auth, level.proxy, level.proxy_mode)


async def describe(db: AsyncSession) -> dict:
    """Что задано на каждом уровне и откуда серверы берут вход."""
    rules = rules_from_profiles((await db.execute(select(SshAccessProfile))).scalars().all())
    # Свой вход сервера виден признаком в запросе — записи целиком расшифровывали бы секреты всего парка
    servers = (await db.execute(
        select(
            Server.id, Server.name, Server.folder, Server.is_active, Server.ssh_user, Server.ssh_port,
            Server.ssh_password.isnot(None).label("has_password"),
            Server.ssh_private_key.isnot(None).label("has_private_key"),
            Server.ssh_proxy, Server.ssh_proxy_mode,
        ).order_by(Server.position)
    )).all()

    folder_sizes = Counter(row.folder for row in servers if row.folder)
    own_levels = []
    taken_from: Counter = Counter()
    without_login = []
    for row in servers:
        has_own_login = row.has_password or row.has_private_key
        if has_own_login or row.ssh_proxy or row.ssh_proxy_mode:
            auth = _auth_kind(row.has_password, row.has_private_key)
            own_levels.append({
                "server_id": row.id, "name": row.name, "folder": row.folder,
                **_level_view(row.ssh_user, row.ssh_port, auth, row.ssh_proxy, row.ssh_proxy_mode),
            })
        if not row.is_active:
            continue
        level = login_level(rules, row.folder, has_own_login)
        taken_from[level] += 1
        if level is None:
            without_login.append({"server_id": row.id, "name": row.name, "folder": row.folder})

    return {
        "global": _profile_view(rules.shared) if rules.shared else None,
        "folders": [
            {"folder": name, "servers": folder_sizes.get(name, 0), **_profile_view(level)}
            for name, level in sorted(rules.folders.items())
        ],
        "servers": own_levels,
        "summary": {
            "total": sum(taken_from.values()),
            "server": taken_from[AccessLevel.SERVER],
            "folder": taken_from[AccessLevel.FOLDER],
            "global": taken_from[AccessLevel.GLOBAL],
            "none": taken_from[None],
        },
        "without_login": without_login[:WITHOUT_LOGIN_LIMIT],
        "without_login_total": len(without_login),
    }


def rules_from_profiles(profiles: Iterable[SshAccessProfile]) -> SshAccessRules:
    shared = None
    folders = {}
    for profile in profiles:
        if profile.folder == SHARED_FOLDER:
            shared = level_from(profile)
        else:
            folders[profile.folder] = level_from(profile)
    return SshAccessRules(shared=shared, folders=folders)


async def load_rules(db: AsyncSession) -> SshAccessRules:
    """Правила одним запросом на операцию: таблица — строка на папку, не на сервер."""
    profiles = (await db.execute(select(SshAccessProfile))).scalars().all()
    return rules_from_profiles(profiles)
