"""Умер ли адрес назначения: отключён почти на всех своих релеях.

Автоотключение решает по парам «релей — адрес», и каждое решение — строка в
Telegram: адрес, упавший на шести релеях, даёт шесть «отключён» и шесть
«возвращён». Оператору нужен один сигнал, когда адрес перестал работать почти
везде (его пора менять), и один, когда он вернулся. Модуль считает это состояние
по парам трекера. Чистый: ничего не шлёт и сам не хранит.

Доля — релеи, отключившие адрес, среди релеев, которые его держат. Держатель
помнится час после последнего замера: релей, с которым панель на минуты потеряла
связь, из знаменателя не выпадает — иначе «2 из 10» становилось бы «2 из 2».
Перезапуск панели — тот же перебой связи, поэтому держатели адресов, по которым
уже есть отключения, хранятся вместе с записями об умерших.
"""

from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping, Optional

from app.services.backend_quarantine import (
    WANTED_STATES,
    Event,
    EventKind,
    Mode,
    Pair,
    PairState,
    QuarantinePolicy,
    ReasonKind,
)

HOLDER_MEMORY_SEC = 3600
REVIVE_HOLD_SEC = 600
# У ноды с единственным адресом «нода умерла» и «адрес умер» — одно и то же
NODE_MIN_TARGETS = 2


@dataclass
class DeadEntry:
    # Строка истории алертов привязана к серверу: первый по имени релей из отключивших
    relay_id: int
    relay_name: str
    owner: Optional[str]
    # Адрес объявлен вместе со всей нодой: тогда и о возврате говорится одной строкой про ноду
    with_node: bool = False
    # С какого момента доля ниже порога; после перезапуска панели срок идёт заново
    below_since: Optional[float] = None


def revive_hold(policy: QuarantinePolicy) -> float:
    """Сколько адрес держится ниже порога, прежде чем считается ожившим: дольше, чем
    автоотключению нужно, чтобы заметить проблему заново. Иначе после перезапуска
    панели в режиме наблюдения адрес «оживал» бы, пока пары собираются заново."""
    return max(REVIVE_HOLD_SEC, 2 * policy.sustained_sec)


def _ip(target: str) -> str:
    return target.rpartition(":")[0]


def _failing(pairs: Iterable[Pair], mode: Mode) -> dict[str, list[Pair]]:
    """Адрес → пары, по которым автоотключение уже решило: отключить (в наблюдении — отключило бы)."""
    counted = (PairState.WOULD_DISABLE,) if mode is Mode.OBSERVE else WANTED_STATES
    failing: dict[str, list[Pair]] = {}
    for pair in pairs:
        if pair.state in counted:
            failing.setdefault(pair.target, []).append(pair)
    return failing


@dataclass
class DeadTracker:
    dead: dict[str, DeadEntry] = field(default_factory=dict)
    # Адрес → релей → когда релей последний раз голосовал по адресу
    _holders: dict[str, dict[int, float]] = field(default_factory=dict)
    # IP → нода-владелец. Реестр замеров забывает ноду через полторы минуты молчания,
    # а молчит как раз нода, которая умерла целиком
    _owners: dict[str, str] = field(default_factory=dict)
    # Адреса, по которым сейчас есть отключения: умереть могут только они, и только их
    # держателей нужно помнить через перезапуск
    _suspects: set[str] = field(default_factory=set)
    # Ноды, объявленные умершими целиком
    _dead_nodes: set[str] = field(default_factory=set)

    def observe(self, pairs: Iterable[Pair], voters: Mapping[str, set[int]], owners: Mapping[str, str],
                mode: Mode, share_pct: int, hold_sec: float, now: float, quiet: bool = False) -> list[Event]:
        """События «адрес умер» / «адрес ожил» за цикл. `quiet` — только запоминать, кто
        держит адреса: сразу после запуска панели реестр замеров ещё пуст."""
        self._owners.update(owners)
        self._remember_holders(voters, now)
        failing = _failing(pairs, mode)
        self._suspects = set(failing)
        if quiet:
            return []
        observe = mode is Mode.OBSERVE
        died: list[Event] = []
        revived: list[Event] = []
        for target in self._holders.keys() | failing.keys() | self.dead.keys():
            bad = failing.get(target, [])
            failing_relays = {pair.relay_id for pair in bad}
            total = len(self._holders.get(target, {}).keys() | failing_relays)
            is_dead = bool(failing_relays) and len(failing_relays) * 100 >= share_pct * total
            entry = self.dead.get(target)
            if entry is None:
                if is_dead:
                    died.append(self._die(target, bad, len(failing_relays), total, observe))
                continue
            if total == 0:
                # Адреса не осталось ни у одного релея: его убрали из конфигов, а не починили
                del self.dead[target]
                continue
            if is_dead:
                entry.below_since = None
                continue
            if not voters.get(target):
                # Свежих замеров нет — нет связи с релеями: сказать «снова работает» не по чему
                entry.below_since = None
                continue
            if entry.below_since is None:
                entry.below_since = now
            if now - entry.below_since >= hold_sec:
                del self.dead[target]
                revived.append(Event(
                    EventKind.ALIVE, entry.relay_id, entry.relay_name, target,
                    failing=len(failing_relays), total=total,
                    owner=entry.owner or self._owners.get(_ip(target)), observe=observe,
                ))
        return self._with_dead_nodes(died) + self._with_revived_nodes(revived)

    def clear(self) -> None:
        """Режим автоотключения сменили: прежние отключения сняты, объявлять о них нечего."""
        self.dead.clear()
        self._dead_nodes.clear()
        self._suspects.clear()

    def stored(self) -> dict[str, dict]:
        """Что должно пережить перезапуск панели: умершие адреса и держатели всех адресов,
        по которым есть отключения, — без них после запуска «2 из 10» выглядело бы как «2 из 2»."""
        rows: dict[str, dict] = {}
        for target in self.dead.keys() | self._suspects:
            entry = self.dead.get(target)
            rows[target] = {
                "holders": sorted(self._holders.get(target, {})),
                "dead": None if entry is None else {
                    "relay_id": entry.relay_id, "relay_name": entry.relay_name, "owner": entry.owner,
                    "node": entry.with_node,
                },
            }
        return rows

    def restore(self, rows: Mapping[str, dict], now: float) -> None:
        """Держатели поднимаются как только что голосовавшие: помнятся ещё час, за который
        релеи успевают ответить заново. Нечитаемая запись пропускается."""
        self.dead, self._holders, self._suspects, self._dead_nodes = {}, {}, set(), set()
        for target, row in rows.items():
            try:
                holders = {int(relay_id): now for relay_id in row["holders"]}
                dead = row.get("dead")
                entry = None if dead is None else DeadEntry(
                    int(dead["relay_id"]), str(dead["relay_name"]),
                    str(dead["owner"]) if dead.get("owner") else None, bool(dead.get("node")),
                )
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
            if holders:
                self._holders[target] = holders
            self._suspects.add(target)
            if entry is not None:
                self.dead[target] = entry
                if entry.with_node and entry.owner:
                    self._dead_nodes.add(entry.owner)

    def _remember_holders(self, voters: Mapping[str, set[int]], now: float) -> None:
        for target, relays in voters.items():
            seen = self._holders.setdefault(target, {})
            for relay_id in relays:
                seen[relay_id] = now
        edge = now - HOLDER_MEMORY_SEC
        for target in list(self._holders):
            seen = self._holders[target]
            for relay_id in [relay_id for relay_id, voted_at in seen.items() if voted_at < edge]:
                del seen[relay_id]
            if not seen:
                del self._holders[target]

    def _die(self, target: str, bad: list[Pair], failing: int, total: int, observe: bool) -> Event:
        first = min(bad, key=lambda pair: (pair.relay_name, pair.relay_id))
        entry = DeadEntry(first.relay_id, first.relay_name, self._owners.get(_ip(target)))
        self.dead[target] = entry
        reasons = {pair.reason_kind for pair in bad}
        return Event(
            EventKind.DEAD, entry.relay_id, entry.relay_name, target, failing=failing, total=total,
            reasons=tuple(kind for kind in ReasonKind if kind in reasons), owner=entry.owner, observe=observe,
        )

    def _owner_of(self, target: str) -> Optional[str]:
        entry = self.dead.get(target)
        return (entry.owner if entry else None) or self._owners.get(_ip(target))

    def _targets_of(self, owners: set[str]) -> dict[str, set[str]]:
        """Нода → её адреса, которые могут умереть: те, у которых есть держатели, и уже умершие."""
        known: dict[str, set[str]] = {owner: set() for owner in owners}
        for target in self._holders.keys() | self.dead.keys():
            owner = self._owner_of(target)
            if owner in known:
                known[owner].add(target)
        return known

    def _with_dead_nodes(self, died: list[Event]) -> list[Event]:
        """Отметить события, после которых у ноды-владельца не работают все её адреса:
        сводка скажет о ноде одной строкой."""
        known = self._targets_of({event.owner for event in died if event.owner})
        marked = []
        for event in died:
            targets = known.get(event.owner or "", set())
            if len(targets) < NODE_MIN_TARGETS or not all(target in self.dead for target in targets):
                marked.append(event)
                continue
            self._dead_nodes.add(event.owner)
            for target in targets:
                self.dead[target].with_node = True
            marked.append(replace(event, node_total=len(targets)))
        return marked

    def _with_revived_nodes(self, revived: list[Event]) -> list[Event]:
        """О возврате ноды целиком говорится, только если она объявлялась умершей целиком:
        у ноды, где умирал один адрес из пяти, «снова работают все адреса» звучало бы как её падение."""
        known = self._targets_of({event.owner for event in revived if event.owner in self._dead_nodes})
        for event in revived:
            # Ожившего адреса среди умерших уже нет, а владельца после перезапуска панели знает только событие
            if event.owner in known:
                known[event.owner].add(event.target)
        back = {owner for owner, targets in known.items() if not any(target in self.dead for target in targets)}
        self._dead_nodes -= back
        return [
            replace(event, node_total=len(known[event.owner]))
            if event.owner in back and len(known[event.owner]) >= NODE_MIN_TARGETS else event
            for event in revived
        ]
