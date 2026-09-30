"""Алерт о потерях с релея до адресов назначения.

Нода-релей сама проверяет свои бэкенды HAProxy и цели DNAT
(node/app/services/loss_probe.py) и присылает итог в метриках: `loss_probe` —
[{ip, port, loss_pct, rtt_ms, samples}] за последние ~2 минуты. Здесь решается,
когда писать об этом в Telegram:
- потери держатся не ниже порога дольше заданного времени — одно сообщение на
  релей со всеми такими адресами;
- «снизились» — только когда потери упали ниже половины порога: адрес,
  колеблющийся у самого порога, не шлёт сообщение каждую проверку;
- адрес, пропавший из метрик (убрали из конфига, нода ещё не набрала окно),
  забывается молча.
"""

import html
from dataclasses import dataclass, field
from typing import Iterable, Optional

# Нода проверяет адрес раз в 2 с: 30 попыток — минута данных. Доля потерь по
# меньшему числу — шум первых попыток после старта агента или смены конфига
MIN_SAMPLES = 30
RECOVERY_RATIO = 0.5


@dataclass(frozen=True)
class LossReading:
    ip: str
    port: int
    loss_pct: float
    rtt_ms: Optional[float]

    @property
    def key(self) -> str:
        return f"{self.ip}:{self.port}"


@dataclass
class LossEvaluation:
    fired: list[LossReading] = field(default_factory=list)
    recovered: list[LossReading] = field(default_factory=list)


def parse_readings(metrics: dict) -> list[LossReading]:
    """Адреса из метрик ноды с достаточным окном; битые записи пропускаются."""
    readings = []
    for entry in metrics.get("loss_probe") or []:
        try:
            if int(entry.get("samples", 0)) < MIN_SAMPLES:
                continue
            rtt = entry.get("rtt_ms")
            readings.append(LossReading(
                ip=str(entry["ip"]),
                port=int(entry["port"]),
                loss_pct=float(entry["loss_pct"]),
                rtt_ms=None if rtt is None else float(rtt),
            ))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return readings


@dataclass
class LossAlertState:
    """Состояние по адресам одного релея; живёт в памяти алертера."""
    started: dict[str, float] = field(default_factory=dict)
    last_fired: dict[str, float] = field(default_factory=dict)
    alerted: set[str] = field(default_factory=set)

    def reset(self) -> None:
        self.started.clear()
        self.alerted.clear()

    def evaluate(
        self,
        readings: Iterable[LossReading],
        threshold: float,
        sustained_sec: float,
        cooldown_sec: float,
        now: float,
    ) -> LossEvaluation:
        result = LossEvaluation()
        present: set[str] = set()
        for reading in readings:
            key = reading.key
            present.add(key)
            if reading.loss_pct >= threshold:
                started = self.started.setdefault(key, now)
                if key in self.alerted or now - started < sustained_sec:
                    continue
                if now - self.last_fired.get(key, float("-inf")) < cooldown_sec:
                    continue
                self.alerted.add(key)
                self.last_fired[key] = now
                result.fired.append(reading)
                continue
            self.started.pop(key, None)
            if key in self.alerted and reading.loss_pct < threshold * RECOVERY_RATIO:
                self.alerted.discard(key)
                result.recovered.append(reading)

        self.started = {key: ts for key, ts in self.started.items() if key in present}
        self.last_fired = {key: ts for key, ts in self.last_fired.items() if key in present}
        self.alerted &= present
        return result


def _target_line(reading: LossReading, ru: bool) -> str:
    if reading.rtt_ms is None:
        rtt = "нет ответа" if ru else "no reply"
    else:
        rtt = f"{reading.rtt_ms:.0f} {'мс' if ru else 'ms'}"
    return f"• {reading.key} — {reading.loss_pct:g}% ({rtt})"


def alert_message(relay_name: str, readings: list[LossReading], threshold: float,
                  sustained_sec: float, lang: str) -> str:
    ru = lang == "ru"
    name = html.escape(relay_name)
    minutes = max(1, round(sustained_sec / 60))
    lines = "\n".join(_target_line(reading, ru) for reading in readings)
    if ru:
        return f"Потери с {name} выше {threshold:g}% дольше {minutes} мин:\n{lines}"
    return f"Packet loss from {name} above {threshold:g}% for over {minutes} min:\n{lines}"


def recovery_message(relay_name: str, readings: list[LossReading], lang: str) -> str:
    ru = lang == "ru"
    name = html.escape(relay_name)
    lines = "\n".join(_target_line(reading, ru) for reading in readings)
    if ru:
        return f"Потери с {name} снизились:\n{lines}"
    return f"Packet loss from {name} is back to normal:\n{lines}"


def readings_details(readings: list[LossReading]) -> list[dict]:
    return [
        {"ip": r.ip, "port": r.port, "loss_pct": r.loss_pct, "rtt_ms": r.rtt_ms}
        for r in readings
    ]
