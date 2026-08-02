"""Управление nginx Remnawave-установки на хосте.

Установка живёт в каталоге на хосте (по умолчанию /opt/remnawave):
docker-compose.yml (сервисы remnawave-nginx + remnanode), nginx.conf, ssl/.
Файл nginx.conf смонтирован в контейнер remnawave-nginx как single-file
bind-mount, поэтому любые записи и откаты выполняются строго in-place
(write_host_file пишет через `> path`, сохраняя inode) — замена файла через
mv/rename создала бы новый inode, и контейнер продолжил бы видеть старый файл.

Правила парсит только панель: нода принимает готовый отрендеренный конфиг
и отвечает за транзакцию backup → write → validate → rollback → reload.
"""

import asyncio
import hashlib
import logging
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.services.host_executor import get_host_executor
from app.services.host_files import read_host_file_exact, write_host_file

logger = logging.getLogger(__name__)

NGINX_CONTAINER = "remnawave-nginx"
DEFAULT_INSTALL_PATH = "/opt/remnawave"
FALLBACK_NGINX_IMAGE = "nginx:1.28"
BACKUP_SUFFIX = ".bak"
CANDIDATE_SUFFIX = ".candidate"

# Путь приходит от панели и подставляется в shell-команды nsenter —
# допускаем только безопасный абсолютный путь без спецсимволов.
_SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
_MOUNT_TARGET_RE = re.compile(
    r"""^\s*-\s*['"]?\./nginx\.conf:([^\s'"#]+)""", re.MULTILINE
)
_COMPOSE_NOFILE_RE = re.compile(r"^\s*soft:\s*(\d+)\s*$", re.MULTILINE)
_COMPOSE_MOUNT_RE = re.compile(
    r"""^(?P<head>[ \t]*-[ \t]*['"]?\./nginx\.conf:)(?P<target>[^\s'":#]+)""", re.MULTILINE
)

# Профиль панели — полный nginx.conf. Установки из install.sh монтируют
# nginx.conf фрагментом http-контекста, туда полный конфиг не подходит
# (http внутри http), поэтому монтирование переводится на главный конфиг.
FULL_CONFIG_MOUNT = "/etc/nginx/nginx.conf"

# Строки, помеченные этим маркером, панель заполняет безопасными дефолтами,
# а нода пересчитывает под конкретный хост при каждом применении. Убрал
# маркер — значение считается заданным вручную и не трогается.
AUTO_MARKER = "# auto: node"
_AUTO_LINE_RE = re.compile(
    r"^(?P<head>[ \t]*(?P<directive>worker_rlimit_nofile|worker_connections|ssl_session_cache)[ \t]+)"
    r"(?P<value>[^;]+);(?P<tail>[ \t]*" + re.escape(AUTO_MARKER) + r".*)$",
    re.MULTILINE,
)

TUNING_FACTS_PATHS = (
    Path("/opt/monitoring/configs/tuning-facts.env"),
    Path("/host/opt/monitoring/configs/tuning-facts.env"),
)
MEMINFO_PATHS = (Path("/host/proc/meminfo"), Path("/proc/meminfo"))

DEFAULT_NOFILE = 65536
# 2 дескриптора на проксируемое соединение (клиент + апстрим) плюс запас
FD_PER_CONNECTION = 4
WORKER_CONNECTIONS_MIN = 1024
WORKER_CONNECTIONS_MAX = 65536
SSL_CACHE_MB_MIN = 10
SSL_CACHE_MB_MAX = 100
# ~1 МБ кэша TLS-сессий на каждые 100 МБ RAM хоста
SSL_CACHE_RAM_DIVISOR = 100

_DOCKER_TIMEOUT = 30
_RELOAD_TIMEOUT = 30
_RESTART_TIMEOUT = 90
_COMPOSE_UP_TIMEOUT = 100
_VALIDATE_TIMEOUT = 60


class InvalidInstallPathError(ValueError):
    def __init__(self, path: str):
        self.path = path
        super().__init__(f"Недопустимый путь установки Remnawave: {path!r}")


@dataclass
class DockerResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        return (self.stdout + self.stderr).strip()


def validate_install_path(path: str) -> str:
    path = path.rstrip("/") or "/"
    if not _SAFE_PATH_RE.match(path) or path == "/":
        raise InvalidInstallPathError(path)
    return path


def config_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _read_fact(name: str) -> Optional[int]:
    """Значение из tuning-facts.env, записанного рендерером системного тюнинга."""
    prefix = f"{name}="
    for path in TUNING_FACTS_PATHS:
        try:
            for line in path.read_text().splitlines():
                if line.startswith(prefix):
                    value = int(line.split("=", 1)[1].strip())
                    if value > 0:
                        return value
        except (OSError, ValueError):
            continue
    return None


def _read_mem_mb() -> Optional[int]:
    for path in MEMINFO_PATHS:
        try:
            for line in path.read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
        except (OSError, ValueError, IndexError):
            continue
    return None


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def compute_host_limits(compose_nofile: Optional[int] = None) -> dict:
    """Лимиты nginx под конкретный хост.

    Профиль в панели один на весь флот, а потолок дескрипторов у контейнера
    задаётся ulimits из compose (их считает рендерер системного тюнинга по
    RAM хоста). Просить у nginx больше этого потолка бессмысленно: setrlimit
    вернёт EPERM, и воркер молча останется со старым лимитом.
    """
    nofile = _read_fact("DOCKER_NOFILE") or _read_fact("NOFILE_LIMIT") \
        or compose_nofile or DEFAULT_NOFILE
    mem_mb = _read_mem_mb() or 1024
    return {
        "worker_rlimit_nofile": nofile,
        "worker_connections": _clamp(
            nofile // FD_PER_CONNECTION, WORKER_CONNECTIONS_MIN, WORKER_CONNECTIONS_MAX
        ),
        "ssl_session_cache": _clamp(
            mem_mb // SSL_CACHE_RAM_DIVISOR, SSL_CACHE_MB_MIN, SSL_CACHE_MB_MAX
        ),
    }


def compose_mount_target(compose: str) -> Optional[str]:
    match = _COMPOSE_MOUNT_RE.search(compose)
    return match.group("target") if match else None


def patch_compose_mount(compose: str) -> Optional[str]:
    """Переводит монтирование nginx.conf на главный конфиг.

    None — если менять нечего (уже полный конфиг или строки монтирования нет).
    """
    match = _COMPOSE_MOUNT_RE.search(compose)
    if not match or match.group("target") == FULL_CONFIG_MOUNT:
        return None
    return compose[: match.start("target")] + FULL_CONFIG_MOUNT + compose[match.end("target"):]


def patch_host_limits(content: str, limits: dict) -> str:
    """Подставляет вычисленные лимиты в строки с маркером `# auto: node`."""
    def replace(match: re.Match) -> str:
        directive = match.group("directive")
        if directive == "ssl_session_cache":
            value = f"shared:SSL:{limits['ssl_session_cache']}m"
        else:
            value = str(limits[directive])
        return f"{match.group('head')}{value};{match.group('tail')}"

    return _AUTO_LINE_RE.sub(replace, content)


async def _docker(*args: str, timeout: int = _DOCKER_TIMEOUT,
                  stdin_data: Optional[str] = None) -> DockerResult:
    """Docker CLI напрямую из контейнера агента (сокет смонтирован)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(stdin_data.encode("utf-8") if stdin_data is not None else None),
            timeout=timeout,
        )
        return DockerResult(proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace"))
    except asyncio.TimeoutError:
        return DockerResult(124, "", f"docker {' '.join(args[:2])}: timeout {timeout}s")
    except Exception as e:
        return DockerResult(1, "", str(e))


class RemnawaveNginxManager:
    def __init__(self):
        # Одновременные apply от панели ломали бы транзакцию backup/rollback
        self._apply_lock = asyncio.Lock()

    @staticmethod
    def _conf_path(path: str) -> str:
        return f"{path}/nginx.conf"

    async def container_status(self) -> dict:
        result = await _docker(
            "inspect", "-f", "{{.State.Running}}|{{.Config.Image}}", NGINX_CONTAINER,
            timeout=10,
        )
        if not result.success:
            return {"exists": False, "running": False, "image": None}
        running, _, image = result.stdout.strip().partition("|")
        return {"exists": True, "running": running.lower() == "true", "image": image or None}

    async def discover(self, path: str) -> dict:
        path = validate_install_path(path)
        executor = get_host_executor()
        probe = await executor.execute(
            f"[ -d {path} ] && echo dir; "
            f"[ -f {path}/docker-compose.yml ] && echo compose; "
            f"[ -f {path}/nginx.conf ] && echo conf; "
            f"[ -d {path}/ssl ] && echo ssl; true",
            timeout=10, shell="bash",
        )
        flags = set(probe.stdout.split()) if probe.success else set()

        compose_present = "compose" in flags
        nginx_conf_present = "conf" in flags

        mount_target = None
        compose_has_nginx = False
        if compose_present:
            compose = await read_host_file_exact(f"{path}/docker-compose.yml")
            if compose:
                compose_has_nginx = NGINX_CONTAINER in compose
                match = _MOUNT_TARGET_RE.search(compose)
                if match:
                    mount_target = match.group(1).split(":")[0]

        config_hash = None
        if nginx_conf_present:
            content = await read_host_file_exact(self._conf_path(path))
            if content is not None:
                config_hash = config_sha256(content)

        return {
            "found": compose_present and nginx_conf_present and compose_has_nginx,
            "path": path,
            "dir_present": "dir" in flags,
            "compose_present": compose_present,
            "nginx_conf_present": nginx_conf_present,
            "ssl_dir_present": "ssl" in flags,
            "mount_target": mount_target,
            "container": await self.container_status(),
            "config_hash": config_hash,
        }

    async def get_config(self, path: str) -> dict:
        path = validate_install_path(path)
        content = await read_host_file_exact(self._conf_path(path))
        if content is None:
            return {"exists": False, "content": None, "hash": None}
        return {"exists": True, "content": content, "hash": config_sha256(content)}

    async def status(self, path: str) -> dict:
        """Лёгкий статус для live-поллинга панели."""
        path = validate_install_path(path)
        content = await read_host_file_exact(self._conf_path(path))
        return {
            "container": await self.container_status(),
            "config_hash": config_sha256(content) if content is not None else None,
        }

    async def logs(self, tail: int = 100) -> str:
        result = await _docker("logs", "--tail", str(tail), NGINX_CONTAINER, timeout=15)
        return result.output

    async def _compose_nofile(self, path: str) -> Optional[int]:
        compose = await read_host_file_exact(f"{path}/docker-compose.yml")
        if not compose:
            return None
        match = _COMPOSE_NOFILE_RE.search(compose)
        return int(match.group(1)) if match else None

    async def apply_host_limits(self, path: str, content: str) -> str:
        """Пересчитывает помеченные `# auto: node` лимиты под этот хост."""
        if AUTO_MARKER not in content:
            return content
        limits = compute_host_limits(await self._compose_nofile(path))
        return patch_host_limits(content, limits)

    async def validate_content(self, path: str, content: str) -> tuple[bool, str]:
        """Проверка кандидата nginx -t без записи в рабочий файл.

        В живом контейнере кандидат кладётся в /tmp через stdin (docker cp
        недоступен: путь хоста не существует в ФС контейнера агента).
        Без живого контейнера — одноразовый контейнер с реальными
        сертификатами (letsencrypt + ssl каталог установки).
        """
        path = validate_install_path(path)
        content = await self.apply_host_limits(path, content)
        container = await self.container_status()

        if container["running"]:
            candidate = f"/tmp/nginx.conf{CANDIDATE_SUFFIX}"
            upload = await _docker(
                "exec", "-i", NGINX_CONTAINER, "sh", "-c", f"cat > {candidate}",
                stdin_data=content,
            )
            if not upload.success:
                return False, f"Не удалось загрузить кандидата в контейнер: {upload.output}"
            check = await _docker(
                "exec", NGINX_CONTAINER, "nginx", "-t", "-c", candidate,
                timeout=_VALIDATE_TIMEOUT,
            )
            await _docker("exec", NGINX_CONTAINER, "rm", "-f", candidate)
            return check.success, check.output

        image = container["image"] or FALLBACK_NGINX_IMAGE
        check = await _docker(
            "run", "--rm", "-i",
            "-v", "/etc/letsencrypt:/etc/letsencrypt:ro",
            "-v", f"{path}/ssl:/etc/nginx/ssl:ro",
            "--entrypoint", "sh", image,
            "-c", "cat > /tmp/candidate.conf && nginx -t -c /tmp/candidate.conf",
            timeout=_VALIDATE_TIMEOUT, stdin_data=content,
        )
        return check.success, check.output

    async def apply_config(self, path: str, content: str, reload_after: bool = True,
                           restart: bool = False, ensure_started: bool = False) -> dict:
        async with self._apply_lock:
            return await self._apply_config_locked(
                path, content, reload_after, restart, ensure_started
            )

    async def _apply_config_locked(self, path: str, content: str, reload_after: bool,
                                   restart: bool, ensure_started: bool) -> dict:
        path = validate_install_path(path)
        conf = self._conf_path(path)

        current = await read_host_file_exact(conf)
        if current is None:
            return self._apply_result(False, f"Файл {conf} не найден — установка Remnawave не обнаружена")

        # Профиль в панели один на весь флот — числа, зависящие от размера
        # хоста, подставляются здесь. Хэш считаем уже от итогового содержимого:
        # его же панель хранит как эталон для drift-детекции.
        content = await self.apply_host_limits(path, content)
        new_hash = config_sha256(content)
        container = await self.container_status()

        # Установка из install.sh монтирует nginx.conf фрагментом http-контекста:
        # полный конфиг профиля туда не подходит, монтирование переводится на
        # главный конфиг — это требует пересоздания контейнера, а не reload.
        remount = await self._prepare_remount(path)
        if remount is not None:
            return await self._apply_with_remount(path, conf, current, content, new_hash, remount)

        if not container["running"]:
            # Валидируем одноразовым контейнером ДО записи: живого nginx,
            # который отловил бы ошибку через exec nginx -t, здесь нет
            valid, output = await self.validate_content(path, content)
            if not valid:
                return self._apply_result(False, f"Конфиг не прошёл проверку: {output}", validated=False)
            if not await self._write_with_backup(conf, current, content):
                return self._apply_result(False, "Не удалось записать конфиг на хост")
            if ensure_started:
                started, msg = await self._compose_up(path)
                if not started:
                    return self._apply_result(False, f"Конфиг записан, но контейнер не запустился: {msg}",
                                              validated=True, hash_=new_hash)
                return self._apply_result(True, "Конфиг применён, контейнер запущен",
                                          validated=True, reloaded=True, hash_=new_hash)
            return self._apply_result(True, "Конфиг применён (контейнер remnawave-nginx не запущен)",
                                      validated=True, hash_=new_hash)

        if not await self._write_with_backup(conf, current, content):
            return self._apply_result(False, "Не удалось записать конфиг на хост")

        # bind-mount: контейнер уже видит новый файл, работающий nginx —
        # ещё нет (до reload), поэтому откат при ошибке безопасен
        check = await _docker("exec", NGINX_CONTAINER, "nginx", "-t", timeout=_VALIDATE_TIMEOUT)
        if not check.success:
            await self._rollback(conf, current)
            return self._apply_result(False, f"Конфиг не прошёл проверку, откат: {check.output}",
                                      validated=False)

        if not reload_after:
            return self._apply_result(True, "Конфиг применён (reload пропущен)",
                                      validated=True, hash_=new_hash)

        if restart:
            action = await _docker("restart", NGINX_CONTAINER, timeout=_RESTART_TIMEOUT)
        else:
            action = await _docker("exec", NGINX_CONTAINER, "nginx", "-s", "reload",
                                   timeout=_RELOAD_TIMEOUT)
        if not action.success:
            await self._rollback(conf, current)
            await _docker("exec", NGINX_CONTAINER, "nginx", "-s", "reload", timeout=_RELOAD_TIMEOUT)
            return self._apply_result(False, f"Reload не удался, откат: {action.output}", validated=True)

        verb = "перезапущен" if restart else "перезагружен"
        return self._apply_result(True, f"Конфиг применён, nginx {verb}",
                                  validated=True, reloaded=True, hash_=new_hash)

    async def _prepare_remount(self, path: str) -> Optional[dict]:
        """(compose_path, current, patched) если монтирование нужно перевести."""
        compose_path = f"{path}/docker-compose.yml"
        compose = await read_host_file_exact(compose_path)
        if not compose:
            return None
        patched = patch_compose_mount(compose)
        if patched is None:
            return None
        return {"path": compose_path, "current": compose, "patched": patched}

    async def _apply_with_remount(self, path: str, conf: str, current_conf: str,
                                  content: str, new_hash: str, remount: dict) -> dict:
        """Перевод фрагментной установки на полный конфиг.

        Кандидат проверяется ДО правки compose (nginx -t -c проверяет его именно
        как главный конфиг), затем меняются compose и nginx.conf, и контейнер
        пересоздаётся — смена точки монтирования иначе не подхватится.
        """
        valid, output = await self.validate_content(path, content)
        if not valid:
            return self._apply_result(False, f"Конфиг не прошёл проверку: {output}", validated=False)

        if not await self._write_with_backup(remount["path"], remount["current"], remount["patched"]):
            return self._apply_result(False, "Не удалось обновить docker-compose.yml установки")
        if not await self._write_with_backup(conf, current_conf, content):
            await write_host_file(remount["path"], remount["current"])
            return self._apply_result(False, "Не удалось записать конфиг на хост")

        recreated, msg = await self._compose_up(path, force_recreate=True)
        check = await _docker("exec", NGINX_CONTAINER, "nginx", "-t", timeout=_VALIDATE_TIMEOUT)
        if not recreated or not check.success:
            await write_host_file(remount["path"], remount["current"])
            await self._rollback(conf, current_conf)
            await self._compose_up(path, force_recreate=True)
            detail = msg if not recreated else check.output
            return self._apply_result(False, f"Не удалось перевести nginx на полный конфиг, откат: {detail}",
                                      validated=True)

        return self._apply_result(
            True,
            "Конфиг применён; монтирование переведено на полный nginx.conf, контейнер пересоздан",
            validated=True, reloaded=True, hash_=new_hash, remounted=True,
        )

    async def reload(self) -> tuple[bool, str]:
        container = await self.container_status()
        if not container["running"]:
            return False, "Контейнер remnawave-nginx не запущен"
        check = await _docker("exec", NGINX_CONTAINER, "nginx", "-t", timeout=_VALIDATE_TIMEOUT)
        if not check.success:
            return False, f"Текущий конфиг невалиден, reload отменён: {check.output}"
        result = await _docker("exec", NGINX_CONTAINER, "nginx", "-s", "reload",
                               timeout=_RELOAD_TIMEOUT)
        if not result.success:
            return False, result.output
        return True, "nginx перезагружен"

    async def restart(self) -> tuple[bool, str]:
        result = await _docker("restart", NGINX_CONTAINER, timeout=_RESTART_TIMEOUT)
        if not result.success:
            return False, result.output
        return True, "Контейнер remnawave-nginx перезапущен"

    @staticmethod
    def _apply_result(success: bool, message: str, validated: bool = False,
                      reloaded: bool = False, hash_: Optional[str] = None,
                      remounted: bool = False) -> dict:
        return {
            "success": success,
            "message": message,
            "validated": validated,
            "reloaded": reloaded,
            "hash": hash_,
            "remounted": remounted,
        }

    async def _write_with_backup(self, conf: str, current: str, content: str) -> bool:
        executor = get_host_executor()
        backup = await executor.execute(
            f"cp -f {shlex.quote(conf)} {shlex.quote(conf + BACKUP_SUFFIX)}", timeout=10
        )
        if not (backup.success and backup.exit_code == 0):
            logger.warning("remnawave nginx: backup failed for %s", conf)
            return False
        return await write_host_file(conf, content, mode="644")

    async def _rollback(self, conf: str, previous: str) -> None:
        if not await write_host_file(conf, previous):
            logger.error("remnawave nginx: rollback write failed for %s", conf)

    async def _compose_up(self, path: str, force_recreate: bool = False) -> tuple[bool, str]:
        executor = get_host_executor()
        flags = " --force-recreate" if force_recreate else ""
        result = await executor.execute(
            f"cd {path} && docker compose up -d{flags} {NGINX_CONTAINER}",
            timeout=_COMPOSE_UP_TIMEOUT, shell="bash",
        )
        if result.success and result.exit_code == 0:
            return True, "started"
        return False, (result.stderr or result.stdout or result.error or "compose up failed").strip()


_manager: Optional[RemnawaveNginxManager] = None


def get_remnawave_nginx_manager() -> RemnawaveNginxManager:
    global _manager
    if _manager is None:
        _manager = RemnawaveNginxManager()
    return _manager
