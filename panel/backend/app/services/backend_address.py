"""Правка адреса бэкенда во всех HAProxy- и DNAT-профилях разом (страница «Потери»).

Адрес, до которого релеи теряют подключения, обычно стоит в нескольких профилях
сразу — менять его руками в каждом долго и легко пропустить место. Здесь поиск
и правка по всем профилям:
- HAProxy — точечно в тексте конфига: адрес в строках `server` бэкендов, а у
  https-правил ещё `sni str()` и заголовок Host. Генератором конфиг не
  пересобирается: пересборка стёрла бы всё, что правили в профиле руками;
- DNAT — в списке целей правила на том же месте: у нод с раскладкой «по адресу
  на ноду» (per_server) их адреса не сдвигаются.
Единственный бэкенд правила не удаляется, а порт DNAT-правила с несколькими
целями не меняется — это пропуск с причиной, а не тихая поломка раскатки.
"""

import ipaddress
import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_maker
from app.models import DnatProfile, HAProxyConfigProfile, Server
from app.services import dnat_profile_sync, haproxy_profile_sync
from app.services.loss_registry import RelaySnapshot

logger = logging.getLogger(__name__)


class Action(str, Enum):
    REPLACE = "replace"
    DELETE = "delete"


class Outcome(str, Enum):
    CHANGED = "changed"
    MERGED = "merged"  # новый IP уже был в списке целей — старый просто убран
    SKIPPED = "skipped"


class SkipReason(str, Enum):
    LAST_BACKEND = "last_backend"
    SHARED_PORT = "shared_port"


class AddressEditError(ValueError):
    pass


@dataclass(frozen=True)
class AddressEdit:
    ip: str
    port: int
    all_ports: bool
    action: Action
    new_ip: Optional[str] = None
    new_port: Optional[int] = None

    @property
    def changes_ip(self) -> bool:
        return self.action is Action.REPLACE and self.new_ip is not None and self.new_ip != self.ip

    @property
    def changes_port(self) -> bool:
        return self.action is Action.REPLACE and self.new_port is not None and self.new_port != self.port

    def matches(self, ip: str, port: Optional[int]) -> bool:
        return ip == self.ip and (self.all_ports or port == self.port)


def validate_edit(edit: AddressEdit) -> None:
    if edit.action is Action.REPLACE and not (edit.changes_ip or edit.changes_port):
        raise AddressEditError("nothing to change: new address equals the old one")
    if edit.changes_port and edit.all_ports:
        raise AddressEditError("port change applies to one port, not to all ports of the address")


@dataclass(frozen=True)
class RuleOutcome:
    rule: str
    outcome: Outcome
    reason: Optional[SkipReason] = None


# ---------------------------------------------------------------------------
# HAProxy: точечная правка текста конфига
# ---------------------------------------------------------------------------

_SECTION_RE = re.compile(
    r"^(global|defaults|frontend|backend|listen|resolvers|peers|userlist|cache|program|mailers|http-errors|ring)\b\s*(\S*)"
)
_SERVER_RE = re.compile(r"^(\s*server\s+\S+\s+)(\S+?):(\d+)(?=\s|$)")
_RULE_PREFIXES = ("backend_tcp_", "backend_https_")


def _rule_name(section_name: str) -> str:
    for prefix in _RULE_PREFIXES:
        if section_name.startswith(prefix):
            return section_name[len(prefix):]
    return section_name


def _sections(lines: list[str]) -> list[tuple[str, str, list[int]]]:
    """(вид, имя, индексы строк тела) каждой секции конфига."""
    sections: list[tuple[str, str, list[int]]] = []
    for index, line in enumerate(lines):
        header = _SECTION_RE.match(line)
        if header:
            sections.append((header.group(1), header.group(2), []))
        elif sections:
            sections[-1][2].append(index)
    return sections


def _replace_ip_token(text: str, pattern: str, old: str, new: str) -> str:
    return re.sub(pattern.format(re.escape(old)), lambda m: m.group(0).replace(old, new), text)


def edit_haproxy_config(config: str, edit: AddressEdit) -> tuple[str, list[RuleOutcome]]:
    lines = config.split("\n")
    dropped: set[int] = set()
    outcomes: list[RuleOutcome] = []

    for kind, name, body in _sections(lines):
        if kind not in ("backend", "listen"):
            continue
        servers = [(i, m) for i in body if (m := _SERVER_RE.match(lines[i]))]
        matched = [(i, m) for i, m in servers if edit.matches(m.group(2), int(m.group(3)))]
        if not matched:
            continue
        rule = _rule_name(name)

        if edit.action is Action.DELETE:
            if len(matched) == len(servers):
                outcomes.append(RuleOutcome(rule, Outcome.SKIPPED, SkipReason.LAST_BACKEND))
                continue
            dropped.update(i for i, _ in matched)
            outcomes.append(RuleOutcome(rule, Outcome.CHANGED))
            continue

        new_ip = edit.new_ip if edit.changes_ip else None
        for i, match in matched:
            port = edit.new_port if edit.changes_port else match.group(3)
            line = f"{match.group(1)}{new_ip or match.group(2)}:{port}{lines[i][match.end():]}"
            if new_ip:
                line = _replace_ip_token(line, r"sni str\({}\)", edit.ip, new_ip)
            lines[i] = line
        if new_ip:
            for i in body:
                lines[i] = _replace_ip_token(lines[i], r"set-header Host {}(?=\s|$)", edit.ip, new_ip)
        outcomes.append(RuleOutcome(rule, Outcome.CHANGED))

    kept = [line for index, line in enumerate(lines) if index not in dropped]
    return "\n".join(kept), outcomes


# ---------------------------------------------------------------------------
# DNAT: правка списка целей правила
# ---------------------------------------------------------------------------

def _dnat_port(rule: dict) -> Optional[int]:
    """Порт назначения правила; None — диапазон без явного порта (порт = входящий)."""
    target_port = int(rule.get("target_port") or 0)
    if target_port:
        return target_port
    listen = int(rule.get("listen_port") or 0)
    end = rule.get("listen_port_end")
    return listen if not end or int(end) == listen else None


def edit_dnat_rules(rules: list[dict], edit: AddressEdit) -> tuple[list[dict], list[RuleOutcome]]:
    result: list[dict] = []
    outcomes: list[RuleOutcome] = []
    for rule in rules:
        targets = [t for t in (rule.get("target_ip") or "").split(",") if t]
        if edit.ip not in targets or not edit.matches(edit.ip, _dnat_port(rule)):
            result.append(rule)
            continue
        name = rule.get("name", "")

        if edit.action is Action.DELETE:
            if len(targets) == 1:
                outcomes.append(RuleOutcome(name, Outcome.SKIPPED, SkipReason.LAST_BACKEND))
                result.append(rule)
                continue
            result.append({**rule, "target_ip": ",".join(t for t in targets if t != edit.ip)})
            outcomes.append(RuleOutcome(name, Outcome.CHANGED))
            continue

        if edit.changes_port and len(targets) > 1:
            outcomes.append(RuleOutcome(name, Outcome.SKIPPED, SkipReason.SHARED_PORT))
            result.append(rule)
            continue
        updated = dict(rule)
        if edit.changes_port:
            updated["target_port"] = edit.new_port
        outcome = Outcome.CHANGED
        if edit.changes_ip:
            if edit.new_ip in targets:
                targets = [t for t in targets if t != edit.ip]
                outcome = Outcome.MERGED
            else:
                targets = [edit.new_ip if t == edit.ip else t for t in targets]
            updated["target_ip"] = ",".join(targets)
        result.append(updated)
        outcomes.append(RuleOutcome(name, outcome))
    return result, outcomes


# ---------------------------------------------------------------------------
# Подсказки нового адреса
# ---------------------------------------------------------------------------

def suggest_addresses(snapshots: list[RelaySnapshot], ip: str) -> list[dict]:
    """Другие внешние IP ноды, которой принадлежит адрес, с их худшими потерями."""
    owner = next((s for s in snapshots if ip in s.addresses), None)
    if owner is None:
        return []
    worst: dict[str, float] = {}
    for snapshot in snapshots:
        for reading in snapshot.readings:
            worst[reading.ip] = max(worst.get(reading.ip, 0.0), reading.loss_pct)
    candidates = [a for a in owner.addresses if a != ip and ipaddress.ip_address(a).is_global]
    known = sorted((a for a in candidates if a in worst), key=lambda a: (worst[a], a))
    unknown = sorted(a for a in candidates if a not in worst)
    return [{"ip": a, "worst_loss": worst.get(a)} for a in known + unknown]


# ---------------------------------------------------------------------------
# Профили в базе
# ---------------------------------------------------------------------------

@dataclass
class ProfileChange:
    kind: str
    profile_id: int
    profile_name: str
    servers: int
    rules: list[RuleOutcome] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(r.outcome is not Outcome.SKIPPED for r in self.rules)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "servers": self.servers,
            "rules": [
                {"rule": r.rule, "outcome": r.outcome.value, "reason": r.reason.value if r.reason else None}
                for r in self.rules
            ],
        }


@dataclass(frozen=True)
class ProfileRef:
    kind: str
    profile_id: int
    profile_name: str


@dataclass
class BatchPlan:
    # По каждой правке списка — что она делает в каждом профиле
    items: list[list[ProfileChange]]
    # Профили, текст или правила которых в итоге изменились — их и раскатывать
    changed: list[ProfileRef]


LINK_COLUMNS = {"haproxy": Server.active_haproxy_profile_id, "dnat": Server.active_dnat_profile_id}
SYNC_STATUS_COLUMNS = {"haproxy": Server.haproxy_sync_status, "dnat": Server.dnat_sync_status}


async def _linked_count(db: AsyncSession, kind: str, profile_id: int) -> int:
    return int((await db.execute(select(func.count()).where(LINK_COLUMNS[kind] == profile_id))).scalar() or 0)


async def plan_batch(db: AsyncSession, edits: list[AddressEdit], save: bool = False) -> BatchPlan:
    """Правки применяются по очереди к одним и тем же текстам профилей, поэтому
    каждая видит результат предыдущих: два удаления из одного балансировщика не
    оставят его пустым. С save=True итог сохраняется, разъехавшиеся серверы —
    pending; коммит — за вызывающим."""
    haproxy = list((await db.execute(select(HAProxyConfigProfile).order_by(HAProxyConfigProfile.id))).scalars())
    dnat = list((await db.execute(select(DnatProfile).order_by(DnatProfile.id))).scalars())
    texts = {p.id: p.config_content or "" for p in haproxy}
    rules = {p.id: dnat_profile_sync.load_rules(p) for p in dnat}
    servers: dict[tuple[str, int], int] = {}

    async def change_for(kind: str, profile, outcomes: list[RuleOutcome]) -> ProfileChange:
        key = (kind, profile.id)
        if key not in servers:
            servers[key] = await _linked_count(db, kind, profile.id)
        return ProfileChange(kind, profile.id, profile.name, servers[key], outcomes)

    items: list[list[ProfileChange]] = []
    for edit in edits:
        changes = []
        for profile in haproxy:
            text, outcomes = edit_haproxy_config(texts[profile.id], edit)
            if outcomes:
                changes.append(await change_for("haproxy", profile, outcomes))
                texts[profile.id] = text
        for profile in dnat:
            updated, outcomes = edit_dnat_rules(rules[profile.id], edit)
            if outcomes:
                changes.append(await change_for("dnat", profile, outcomes))
                rules[profile.id] = updated
        items.append(changes)

    dirty_haproxy = [p for p in haproxy if texts[p.id] != (p.config_content or "")]
    dirty_dnat = [p for p in dnat if rules[p.id] != dnat_profile_sync.load_rules(p)]
    if save:
        for profile in dirty_haproxy:
            profile.config_content = texts[profile.id]
            await haproxy_profile_sync.mark_outdated_pending(db, profile)
        for profile in dirty_dnat:
            profile.rules_json = json.dumps(rules[profile.id])
            linked = await dnat_profile_sync.ordered_linked_servers(profile.id, db)
            for index, server in enumerate(linked):
                expected = dnat_profile_sync.compute_rules_hash(
                    dnat_profile_sync.render_rules_for_server(rules[profile.id], index))
                if server.dnat_rules_hash != expected:
                    server.dnat_sync_status = "pending"
    changed = [ProfileRef("haproxy", p.id, p.name) for p in dirty_haproxy] + \
              [ProfileRef("dnat", p.id, p.name) for p in dirty_dnat]
    return BatchPlan(items, changed)


async def sync_profile(ref: ProfileRef) -> None:
    """Раскатка профиля — тем же путём, что и сохранение в его разделе."""
    model = HAProxyConfigProfile if ref.kind == "haproxy" else DnatProfile
    sync = (haproxy_profile_sync if ref.kind == "haproxy" else dnat_profile_sync).sync_profile_to_servers
    async with async_session_maker() as db:
        profile = await db.get(model, ref.profile_id)
        if profile:
            await sync(profile, db)


async def rollout_progress(db: AsyncSession, refs: list[ProfileRef]) -> list[dict]:
    """Сколько привязанных нод каждого профиля уже получили конфиг — по статусам в базе."""
    progress = []
    for ref in refs:
        status = SYNC_STATUS_COLUMNS[ref.kind]
        rows = (await db.execute(
            select(status, func.count()).where(LINK_COLUMNS[ref.kind] == ref.profile_id).group_by(status)
        )).all()
        counts = {"synced": 0, "failed": 0, "denied": 0, "pending": 0}
        for value, count in rows:
            counts[value if value in ("synced", "failed", "denied") else "pending"] += count
        progress.append({
            "kind": ref.kind, "profile_id": ref.profile_id, "profile_name": ref.profile_name,
            "total": sum(counts.values()), **counts,
        })
    return progress
