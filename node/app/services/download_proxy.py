"""Прокси для загрузок ноды: где он прописан, проверка, смена и удаление.

Свой прокси установщик хранит в /etc/monitoring/proxy.conf и раскладывает в
apt, git и демон Docker. Но прокси, поставленный руками давно и мимо
установщика, живёт и в других местах: в настройках Docker-клиента (оттуда он
попадает в окружение контейнера агента и ломает всё, что панель запускает на
хосте), /root/.curlrc, /etc/environment. Мёртвый прокси в любом из них срывает
обновления, поэтому разведка смотрит во все, а удаление вычищает каждый.

Изменение для демона Docker действует только после его перезапуска, а прокси в
окружении агента — только после пересоздания контейнера. И то и другое убило бы
сам запрос посреди ответа, поэтому выполняется отложенно в отдельном юните
systemd, вне cgroup контейнера агента.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.services.host_executor import get_host_executor
from app.services.host_files import write_host_file

logger = logging.getLogger(__name__)

MONITORING_CONF = "/etc/monitoring/proxy.conf"
APT_OWN = "/etc/apt/apt.conf.d/99monitoring-proxy"
DOCKER_DROPIN_DIR = "/etc/systemd/system/docker.service.d/"
DOCKER_DROPIN_OWN = DOCKER_DROPIN_DIR + "proxy.conf"
DOCKER_DAEMON_JSON = "/etc/docker/daemon.json"
DOCKER_CLIENT_CONFIG = "/root/.docker/config.json"
GITCONFIG = "/root/.gitconfig"
CURLRC = "/root/.curlrc"
ENVIRONMENT = "/etc/environment"
NODE_DIR = "/opt/monitoring-node"
NODE_ENV = NODE_DIR + "/.env"
AGENT_ENV_LOCATION = "monitoring-api: env"

# Без кавычек, пробелов и символов, которые shell или systemd разберут по-своему:
# адрес уходит в sourced proxy.conf, apt.conf и Environment= юнита Docker
PROXY_URL_PATTERN = r"^https?://[A-Za-z0-9._~%!*+,=:@\[\]-]+/?$"

SCAN_TIMEOUT = 20
APPLY_TIMEOUT = 20
TEST_TIMEOUT_SECONDS = 10
TEST_TARGETS = ("https://github.com", "https://ghcr.io/v2/", "https://raw.githubusercontent.com")
# Пауза перед перезапуском: ответ панели должен успеть уйти, пока агент жив
RESTART_DELAY_SECONDS = 3

# Отметка в своих файлах: на сервере видно, откуда взялся прокси
OWN_HEADER = "Прокси для загрузок — задан панелью мониторинга"

SCAN_SCRIPT = r"""
set +e
for f in /etc/monitoring/proxy.conf /etc/apt/apt.conf /etc/apt/apt.conf.d/* \
         /etc/systemd/system/docker.service.d/*.conf /etc/docker/daemon.json \
         /root/.docker/config.json /root/.gitconfig /root/.curlrc /etc/environment \
         /opt/monitoring-node/.env; do
  [ -f "$f" ] || continue
  printf 'FILE\t%s\t%s\n' "$f" "$(base64 -w0 < "$f" 2>/dev/null)"
done
"""

_APT_PROXY = re.compile(r'^\s*Acquire::(?:https?|ftp)::Proxy\s+"([^"]+)"\s*;', re.IGNORECASE)
_SYSTEMD_PROXY = re.compile(r'(?:HTTPS?|ALL)_PROXY=([^"\s]+)', re.IGNORECASE)
_SYSTEMD_PROXY_ASSIGN = re.compile(r'\s*"?(?:HTTPS?|ALL|NO)_PROXY=[^"\s]*"?', re.IGNORECASE)
_ENV_PROXY = re.compile(r'^\s*(?:export\s+)?(?:https?|all)_proxy\s*=\s*["\']?([^"\'\s]+)', re.IGNORECASE)
_ENV_PROXY_LINE = re.compile(r'^\s*(?:export\s+)?(?:https?|all|no)_proxy\s*=', re.IGNORECASE)
_CURL_PROXY = re.compile(r'^\s*(?:-x|--proxy|proxy)\b\s*[=:]?\s*["\']?([^"\'\s]+)', re.IGNORECASE)
_GIT_SECTION = re.compile(r'^\s*\[\s*(https?)\b', re.IGNORECASE)
_GIT_ANY_SECTION = re.compile(r'^\s*\[')
_GIT_PROXY = re.compile(r'^\s*proxy\s*=\s*"?([^"\s]+)', re.IGNORECASE)
_URL_CREDENTIALS = re.compile(r"^(\w+://)([^:@/]+):[^@/]*@")
_AGENT_ENV_KEYS = ("http_proxy", "https_proxy", "all_proxy")


class ProxySource(str, Enum):
    MONITORING = "monitoring"
    APT = "apt"
    DOCKER_DAEMON = "docker_daemon"
    DOCKER_CLIENT = "docker_client"
    GIT = "git"
    CURL = "curl"
    ENVIRONMENT = "environment"
    NODE_ENV = "node_env"
    AGENT_ENV = "agent_env"


class DownloadProxyError(Exception):
    """Изменение не применилось на хосте."""


@dataclass(frozen=True)
class ProxyEntry:
    source: ProxySource
    location: str
    url: str

    def public(self) -> dict:
        return {"source": self.source.value, "location": self.location, "url": mask_proxy_url(self.url)}


@dataclass
class HostChanges:
    """Что записать и удалить на хосте, и что перезапустить после."""
    writes: dict[str, str] = field(default_factory=dict)
    deletes: set[str] = field(default_factory=set)
    restart_docker: bool = False
    recreate_agent: bool = False


def mask_proxy_url(url: str) -> str:
    """http://user:secret@host:3128 → http://user:•••@host:3128."""
    return _URL_CREDENTIALS.sub(r"\1\2:•••@", url)


def parse_scan_output(raw: str) -> dict[str, str]:
    files: dict[str, str] = {}
    for line in raw.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or parts[0] != "FILE":
            continue
        try:
            files[parts[1]] = base64.b64decode(parts[2]).decode("utf-8", errors="replace")
        except ValueError:
            continue
    return files


def _monitoring_url(content: str) -> Optional[str]:
    values = dict(line.split("=", 1) for line in content.splitlines() if "=" in line and not line.startswith("#"))
    url = values.get("PROXY_URL", "").strip().strip("'\"")
    return url if values.get("PROXY_ENABLED", "").strip() == "1" and url else None


def _json_proxies(content: str, path: str) -> list[str]:
    try:
        data = json.loads(content)
    except ValueError:
        return []
    proxies = data.get("proxies") if isinstance(data, dict) else None
    if not isinstance(proxies, dict):
        return []
    # daemon.json: {"http-proxy": …}; config.json клиента: {"default": {"httpProxy": …}}
    groups = proxies.values() if path == DOCKER_CLIENT_CONFIG else [proxies]
    urls = []
    for group in groups:
        if isinstance(group, dict):
            urls += [v for k, v in group.items() if isinstance(v, str) and v and "no" not in k.lower()]
    return urls


def _git_proxies(content: str) -> list[str]:
    urls, in_http = [], False
    for line in content.splitlines():
        if _GIT_ANY_SECTION.match(line):
            in_http = bool(_GIT_SECTION.match(line))
            continue
        match = _GIT_PROXY.match(line) if in_http else None
        if match:
            urls.append(match.group(1))
    return urls


def _line_proxies(content: str, pattern: re.Pattern) -> list[str]:
    urls = []
    for line in content.splitlines():
        match = pattern.match(line)
        if match and match.group(1).upper() not in ("DIRECT", "FALSE"):
            urls.append(match.group(1))
    return urls


def _file_proxies(path: str, content: str) -> tuple[Optional[ProxySource], list[str]]:
    if path == MONITORING_CONF:
        url = _monitoring_url(content)
        return ProxySource.MONITORING, [url] if url else []
    if path.startswith("/etc/apt/"):
        return ProxySource.APT, _line_proxies(content, _APT_PROXY)
    if path.startswith(DOCKER_DROPIN_DIR):
        return ProxySource.DOCKER_DAEMON, [m for line in content.splitlines()
                                           if line.strip().lower().startswith("environment")
                                           for m in _SYSTEMD_PROXY.findall(line)]
    if path in (DOCKER_DAEMON_JSON, DOCKER_CLIENT_CONFIG):
        source = ProxySource.DOCKER_DAEMON if path == DOCKER_DAEMON_JSON else ProxySource.DOCKER_CLIENT
        return source, _json_proxies(content, path)
    if path == GITCONFIG:
        return ProxySource.GIT, _git_proxies(content)
    if path == CURLRC:
        return ProxySource.CURL, _line_proxies(content, _CURL_PROXY)
    if path in (ENVIRONMENT, NODE_ENV):
        source = ProxySource.ENVIRONMENT if path == ENVIRONMENT else ProxySource.NODE_ENV
        return source, _line_proxies(content, _ENV_PROXY)
    return None, []


def detect(files: dict[str, str], agent_env: dict[str, str]) -> list[ProxyEntry]:
    entries: list[ProxyEntry] = []
    for path, content in sorted(files.items()):
        source, urls = _file_proxies(path, content)
        for url in dict.fromkeys(urls):
            entries.append(ProxyEntry(source, path, url))
    agent_urls = [v for k, v in agent_env.items() if k.lower() in _AGENT_ENV_KEYS and v]
    for url in dict.fromkeys(agent_urls):
        entries.append(ProxyEntry(ProxySource.AGENT_ENV, AGENT_ENV_LOCATION, url))
    return entries


def _strip_lines(content: str, pattern: re.Pattern) -> str:
    return "".join(line for line in content.splitlines(keepends=True) if not pattern.match(line))


def _strip_dropin(content: str) -> str:
    kept = []
    for line in content.splitlines(keepends=True):
        if not line.strip().lower().startswith("environment"):
            kept.append(line)
            continue
        rest = _SYSTEMD_PROXY_ASSIGN.sub("", line.split("=", 1)[1]).strip()
        if rest:
            kept.append("Environment=" + rest + "\n")
    return "".join(kept)


def _strip_json_proxies(content: str) -> str:
    data = json.loads(content)
    data.pop("proxies", None)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _strip_git(content: str) -> str:
    kept, in_http = [], False
    for line in content.splitlines(keepends=True):
        if _GIT_ANY_SECTION.match(line):
            in_http = bool(_GIT_SECTION.match(line))
        elif in_http and _GIT_PROXY.match(line):
            continue
        kept.append(line)
    # Секция [http]/[https], в которой был только прокси, иначе копилась бы пустой
    # при каждой смене адреса
    result = []
    for i, line in enumerate(kept):
        rest = [ln for ln in kept[i + 1:] if ln.strip()]
        if _GIT_SECTION.match(line) and (not rest or _GIT_ANY_SECTION.match(rest[0])):
            continue
        result.append(line)
    return "".join(result)


def _without_proxy(path: str, content: str) -> str:
    if path == MONITORING_CONF:
        return "PROXY_ENABLED=0\nPROXY_URL=\n"
    if path.startswith("/etc/apt/"):
        return _strip_lines(content, _APT_PROXY)
    if path.startswith(DOCKER_DROPIN_DIR):
        return _strip_dropin(content)
    if path in (DOCKER_DAEMON_JSON, DOCKER_CLIENT_CONFIG):
        return _strip_json_proxies(content)
    if path == GITCONFIG:
        return _strip_git(content)
    if path == CURLRC:
        return _strip_lines(content, _CURL_PROXY)
    return _strip_lines(content, _ENV_PROXY_LINE)


def plan_removal(files: dict[str, str], entries: list[ProxyEntry]) -> HostChanges:
    changes = HostChanges()
    for path in sorted({e.location for e in entries if e.source is not ProxySource.AGENT_ENV}):
        source = next(e.source for e in entries if e.location == path)
        if path in (APT_OWN, DOCKER_DROPIN_OWN):
            changes.deletes.add(path)
        else:
            changes.writes[path] = _without_proxy(path, files[path])
        if source is ProxySource.DOCKER_DAEMON:
            changes.restart_docker = True
    # Окружение контейнера фиксируется при создании: снять прокси из него можно,
    # только убрав источник (клиент Docker, .env ноды) и пересоздав контейнер
    agent_has_proxy = any(e.source is ProxySource.AGENT_ENV for e in entries)
    agent_source_fixed = any(e.source in (ProxySource.DOCKER_CLIENT, ProxySource.NODE_ENV) for e in entries)
    changes.recreate_agent = agent_has_proxy and agent_source_fixed
    return changes


def _systemd_escape(url: str) -> str:
    # В Environment= systemd раскрывает %-спецификаторы, а адрес бывает с %XX
    return url.replace("%", "%%")


def plan_set(files: dict[str, str], entries: list[ProxyEntry], url: str) -> HostChanges:
    """Один прокси по схеме установщика вместо всех найденных."""
    changes = plan_removal(files, entries)
    changes.writes[MONITORING_CONF] = f"# {OWN_HEADER}\nPROXY_ENABLED=1\nPROXY_URL='{url}'\n"
    changes.writes[APT_OWN] = (
        f"// {OWN_HEADER}\nAcquire::http::Proxy \"{url}\";\nAcquire::https::Proxy \"{url}\";\n"
    )
    changes.writes[DOCKER_DROPIN_OWN] = (
        f"# {OWN_HEADER}\n[Service]\n"
        f"Environment=\"HTTP_PROXY={_systemd_escape(url)}\"\n"
        f"Environment=\"HTTPS_PROXY={_systemd_escape(url)}\"\n"
        "Environment=\"NO_PROXY=localhost,127.0.0.1,::1\"\n"
    )
    git_base = changes.writes.get(GITCONFIG, files.get(GITCONFIG, ""))
    if git_base and not git_base.endswith("\n"):
        git_base += "\n"
    changes.writes[GITCONFIG] = git_base + f"[http]\n\tproxy = {url}\n[https]\n\tproxy = {url}\n"
    changes.deletes -= {APT_OWN, DOCKER_DROPIN_OWN}
    changes.restart_docker = True
    return changes


def restart_script(changes: HostChanges) -> Optional[str]:
    steps = []
    if changes.restart_docker:
        steps.append("systemctl restart docker")
    if changes.recreate_agent:
        steps.append(f"cd {NODE_DIR} && docker compose up -d --force-recreate")
    return " && ".join(steps) or None


def _probe(url: str, target: str) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": url, "https": url}))
    started = time.monotonic()
    try:
        with opener.open(urllib.request.Request(target, method="HEAD"), timeout=TEST_TIMEOUT_SECONDS) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        # Ответил сам сайт (401 у ghcr.io, 405 на HEAD) — значит прокси довёз запрос
        status = exc.code
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return {"target": target, "ok": False, "error": str(reason)[:200]}
    return {"target": target, "ok": True, "status": status, "ms": int((time.monotonic() - started) * 1000)}


class DownloadProxyManager:
    def __init__(self):
        self._executor = get_host_executor()

    async def _scan(self) -> tuple[dict[str, str], list[ProxyEntry]]:
        result = await self._executor.execute(SCAN_SCRIPT, timeout=SCAN_TIMEOUT, shell="bash")
        files = parse_scan_output(result.stdout or "")
        return files, detect(files, dict(os.environ))

    async def state(self) -> dict:
        _, entries = await self._scan()
        return {
            "entries": [e.public() for e in entries],
            "urls": list(dict.fromkeys(mask_proxy_url(e.url) for e in entries)),
        }

    async def summary(self) -> dict:
        state = await self.state()
        return {"urls": state["urls"], "sources": list(dict.fromkeys(e["source"] for e in state["entries"]))}

    async def set(self, url: str) -> dict:
        files, entries = await self._scan()
        return await self._apply(plan_set(files, entries, url))

    async def remove(self) -> dict:
        files, entries = await self._scan()
        return await self._apply(plan_removal(files, entries))

    async def test(self, url: Optional[str]) -> list[dict]:
        if url:
            urls = [url]
        else:
            _, entries = await self._scan()
            urls = list(dict.fromkeys(e.url for e in entries))
        checks = [asyncio.to_thread(_probe, u, target) for u in urls for target in TEST_TARGETS]
        results = await asyncio.gather(*checks)
        return [
            {"url": mask_proxy_url(u), "results": results[i * len(TEST_TARGETS):(i + 1) * len(TEST_TARGETS)]}
            for i, u in enumerate(urls)
        ]

    async def _apply(self, changes: HostChanges) -> dict:
        for path, content in changes.writes.items():
            mode = "600" if path in (MONITORING_CONF, GITCONFIG, NODE_ENV, DOCKER_CLIENT_CONFIG) else "644"
            if not await write_host_file(path, content, mode=mode, secret=True):
                raise DownloadProxyError(f"не удалось записать {path}")
        if changes.deletes:
            result = await self._executor.execute("rm -f " + " ".join(sorted(changes.deletes)), timeout=APPLY_TIMEOUT)
            if not result.success:
                raise DownloadProxyError(f"не удалось удалить {', '.join(sorted(changes.deletes))}")
        script = restart_script(changes)
        if script:
            await self._schedule(script)
        logger.info(
            "Download proxy changed: written=%s deleted=%s restart_docker=%s recreate_agent=%s",
            sorted(changes.writes), sorted(changes.deletes), changes.restart_docker, changes.recreate_agent,
        )
        return {
            "changed": sorted(changes.writes) + sorted(changes.deletes),
            "restart_docker": changes.restart_docker,
            "recreate_agent": changes.recreate_agent,
        }

    async def _schedule(self, script: str) -> None:
        # Отдельный юнит systemd: перезапуск Docker убивает контейнер агента, а с ним
        # и всё, что он запустил в своей cgroup. Без systemd-run — nohup: Docker
        # перезапустится и так, но пересоздание агента может не успеть
        unit = f"mon-download-proxy-{int(time.time())}"
        command = (
            f"systemctl daemon-reload; "
            f"systemd-run --unit={unit} --collect --on-active={RESTART_DELAY_SECONDS} /bin/sh -c '{script}' "
            f"|| nohup sh -c 'sleep {RESTART_DELAY_SECONDS}; {script}' >/dev/null 2>&1 &"
        )
        result = await self._executor.execute(command, timeout=APPLY_TIMEOUT, shell="bash")
        if not result.success:
            raise DownloadProxyError("не удалось запланировать перезапуск Docker")


_manager: Optional[DownloadProxyManager] = None


def get_download_proxy_manager() -> DownloadProxyManager:
    global _manager
    if _manager is None:
        _manager = DownloadProxyManager()
    return _manager
