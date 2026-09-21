"""Разведка и вырезание средств доступа хостера внутрь ВМ.

Хостеры (VK, Selectel, Timeweb, Yandex и др.) оставляют в арендованной машине
собственные каналы управления и наблюдения: qemu-guest-agent (гипервизор через
него выполняет команды и сбрасывает root-пароль), cloud-init (на перезагрузке
может заново прописать пароль/ключи), мониторинг-агентов, свои SSH-ключи, лишних
sudo-юзеров, apt-зеркала и cron. Модуль их находит (scan, read-only) и по команде
оператора вырезает (purge).

Разведка — один read-only shell-скрипт за один заход (SCAN_SCRIPT), его вывод
разбирают чистые функции (parse_scan_output → HostFacts), а detect() по фактам
строит находки. Purge всегда заново сканирует и сопоставляет запрошенные id с
реально найденным: клиент не может удалить произвольный путь, только то, что
разведка действительно обнаружила.

Собственный канал панели (её агент, nginx на :9100, docker, наши юниты) в находки
не попадает: агенты ищутся по allow-list известных имён, а не «всё нестандартное».
"""

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.models.hoster_access import (
    HosterFinding,
    HosterPurgeResponse,
    HosterPurgeResultItem,
    HosterScanResponse,
)
from app.services.host_executor import get_host_executor
from app.services.host_files import read_host_file_exact, write_host_file

logger = logging.getLogger(__name__)

# Долгие apt purge не должны упираться в лок dpkg (unattended-upgrades) — короткое
# ожидание лока лучше мгновенного провала.
PURGE_TIMEOUT = 240
SCAN_TIMEOUT = 45
APT_LOCK_WAIT = "-o DPkg::Lock::Timeout=60"

# Известные агенты хостера/облака (allow-list, а не «всё нестандартное» — так наши
# сервисы nginx/haproxy/ddos-watchdog/docker в находки не попадают физически).
# Имя пакета и имя юнита у одного агента различаются (Timeweb: пакет
# zabbix-agent-timeweb, юнит zabbix-agent) — поэтому у сигнатуры оба списка, и
# purge гасит все юниты и вычищает все пакеты группы одной находкой.
@dataclass(frozen=True)
class AgentSig:
    id: str
    title: str
    packages: tuple[str, ...]
    units: tuple[str, ...]
    access_critical: bool = False
    extra_cmds: tuple[str, ...] = ()


AGENT_SIGS: tuple[AgentSig, ...] = (
    AgentSig("telegraf", "Telegraf (InfluxData)", ("telegraf",), ("telegraf",)),
    AgentSig(
        "zabbix", "Zabbix agent",
        ("zabbix-agent", "zabbix-agent2", "zabbix-agent-timeweb"),
        ("zabbix-agent", "zabbix-agent2"),
    ),
    AgentSig(
        "node-exporter", "Prometheus node_exporter",
        ("prometheus-node-exporter", "node-exporter"),
        ("prometheus-node-exporter", "node-exporter", "node_exporter"),
    ),
    AgentSig("netdata", "Netdata", ("netdata",), ("netdata",)),
    AgentSig("collectd", "collectd", ("collectd",), ("collectd",)),
    AgentSig("walinuxagent", "Azure Linux Agent", ("walinuxagent",), ("walinuxagent",)),
    AgentSig(
        "google-guest", "Google/Yandex Guest Agent",
        ("google-guest-agent", "google-osconfig-agent"),
        ("google-guest-agent", "google-osconfig-agent"),
        access_critical=True,
        extra_cmds=("rm -f /etc/sudoers.d/google_sudoers 2>/dev/null || true",),
    ),
    AgentSig("amazon-ssm", "Amazon SSM Agent", ("amazon-ssm-agent",), ("amazon-ssm-agent",)),
    AgentSig("datadog", "Datadog Agent", ("datadog-agent",), ("datadog-agent",)),
    AgentSig("newrelic", "New Relic Infrastructure", ("newrelic-infra",), ("newrelic-infra",)),
    AgentSig("qualys", "Qualys Cloud Agent", ("qualys-cloud-agent",), ("qualys-cloud-agent",)),
    AgentSig("cloudbase-init", "Cloudbase-Init", ("cloudbase-init",), ("cloudbase-init",)),
)

# Точные хосты основных apt-зеркал хостеров (кандидаты на подмену стоковым архивом).
HOSTER_MIRROR_HOSTS: tuple[str, ...] = (
    "mirror.timeweb.ru",
    "mirror.timeweb.com",
    "mirror.selectel.ru",
    "mirror.yandex.ru",
    "mirror.vkcs.cloud",
    "mirror.vk.cloud",
)

# Домены инфраструктуры хостеров: любой их хост, кроме основного зеркала на пути
# /ubuntu, — сторонний репозиторий (zabbix.repo.timeweb.ru, mirror.selectel.ru/3rd-party),
# выбрасывается целиком.
HOSTER_REPO_DOMAINS: tuple[str, ...] = (
    ".timeweb.ru",
    ".timeweb.cloud",
    ".timeweb.com",
    ".selectel.ru",
    ".selectel.com",
    ".yandex.ru",
    ".yandexcloud.net",
    ".vkcs.cloud",
    ".vk.cloud",
)

STOCK_MIRROR = "archive.ubuntu.com"
_URL_RE = re.compile(r'https?://([^/\s\]"\']+)(/[^\s\]"\']*)?')

# Маркеры хостерского происхождения SSH-ключа (по комментарию/опциям).
HOSTER_KEY_MARKERS: tuple[str, ...] = (
    "generated-by-nova",
    "openstack",
    "cloud-init",
    "cloudinit",
    "google",
    "selectel",
    "timeweb",
    "yandex",
    "vkcloud",
    "please login as",
)

# Юзеры, которых заводит cloud-init/облако по умолчанию.
CLOUD_DEFAULT_USERS: tuple[str, ...] = ("ubuntu", "cloud-user", "admin", "user1", "debian")

CLOUD_INIT_UNITS = "cloud-init cloud-init-local cloud-config cloud-final"


@dataclass
class HostFacts:
    vendor: str = ""
    packages: set[str] = field(default_factory=set)
    unit_files: set[str] = field(default_factory=set)
    unit_states: dict[str, str] = field(default_factory=dict)  # base-имя юнита → состояние
    qemu_active: str = ""
    qemu_ports: list[str] = field(default_factory=list)
    cloudinit_disabled: bool = False
    cloudinit_installed: bool = False
    keys: list[tuple[str, str, str]] = field(default_factory=list)  # (file, fp_line, raw_b64)
    users: list[tuple[str, int, str, str]] = field(default_factory=list)  # name, uid, home, shell
    sudoers: list[tuple[str, str]] = field(default_factory=list)  # file, content
    group_members: dict[str, list[str]] = field(default_factory=dict)
    apt: list[tuple[str, str]] = field(default_factory=list)  # file, content
    sshd: dict[str, str] = field(default_factory=dict)
    dropins: list[tuple[str, str]] = field(default_factory=list)  # file, content


@dataclass
class DetectedItem:
    id: str
    category: str
    title: str
    detail: str
    severity: str
    access_critical: bool
    default_selected: bool
    remove_hint: str
    kind: str  # shell | ssh_key | apt_source
    commands: list[str] = field(default_factory=list)
    target_file: str = ""
    raw_line: str = ""  # для ssh_key — исходная строка ключа
    reboot: bool = False

    def to_finding(self) -> HosterFinding:
        return HosterFinding(
            id=self.id,
            category=self.category,
            title=self.title,
            detail=self.detail,
            severity=self.severity,  # type: ignore[arg-type]
            access_critical=self.access_critical,
            default_selected=self.default_selected,
            remove_hint=self.remove_hint,
        )


SCAN_SCRIPT = r"""
set +e
echo "@@VENDOR"
cat /sys/class/dmi/id/sys_vendor 2>/dev/null
cat /sys/class/dmi/id/product_name 2>/dev/null
echo "@@PKGS"
dpkg-query -W -f='${db:Status-Abbrev}\t${Package}\n' 2>/dev/null
echo "@@UNITS"
systemctl list-unit-files --type=service --no-legend 2>/dev/null | awk '{print $1"\t"$2}'
echo "@@QEMU_ACTIVE"
systemctl is-active qemu-guest-agent 2>/dev/null
echo "@@QEMU_PORT"
ls /dev/virtio-ports/ 2>/dev/null
echo "@@CLOUDINIT_DISABLED"
if [ -f /etc/cloud/cloud-init.disabled ]; then echo yes; else echo no; fi
echo "@@KEYS"
for f in /root/.ssh/authorized_keys /root/.ssh/authorized_keys2 /home/*/.ssh/authorized_keys; do
  [ -f "$f" ] || continue
  while IFS= read -r line; do
    case "$line" in "") continue ;; "#"*) continue ;; esac
    fp=$(printf '%s\n' "$line" | ssh-keygen -lf - 2>/dev/null | head -n1)
    b=$(printf '%s' "$line" | base64 -w0 2>/dev/null)
    printf 'KEY\t%s\t%s\t%s\n' "$f" "$fp" "$b"
  done < "$f"
done
echo "@@USERS"
getent passwd 2>/dev/null | awk -F: '$7 !~ /(nologin|false|sync)$/ {print $1":"$3":"$6":"$7}'
echo "@@SUDOERS"
for f in /etc/sudoers.d/*; do
  [ -f "$f" ] || continue
  case "$(basename "$f")" in README) continue ;; esac
  printf 'SUDO\t%s\t%s\n' "$f" "$(base64 -w0 < "$f" 2>/dev/null)"
done
echo "@@GROUPS"
getent group sudo google-sudoers adm 2>/dev/null
echo "@@APT"
for f in /etc/apt/sources.list /etc/apt/sources.list.d/*; do
  [ -f "$f" ] || continue
  printf 'APT\t%s\t%s\n' "$f" "$(base64 -w0 < "$f" 2>/dev/null)"
done
echo "@@SSHD"
sshd -T 2>/dev/null | grep -Ei '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication) '
echo "@@DROPINS"
for f in /etc/ssh/sshd_config.d/*.conf; do
  [ -f "$f" ] || continue
  printf 'DROPIN\t%s\t%s\n' "$f" "$(base64 -w0 < "$f" 2>/dev/null)"
done
echo "@@END"
"""


def _b64_decode(value: str) -> str:
    try:
        return base64.b64decode(value.encode("ascii")).decode("utf-8", errors="replace")
    except (ValueError, UnicodeDecodeError):
        return ""


def parse_scan_output(raw: str) -> HostFacts:
    """Разбор вывода SCAN_SCRIPT по секционным маркерам @@. Чистая функция."""
    facts = HostFacts()
    section = ""
    vendor_parts: list[str] = []

    for line in raw.splitlines():
        if line.startswith("@@"):
            section = line[2:].strip()
            continue

        if section == "VENDOR":
            if line.strip():
                vendor_parts.append(line.strip())
        elif section == "PKGS":
            # `${db:Status-Abbrev}` — напр. `ii`/`hi` (установлен), `rc` (снят,
            # остались конфиги), `un`. Считаем установленным только 2-й символ 'i'.
            parts = line.split("\t")
            if len(parts) == 2:
                status, pkg = parts[0].strip(), parts[1].strip()
                if pkg and len(status) >= 2 and status[1] == "i":
                    facts.packages.add(pkg)
        elif section == "UNITS":
            parts = line.split("\t")
            name = parts[0].strip()
            if not name:
                continue
            base = name[:-8] if name.endswith(".service") else name
            state = parts[1].strip() if len(parts) > 1 else ""
            facts.unit_files.add(base)
            facts.unit_states[base] = state
        elif section == "QEMU_ACTIVE":
            if line.strip():
                facts.qemu_active = line.strip()
        elif section == "QEMU_PORT":
            if line.strip():
                facts.qemu_ports.append(line.strip())
        elif section == "CLOUDINIT_DISABLED":
            if line.strip():
                facts.cloudinit_disabled = line.strip() == "yes"
        elif section == "KEYS":
            parts = line.split("\t")
            if parts[0] == "KEY" and len(parts) == 4:
                facts.keys.append((parts[1], parts[2], parts[3]))
        elif section == "USERS":
            row = line.strip()
            if row:
                fields = row.split(":")
                if len(fields) == 4 and fields[1].isdigit():
                    facts.users.append((fields[0], int(fields[1]), fields[2], fields[3]))
        elif section == "SUDOERS":
            parts = line.split("\t")
            if parts[0] == "SUDO" and len(parts) == 3:
                facts.sudoers.append((parts[1], _b64_decode(parts[2])))
        elif section == "GROUPS":
            row = line.strip()
            if row and ":" in row:
                name, _, _, members = (row.split(":", 3) + ["", "", "", ""])[:4]
                facts.group_members[name] = [m for m in members.split(",") if m]
        elif section == "APT":
            parts = line.split("\t")
            if parts[0] == "APT" and len(parts) == 3:
                facts.apt.append((parts[1], _b64_decode(parts[2])))
        elif section == "SSHD":
            row = line.strip()
            if " " in row:
                key, _, value = row.partition(" ")
                facts.sshd[key.lower()] = value.strip().lower()
        elif section == "DROPINS":
            parts = line.split("\t")
            if parts[0] == "DROPIN" and len(parts) == 3:
                facts.dropins.append((parts[1], _b64_decode(parts[2])))

    facts.vendor = " ".join(vendor_parts)
    facts.cloudinit_installed = "cloud-init" in facts.packages
    return facts


def classify_ssh_key(fp_line: str) -> bool:
    """True — ключ выглядит хостерским (можно предлагать к удалению по умолчанию)."""
    low = fp_line.lower()
    return any(marker in low for marker in HOSTER_KEY_MARKERS)


def _key_short(fp_line: str) -> str:
    """Короткое читаемое имя ключа из вывода ssh-keygen -lf."""
    parts = fp_line.split()
    if len(parts) >= 3:
        fp = parts[1]
        comment = " ".join(parts[2:-1]) if len(parts) > 3 else parts[2]
        return f"{fp} ({comment})"
    return fp_line.strip() or "ключ без отпечатка"


def _url_verdict(host: str, path: str) -> str:
    """keep — не хостер; rewrite — основное зеркало хостера на /ubuntu (меняем на
    стоковый архив); drop — любой другой хостерский репозиторий (выбросить)."""
    hostl = host.lower()
    if not any(hostl.endswith(dom) for dom in HOSTER_REPO_DOMAINS):
        return "keep"
    if hostl in HOSTER_MIRROR_HOSTS and (path or "").startswith("/ubuntu"):
        return "rewrite"
    return "drop"


def _url_verdicts(text: str) -> list[str]:
    return [_url_verdict(m.group(1), m.group(2) or "") for m in _URL_RE.finditer(text)]


def is_hoster_mirror_line(text: str) -> bool:
    """Есть ли в строке/блоке хоть один apt-источник хостера (rewrite или drop)."""
    return any(v != "keep" for v in _url_verdicts(text))


def _rewrite_hoster_urls(text: str) -> str:
    def repl(match: "re.Match[str]") -> str:
        host, path = match.group(1), match.group(2) or ""
        if _url_verdict(host, path) == "rewrite":
            return match.group(0).replace(host, STOCK_MIRROR, 1)
        return match.group(0)

    return _URL_RE.sub(repl, text)


def _strip_trusted_option(line: str) -> str:
    """Снять trusted=yes из однострочного `[...]`-блока опций (проверка подписи)."""
    if "trusted=yes" not in line:
        return line
    stripped = re.sub(r"trusted=yes\s*", "", line)
    stripped = stripped.replace("[]", "").replace("[ ]", "")
    return re.sub(r"\s{2,}", " ", stripped).strip()


def _neutralize_oneline(content: str) -> tuple[str, bool]:
    out: list[str] = []
    changed = False
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        verdicts = _url_verdicts(line)
        if "drop" in verdicts:
            changed = True  # сторонний репозиторий хостера — выкинуть строку целиком
            continue
        new_line = line
        if "rewrite" in verdicts:
            new_line = _rewrite_hoster_urls(new_line)
            changed = True
        if "trusted=yes" in new_line:
            new_line = _strip_trusted_option(new_line)
            changed = True
        out.append(new_line)
    return "\n".join(out) + ("\n" if content.endswith("\n") else ""), changed


def _neutralize_deb822(content: str) -> tuple[str, bool]:
    """deb822 (.sources): работаем целыми станцами (разделены пустой строкой),
    чтобы не осиротить блок, выкинув одну строку URIs.
    """
    blocks = re.split(r"\n\s*\n", content.strip("\n"))
    out_blocks: list[str] = []
    changed = False

    for block in blocks:
        lines = block.splitlines()
        uris = " ".join(l.split(":", 1)[1] for l in lines if l.strip().lower().startswith("uris:"))
        verdicts = _url_verdicts(uris)
        if "drop" in verdicts:
            changed = True  # сторонний репозиторий хостера — выбросить весь блок
            continue
        if "rewrite" in verdicts:
            new_lines = []
            for l in lines:
                low = l.strip().lower()
                if low.startswith("trusted:"):  # Trusted: yes — снимаем проверку подписи
                    changed = True
                    continue
                if low.startswith("uris:"):
                    l = _rewrite_hoster_urls(l)
                new_lines.append(l)
            out_blocks.append("\n".join(new_lines))
            changed = True
        else:
            new_lines = [l for l in lines if not l.strip().lower().startswith("trusted:")]
            if len(new_lines) != len(lines):
                changed = True
            out_blocks.append("\n".join(new_lines))

    result = "\n\n".join(b for b in out_blocks if b.strip())
    if result and not result.endswith("\n"):
        result += "\n"
    return result, changed


def neutralize_apt_source(content: str) -> tuple[str, bool]:
    """Обезвредить apt-источник хостера: снять trusted=yes (проверка подписи) и
    подменить зеркало хостера на стоковое archive.ubuntu.com; отдельные
    репозитории хостера (путь не /ubuntu) выбросить целиком. Понимает и
    однострочный формат, и deb822 (.sources). Чистая функция.
    """
    if re.search(r"(?im)^\s*(types|uris):", content):
        return _neutralize_deb822(content)
    return _neutralize_oneline(content)


def _unit_live(facts: HostFacts, base: str) -> bool:
    """Юнит присутствует и не обезврежен. Замаскированный (mask → symlink на
    /dev/null) остаётся в list-unit-files как `masked`, но запуститься не может —
    угрозой не считается, иначе purge+mask давал бы вечную находку."""
    state = facts.unit_states.get(base)
    if state is None:
        return False
    return state not in ("masked", "masked-runtime")


def _agent_items(facts: HostFacts) -> list[DetectedItem]:
    """Найденные агенты — одна находка на сигнатуру, даже если имя пакета и юнита
    различаются (zabbix-agent-timeweb / zabbix-agent). Purge гасит все юниты и
    вычищает все пакеты группы."""
    items: list[DetectedItem] = []
    for sig in AGENT_SIGS:
        pkgs = [p for p in sig.packages if p in facts.packages]
        units = [u for u in sig.units if _unit_live(facts, u)]
        if not pkgs and not units:
            continue
        commands = [f"systemctl disable --now {u} 2>/dev/null || true" for u in sig.units]
        commands += [f"systemctl mask {u} 2>/dev/null || true" for u in sig.units]
        commands += [
            f"DEBIAN_FRONTEND=noninteractive apt-get {APT_LOCK_WAIT} purge -y {p} 2>/dev/null || true"
            for p in sig.packages
        ]
        commands += list(sig.extra_cmds)
        seen = pkgs + [u for u in units if u not in pkgs]
        items.append(DetectedItem(
            id=f"agent:{sig.id}",
            category="monitoring_agent",
            title=sig.title,
            detail=f"Агент хостера/облака ({', '.join(seen)}): телеметрия и/или управление изнутри гостя.",
            severity="warning",
            access_critical=sig.access_critical,
            default_selected=True,
            remove_hint="disable + mask + purge",
            kind="shell",
            commands=commands,
        ))
    return items


def detect(facts: HostFacts) -> list[DetectedItem]:
    """По фактам собрать находки. Чистая функция — легко тестируется."""
    items: list[DetectedItem] = []

    # --- qemu-guest-agent ---
    # Триггер — установленный пакет или живой юнит, но НЕ голый virtio-порт:
    # порт даёт гипервизор, изнутри гостя он не убирается, и после purge агента
    # угрозы уже нет — иначе находка «возвращалась» бы навсегда.
    if "qemu-guest-agent" in facts.packages or _unit_live(facts, "qemu-guest-agent"):
        state = "работает" if facts.qemu_active == "active" else "установлен"
        items.append(DetectedItem(
            id="qemu_guest_agent",
            category="qemu_guest_agent",
            title="QEMU Guest Agent",
            detail=(
                f"qemu-guest-agent {state}. Через virtio-порт гипервизор может "
                "выполнять команды от root, читать/писать файлы и сбрасывать пароль."
            ),
            severity="danger",
            access_critical=True,
            default_selected=True,
            remove_hint="apt purge + mask; гипервизор теряет управление изнутри гостя",
            kind="shell",
            commands=[
                "apt-mark unhold qemu-guest-agent 2>/dev/null || true",
                "systemctl disable --now qemu-guest-agent 2>/dev/null || true",
                f"DEBIAN_FRONTEND=noninteractive apt-get {APT_LOCK_WAIT} purge -y qemu-guest-agent 2>/dev/null || true",
                "systemctl mask qemu-guest-agent 2>/dev/null || true",
            ],
        ))

    # --- cloud-init ---
    if facts.cloudinit_installed and not facts.cloudinit_disabled:
        items.append(DetectedItem(
            id="cloud_init",
            category="cloud_init",
            title="cloud-init",
            detail=(
                "cloud-init установлен и активен. На перезагрузке из seed/метаданных "
                "хостера он может заново прописать root-пароль, SSH-ключи и юзеров."
            ),
            severity="danger",
            access_critical=True,
            default_selected=True,
            remove_hint="disabled-флаг + purge; сеть в netplan остаётся файлом и переживёт удаление",
            kind="shell",
            commands=[
                "mkdir -p /etc/cloud && touch /etc/cloud/cloud-init.disabled",
                f"systemctl disable --now {CLOUD_INIT_UNITS} 2>/dev/null || true",
                f"DEBIAN_FRONTEND=noninteractive apt-get {APT_LOCK_WAIT} purge -y cloud-init 2>/dev/null || true",
                "rm -rf /etc/cloud/cloud.cfg.d/*_ec2* /etc/cloud/cloud.cfg.d/*datasource* 2>/dev/null || true",
            ],
        ))

    # --- мониторинг-агенты ---
    items.extend(_agent_items(facts))

    # --- чужие SSH-ключи ---
    for file_path, fp_line, raw_b64 in facts.keys:
        raw_line = _b64_decode(raw_b64)
        if not raw_line:
            continue
        hoster = classify_ssh_key(fp_line)
        detail = f"{file_path}: {_key_short(fp_line)}"
        if not hoster:
            detail += " — похоже на ваш ключ, снимайте галочку осознанно"
        items.append(DetectedItem(
            id=f"sshkey:{base64.urlsafe_b64encode(f'{file_path}|{fp_line}'.encode()).decode()}",
            category="foreign_ssh_key",
            title="SSH-ключ хостера" if hoster else "Сторонний SSH-ключ",
            detail=detail,
            severity="danger" if hoster else "warning",
            access_critical=True,
            default_selected=hoster,
            remove_hint="удаление строки из authorized_keys",
            kind="ssh_key",
            target_file=file_path,
            raw_line=raw_line,
        ))

    # --- лишние пользователи с логином ---
    google_sudoers = set(facts.group_members.get("google-sudoers", []))
    for name, uid, home, _shell in facts.users:
        if name == "root" or uid < 1000 or uid >= 65534:
            continue
        cloud_user = name in CLOUD_DEFAULT_USERS or name in google_sudoers
        detail = f"{name} (uid {uid}, {home})"
        if cloud_user:
            detail += " — заведён облаком/хостером"
        items.append(DetectedItem(
            id=f"user:{name}",
            category="foreign_user",
            title=f"Пользователь {name}",
            detail=detail,
            severity="warning",
            access_critical=True,
            default_selected=cloud_user,
            remove_hint="userdel -r + чистка sudoers.d",
            kind="shell",
            commands=[
                f"pkill -KILL -u {name} 2>/dev/null || true",
                f"userdel -r {name} 2>/dev/null || true",
                f"rm -f /etc/sudoers.d/90-cloud-init-users 2>/dev/null; sed -i '/^{name} /d' /etc/sudoers 2>/dev/null || true",
            ],
        ))

    # --- apt-зеркала хостера ---
    for file_path, content in facts.apt:
        has_trusted = re.search(r"(?i)trusted\s*[:=]\s*yes", content) is not None
        if not is_hoster_mirror_line(content) and not has_trusted:
            continue
        new_content, changed = neutralize_apt_source(content)
        if not changed:
            continue
        items.append(DetectedItem(
            id=f"apt:{base64.urlsafe_b64encode(file_path.encode()).decode()}",
            category="hoster_apt_repo",
            title="Репозиторий хостера",
            detail=f"{file_path}: зеркало хостера или trusted=yes (пакеты без проверки подписи).",
            severity="warning",
            access_critical=False,
            default_selected=True,
            remove_hint="подмена на archive.ubuntu.com и снятие trusted=yes",
            kind="apt_source",
            target_file=file_path,
        ))

    # --- ослабленный sshd (парольный вход / root по паролю) ---
    weak = []
    if facts.sshd.get("passwordauthentication") == "yes":
        weak.append("вход по паролю разрешён")
    if facts.sshd.get("permitrootlogin") == "yes":
        weak.append("root по паролю разрешён")
    if weak:
        items.append(DetectedItem(
            id="sshd_weak",
            category="sshd_weakening",
            title="Ослабленный SSH",
            detail=(
                "sshd: " + ", ".join(weak)
                + ". Оставлено хостером — упрощает вход по украденному/сброшенному паролю."
            ),
            severity="warning",
            access_critical=True,
            default_selected=False,
            remove_hint="только ключи: PasswordAuthentication no, PermitRootLogin prohibit-password",
            kind="shell",
            commands=[
                "mkdir -p /etc/ssh/sshd_config.d",
                "printf 'PasswordAuthentication no\\nKbdInteractiveAuthentication no\\nPermitRootLogin prohibit-password\\n' "
                "> /etc/ssh/sshd_config.d/99-monitoring-hoster.conf",
                "sshd -t 2>/dev/null && systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true",
            ],
        ))

    return items


def _hoster_hint(facts: HostFacts) -> str | None:
    haystack = (facts.vendor + " " + " ".join(c for _, c in facts.apt)).lower()
    for key, label in (
        ("selectel", "Selectel"),
        ("yandex", "Yandex Cloud"),
        ("timeweb", "Timeweb"),
        ("vkcs", "VK Cloud"),
        ("openstack", "OpenStack (VK/облако)"),
    ):
        if key in haystack:
            return label
    return facts.vendor or None


class HosterAccessManager:
    def __init__(self):
        self._executor = get_host_executor()

    async def _facts(self) -> HostFacts:
        result = await self._executor.execute(SCAN_SCRIPT, timeout=SCAN_TIMEOUT, shell="bash")
        return parse_scan_output(result.stdout or "")

    async def scan(self) -> HosterScanResponse:
        facts = await self._facts()
        findings = [item.to_finding() for item in detect(facts)]
        return HosterScanResponse(
            supported=True,
            hoster_hint=_hoster_hint(facts),
            generated_at=datetime.now(timezone.utc).isoformat(),
            findings=findings,
        )

    async def purge(self, finding_ids: list[str]) -> HosterPurgeResponse:
        # Заново сканируем и сопоставляем id с реально найденным: удалить можно
        # только то, что разведка обнаружила прямо сейчас.
        facts = await self._facts()
        by_id = {item.id: item for item in detect(facts)}

        results: list[HosterPurgeResultItem] = []
        reboot = False
        for fid in finding_ids:
            item = by_id.get(fid)
            if item is None:
                results.append(HosterPurgeResultItem(id=fid, ok=False, message="не найдено при повторном скане"))
                continue
            ok, message = await self._apply_one(item)
            results.append(HosterPurgeResultItem(id=fid, ok=ok, message=message))
            if ok and item.reboot:
                reboot = True

        return HosterPurgeResponse(results=results, reboot_recommended=reboot)

    async def _apply_one(self, item: DetectedItem) -> tuple[bool, str]:
        try:
            if item.kind == "ssh_key":
                return await self._remove_ssh_key(item)
            if item.kind == "apt_source":
                return await self._neutralize_apt(item)
            return await self._run_shell(item)
        except Exception as exc:  # noqa: BLE001 — граница операции: одна находка не роняет остальные
            logger.error("hoster purge failed for %s: %s", item.id, exc)
            return False, str(exc)

    async def _run_shell(self, item: DetectedItem) -> tuple[bool, str]:
        script = "\n".join(item.commands)
        result = await self._executor.execute(script, timeout=PURGE_TIMEOUT, shell="bash")
        if result.success:
            return True, "удалено"
        return False, (result.stderr or result.error or f"exit {result.exit_code}").strip()

    async def _remove_ssh_key(self, item: DetectedItem) -> tuple[bool, str]:
        content = await read_host_file_exact(item.target_file)
        if content is None:
            return False, "файл ключей недоступен"
        kept = [ln for ln in content.splitlines() if ln.strip() != item.raw_line.strip()]
        if len(kept) == len(content.splitlines()):
            return True, "ключ уже отсутствует"
        new_content = "\n".join(kept)
        if new_content:
            new_content += "\n"
        ok = await write_host_file(item.target_file, new_content, mode="600")
        return (True, "ключ удалён") if ok else (False, "не удалось записать authorized_keys")

    async def _neutralize_apt(self, item: DetectedItem) -> tuple[bool, str]:
        content = await read_host_file_exact(item.target_file)
        if content is None:
            return False, "файл репозитория недоступен"
        new_content, changed = neutralize_apt_source(content)
        if not changed:
            return True, "уже нейтрализовано"
        ok = await write_host_file(item.target_file, new_content, mode="644")
        return (True, "зеркало заменено на archive.ubuntu.com") if ok else (False, "не удалось записать источник")


_manager: HosterAccessManager | None = None


def get_hoster_access_manager() -> HosterAccessManager:
    global _manager
    if _manager is None:
        _manager = HosterAccessManager()
    return _manager
