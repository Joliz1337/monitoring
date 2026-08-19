"""Посекундный замер скоростей: CPU, сеть, диск.

Фоновая задача раз в секунду снимает `/proc/stat`, `/proc/net/dev` и
`/proc/diskstats`, считает дельту к предыдущей секунде и держит в памяти
только последний результат. `/api/metrics` его копирует — панель получает
нагрузку за последнюю секунду, а не среднее за свой интервал опроса.

Кумулятивные счётчики (байты, пакеты) нода по-прежнему отдаёт сырыми:
учёт трафика и историю по ним ведёт панель. Здесь — только скорости.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import psutil

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_SEC = 1.0
# Если фоновый замер замолчал, нода лучше не отдаст скорость вовсе, чем
# будет отдавать одну и ту же стухшую цифру
STALE_AFTER_SEC = 5.0
# Минимум натиканного счётчиками /proc/stat, при котором замер осмыслен.
# Тик ядра — 10 мс, и на дельте в единицы тиков доля busy вырождается
# в 0 или 100 («одно ядро 100%, остальные 0»).
CPU_MIN_TICKS_SECONDS = 0.2
# Два чтения ближе этого по часам не образуют окна — см. advance()
MIN_WINDOW_SEC = 0.2
# Длительность стартового замера — с запасом над порогами выше
CPU_PRIME_SECONDS = 0.3

Pair = tuple[float, float]
Counters = dict[str, tuple[int, int]]


@dataclass(frozen=True)
class RawCounters:
    """Одно чтение счётчиков хоста."""

    taken_at: float
    wall_time: float
    cpu_times: list
    net: Counters
    disk: Counters


@dataclass(frozen=True)
class RateSample:
    """Скорости за окно между двумя последними чтениями."""

    sampled_at: float
    window_sec: float
    per_cpu_percent: list[float]
    net: dict[str, Pair] = field(default_factory=dict)
    disk: dict[str, Pair] = field(default_factory=dict)
    disk_total: Pair = (0.0, 0.0)


def per_cpu_percent(before: list, after: list) -> Optional[list[float]]:
    """Занятость каждого ядра по дельте счётчиков, либо None при слишком
    короткой дельте — тогда считать нечего и звать не за чем."""
    if not after or len(after) != len(before):
        return None

    percents = []
    for prev, cur in zip(before, after):
        total = sum(cur) - sum(prev)
        if total < CPU_MIN_TICKS_SECONDS:
            return None
        busy = total - (cur.idle - prev.idle)
        percents.append(round(min(max(busy, 0.0) / total * 100, 100.0), 1))
    return percents


def counter_rates(before: Counters, after: Counters, dt: float) -> dict[str, Pair]:
    """Байт/с по каждому ключу, который есть в обоих чтениях.

    Счётчик «назад» (сброс драйвера, переподнятый интерфейс) даёт ноль,
    а не отрицательную скорость.
    """
    rates: dict[str, Pair] = {}
    for name, (cur_a, cur_b) in after.items():
        prev = before.get(name)
        if prev is None:
            continue
        rates[name] = (max(cur_a - prev[0], 0) / dt, max(cur_b - prev[1], 0) / dt)
    return rates


def read_net_dev() -> dict[str, dict[str, int]]:
    """Счётчики интерфейсов из /proc/net/dev (network_mode: host — это хост)."""
    result: dict[str, dict[str, int]] = {}
    try:
        content = Path("/proc/net/dev").read_text()
    except OSError as e:
        logger.warning(f"Failed to read /proc/net/dev, network counters will be zero: {e}")
        return result

    for line in content.split('\n')[2:]:
        if ':' not in line:
            continue
        iface, _, rest = line.partition(':')
        iface = iface.strip()
        if iface == 'lo':
            continue
        values = rest.split()
        if len(values) < 16:
            continue
        try:
            result[iface] = {
                'rx_bytes': int(values[0]),
                'rx_packets': int(values[1]),
                'rx_errors': int(values[2]),
                'rx_drops': int(values[3]),
                'tx_bytes': int(values[8]),
                'tx_packets': int(values[9]),
                'tx_errors': int(values[10]),
                'tx_drops': int(values[11]),
            }
        except ValueError:
            continue
    return result


def read_host_counters() -> RawCounters:
    net = {name: (io['rx_bytes'], io['tx_bytes']) for name, io in read_net_dev().items()}
    disk_io = psutil.disk_io_counters(perdisk=True) or {}
    disk = {name: (c.read_bytes, c.write_bytes) for name, c in disk_io.items()}
    return RawCounters(
        taken_at=time.monotonic(),
        wall_time=time.time(),
        cpu_times=psutil.cpu_times(percpu=True),
        net=net,
        disk=disk,
    )


def is_whole_disk(name: str) -> bool:
    """sda/nvme0n1 — да, sda1/nvme0n1p1 — нет (та же проверка, что у psutil
    для `disk_io_counters(perdisk=False)`): байты раздела уже учтены в диске."""
    return Path("/sys/block", name.replace('/', '!')).exists()


class RateSampler:
    """Тикает раз в секунду и хранит последний замер скоростей."""

    def __init__(
        self,
        read_counters: Callable[[], RawCounters] = read_host_counters,
        is_whole_disk: Callable[[str], bool] = is_whole_disk,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read_counters = read_counters
        self._is_whole_disk = is_whole_disk
        self._clock = clock
        self._prev: Optional[RawCounters] = None
        self._sample: Optional[RateSample] = None
        self._task: Optional[asyncio.Task] = None

    def prime(self) -> None:
        """Стартовый блокирующий замер: первый же запрос метрик получает
        реальные значения, а не нули пустого baseline."""
        self.advance(self._read_counters())
        time.sleep(CPU_PRIME_SECONDS)
        self.advance(self._read_counters())

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._sampling_loop())
        logger.info("Rate sampler started, interval %ss", SAMPLE_INTERVAL_SEC)

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("Rate sampler stopped")

    def snapshot(self) -> Optional[RateSample]:
        """Последний замер, либо None, пока его нет или он протух.

        Вызывается из сбора метрик в тред-пуле — синхронно и без I/O; ссылка
        на неизменяемый сэмпл подменяется целиком, лок не нужен.
        """
        sample, prev = self._sample, self._prev
        if sample is None or prev is None:
            return None
        if self._clock() - prev.taken_at > STALE_AFTER_SEC:
            return None
        return sample

    def advance(self, current: RawCounters) -> None:
        prev = self._prev
        if prev is None:
            self._prev = current
            return

        dt = current.taken_at - prev.taken_at
        if dt < MIN_WINDOW_SEC:
            # Горстка тиков и пакетов — не скорость, а шум. Baseline не двигаем:
            # следующий тик померит от него же, уже на полном окне.
            return

        self._prev = current
        disk = counter_rates(prev.disk, current.disk, dt)
        self._sample = RateSample(
            sampled_at=current.wall_time,
            window_sec=dt,
            per_cpu_percent=self._cpu_percent(prev.cpu_times, current.cpu_times),
            net=counter_rates(prev.net, current.net, dt),
            disk=disk,
            disk_total=self._disk_total(disk),
        )

    def _cpu_percent(self, before: list, after: list) -> list[float]:
        percents = per_cpu_percent(before, after)
        if percents is not None:
            return percents
        last = self._sample.per_cpu_percent if self._sample else []
        if len(last) == len(after):
            # Слишком короткое окно — отдаём последний валидный замер
            return last
        # Число ядер сменилось (ресайз VPS): старые проценты несопоставимы
        return [0.0] * len(after)

    def _disk_total(self, disk: dict[str, Pair]) -> Pair:
        whole = [rate for name, rate in disk.items() if self._is_whole_disk(name)]
        if not whole:
            whole = list(disk.values())
        return (
            sum(rate[0] for rate in whole),
            sum(rate[1] for rate in whole),
        )

    async def _sampling_loop(self) -> None:
        while True:
            await asyncio.sleep(SAMPLE_INTERVAL_SEC)
            try:
                self.advance(await asyncio.to_thread(self._read_counters))
            except Exception as e:
                logger.warning(f"Rate sample failed: {e}")


_sampler: Optional[RateSampler] = None


def get_rate_sampler() -> RateSampler:
    global _sampler
    if _sampler is None:
        _sampler = RateSampler()
    return _sampler
