"""Посекундный замер скоростей: CPU, сеть, диск.

Фоновая задача раз в секунду снимает `/proc/stat`, `/proc/net/dev` и
`/proc/diskstats`, считает дельту к предыдущей секунде и держит в памяти
последний результат плюс кольцевой буфер за `MAX_WINDOW_SEC`. `/api/metrics`
копирует последний замер (нагрузка за секунду), а по запросу панели с
`?window=N` сводит буфер в средние и пики за N секунд — ровно за промежуток
между её опросами.

Кумулятивные счётчики (байты, пакеты) нода по-прежнему отдаёт сырыми:
учёт трафика и историю по ним ведёт панель. Здесь — только скорости.
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Collection, Iterable, Optional, Sequence

import psutil

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_SEC = 1.0
# Глубина буфера посекундных замеров: интервал опроса панели (до 300 с)
# с запасом на дрейф её цикла
MAX_WINDOW_SEC = 330
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
    # False — проценты унаследованы от прошлого замера (дельта тиков была
    # слишком короткой) или обнулены после смены числа ядер: в сводке за окно
    # такой сэмпл не учитывается, иначе одна секунда весила бы дважды
    cpu_measured: bool = True
    net: dict[str, Pair] = field(default_factory=dict)
    disk: dict[str, Pair] = field(default_factory=dict)
    disk_total: Pair = (0.0, 0.0)


@dataclass(frozen=True)
class WindowSummary:
    """Средние и пики по сэмплам окна.

    `cpu_max` — максимум секундного среднего по ядрам, то есть пик хоста,
    а не самого горячего ядра.
    """

    window_sec: float
    samples: int
    cpu_avg: float
    cpu_max: float
    per_cpu_avg: list[float]
    net_rx_avg: float
    net_tx_avg: float
    net_rx_max: float
    net_tx_max: float
    disk_read_avg: float
    disk_write_avg: float


def weighted_mean(values: Iterable[tuple[float, float]]) -> float:
    """Среднее пар (значение, вес); без веса — 0.0."""
    total = 0.0
    weight_sum = 0.0
    for value, weight in values:
        total += value * weight
        weight_sum += weight
    if weight_sum <= 0:
        return 0.0
    return total / weight_sum


def host_cpu_percent(per_cpu: Sequence[float]) -> float:
    return sum(per_cpu) / len(per_cpu) if per_cpu else 0.0


def _physical_sum(rates: dict[str, Pair], physical_ifaces: Collection[str]) -> Pair:
    physical = [rates[name] for name in physical_ifaces if name in rates]
    return (
        sum(rate[0] for rate in physical),
        sum(rate[1] for rate in physical),
    )


def summarize_window(samples: Sequence[RateSample], physical_ifaces: Collection[str]) -> WindowSummary:
    """Сводка по сэмплам от новейшего к старому (как отдаёт `window()`).

    Сэмплы разной длины неравноправны — пропущенный тик даёт окно в две
    секунды, — поэтому средние взвешены по `window_sec`. CPU считается
    только по измеренным сэмплам с тем же числом ядер, что у новейшего:
    после ресайза VPS старые проценты несопоставимы. Сеть — сумма по
    физическим интерфейсам внутри каждого сэмпла, пик — по этим суммам.
    """
    latest = samples[0]
    core_count = len(latest.per_cpu_percent)
    cpu_samples = [
        sample for sample in samples
        if sample.cpu_measured and len(sample.per_cpu_percent) == core_count
    ]
    host_cpu = [host_cpu_percent(sample.per_cpu_percent) for sample in cpu_samples]
    net_sums = [_physical_sum(sample.net, physical_ifaces) for sample in samples]

    return WindowSummary(
        window_sec=sum(sample.window_sec for sample in samples),
        samples=len(samples),
        cpu_avg=weighted_mean(zip(host_cpu, (s.window_sec for s in cpu_samples))),
        cpu_max=max(host_cpu, default=0.0),
        per_cpu_avg=[
            weighted_mean((s.per_cpu_percent[core], s.window_sec) for s in cpu_samples)
            for core in range(core_count)
        ] if cpu_samples else [],
        net_rx_avg=weighted_mean((rx, s.window_sec) for (rx, _), s in zip(net_sums, samples)),
        net_tx_avg=weighted_mean((tx, s.window_sec) for (_, tx), s in zip(net_sums, samples)),
        net_rx_max=max((rx for rx, _ in net_sums), default=0.0),
        net_tx_max=max((tx for _, tx in net_sums), default=0.0),
        disk_read_avg=weighted_mean((s.disk_total[0], s.window_sec) for s in samples),
        disk_write_avg=weighted_mean((s.disk_total[1], s.window_sec) for s in samples),
    )


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
        self._buffer: deque[RateSample] = deque(maxlen=MAX_WINDOW_SEC)
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

    def window(self, seconds: float) -> Optional[tuple[RateSample, ...]]:
        """Сэмплы от новейшего к старому, пока их окна не покроют `seconds`.

        Покрытие копится по `window_sec` (monotonic-дельты чтений): пропущенный
        тик — это один сэмпл с окном в две секунды, а не дыра. Пока буфер
        короче запрошенного окна, отдаётся всё, что есть. `None` — когда
        протух `snapshot()`: сводка по замолчавшему семплеру не лучше стухшей
        секунды.

        Буфер пополняет `advance()` в потоке event loop, и читать его можно
        только оттуда — deque не переживает мутацию во время обхода.
        """
        if self.snapshot() is None:
            return None
        taken: list[RateSample] = []
        covered = 0.0
        for sample in reversed(self._buffer):
            taken.append(sample)
            covered += sample.window_sec
            if covered >= seconds:
                break
        return tuple(taken)

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
        per_cpu, cpu_measured = self._cpu_percent(prev.cpu_times, current.cpu_times)
        self._sample = RateSample(
            sampled_at=current.wall_time,
            window_sec=dt,
            per_cpu_percent=per_cpu,
            cpu_measured=cpu_measured,
            net=counter_rates(prev.net, current.net, dt),
            disk=disk,
            disk_total=self._disk_total(disk),
        )
        self._buffer.append(self._sample)

    def _cpu_percent(self, before: list, after: list) -> tuple[list[float], bool]:
        """Проценты по ядрам и признак, что они измерены именно в этом окне."""
        percents = per_cpu_percent(before, after)
        if percents is not None:
            return percents, True
        last = self._sample.per_cpu_percent if self._sample else []
        if len(last) == len(after):
            # Слишком короткое окно — отдаём последний валидный замер
            return last, False
        # Число ядер сменилось (ресайз VPS): старые проценты несопоставимы
        return [0.0] * len(after), False

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
