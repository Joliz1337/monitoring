"""Tests for HAProxy CPU affinity rendering.

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Ради чего тест существует. Первое — отказ по умолчанию: без маркера в конфиге
нода не должна трогать ничего, иначе обновление агента молча переписало бы
рабочие профили на всех нодах. Второе — определение сетевых ядер: их номера
задаёт гипервизор (на боевых нодах встречались и 0,1 и 5,7), поэтому парсинг
/proc/interrupts обязан работать по факту, а не по предположению про CPU0, и
обязан отсеивать virtio-config — привязанный к ядру вектор без трафика. Третье —
идемпотентность: конфиг ходит панель→нода→панель, и повторное применение не
должно накапливать блоки или сохранять привязку после смены тарифа.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import cpu_affinity  # noqa: E402

# Реальный формат: virtio-IRQ не содержат имени интерфейса, только имя устройства.
INTERRUPTS_VIRTIO = """\
 28:          0          0          0          0   PCI-MSIX-0000:00:03.0   0-edge      virtio1-config
 29: 1251313137          0          0          0   PCI-MSIX-0000:00:03.0   1-edge      virtio1-input.0
 30:          0 1449029918          0          0   PCI-MSIX-0000:00:03.0   2-edge      virtio1-output.0
"""

# Тот же хост до появления трафика: счётчики пустые у всех векторов.
INTERRUPTS_COLD = """\
 28:          0          0          0          0   PCI-MSIX-0000:00:03.0   0-edge      virtio1-config
 29:          0          0          0          0   PCI-MSIX-0000:00:03.0   1-edge      virtio1-input.0
 30:          0          0          0          0   PCI-MSIX-0000:00:03.0   2-edge      virtio1-output.0
"""

# Встроенная Intel PCH на физическом сервере: единственный вектор назван просто
# именем интерфейса, без rx/tx/input/output в имени.
INTERRUPTS_E1000E = """\
 16:  982341552          0          0          0   IO-APIC   16-fasteoi   enp0s31f6
"""

BASE_CONFIG = """\
global
    maxconn 1000
    # cpu-affinity (auto)
    tune.bufsize 16384

defaults
    mode tcp
"""


def _fake_reader(interrupts: str, affinity: dict[str, str] | None = None):
    """Подменяет чтение /proc и /sys, чтобы тест не зависел от машины."""
    affinity = affinity or {}

    def read(path: Path) -> str:
        name = str(path).replace("\\", "/")
        if name.endswith("/proc/interrupts"):
            return interrupts
        if name.endswith("/proc/net/route"):
            return "Iface\tDestination\nens3\t00000000\n"
        for irq, cpu in affinity.items():
            if f"/proc/irq/{irq}/effective_affinity_list" in name:
                return cpu
        return ""

    return read


class DetectionTests(unittest.TestCase):
    def test_cpu_taken_from_busiest_counter(self):
        with mock.patch.object(cpu_affinity, "_read", _fake_reader(INTERRUPTS_VIRTIO)), \
             mock.patch.object(cpu_affinity, "_device_name", return_value="virtio1"):
            self.assertEqual(cpu_affinity.detect_network_cpus("ens3"), {0, 1})

    def test_config_vector_never_counts_as_network_cpu(self):
        """virtio1-config привязан к CPU3, но пакетов не несёт: если бы он попал
        в сетевые ядра, HAProxy лишился бы полностью свободного ядра."""
        with mock.patch.object(
            cpu_affinity, "_read",
            _fake_reader(INTERRUPTS_COLD, {"28": "3", "29": "0", "30": "1"}),
        ), mock.patch.object(cpu_affinity, "_device_name", return_value="virtio1"):
            self.assertEqual(cpu_affinity.detect_network_cpus("ens3"), {0, 1})

    def test_cold_node_falls_back_to_affinity(self):
        with mock.patch.object(
            cpu_affinity, "_read",
            _fake_reader(INTERRUPTS_COLD, {"29": "5", "30": "7"}),
        ), mock.patch.object(cpu_affinity, "_device_name", return_value="virtio1"):
            self.assertEqual(cpu_affinity.detect_network_cpus("ens3"), {5, 7})

    def test_vector_named_after_interface_is_detected(self):
        """e1000e и подобные не добавляют rx/tx к имени вектора — требование
        такого суффикса оставляло физические серверы вообще без детекта."""
        with mock.patch.object(cpu_affinity, "_read", _fake_reader(INTERRUPTS_E1000E)), \
             mock.patch.object(cpu_affinity, "_device_name", return_value="0000:00:1f.6"):
            self.assertEqual(cpu_affinity.detect_network_cpus("enp0s31f6"), {0})

    def test_unpinned_vector_is_not_a_network_cpu(self):
        """Диапазон вместо номера означает, что вектор ходит по всем ядрам —
        исключать по нему нечего."""
        with mock.patch.object(
            cpu_affinity, "_read",
            _fake_reader(INTERRUPTS_COLD, {"29": "0-7", "30": "0-7"}),
        ), mock.patch.object(cpu_affinity, "_device_name", return_value="virtio1"):
            self.assertEqual(cpu_affinity.detect_network_cpus("ens3"), set())


class ResolveTests(unittest.TestCase):
    def _resolve(self, value, cpu_count, network=(0, 1), irqbalance=False):
        with mock.patch.object(cpu_affinity, "detect_network_cpus", return_value=set(network)), \
             mock.patch.object(cpu_affinity, "_irqbalance_running", return_value=irqbalance):
            return cpu_affinity.resolve_app_cpus(value, cpu_count)

    def test_auto_excludes_network_cpus(self):
        self.assertEqual(self._resolve("auto", 8), [2, 3, 4, 5, 6, 7])

    def test_manual_list_overrides_detection(self):
        self.assertEqual(self._resolve("5,7", 8, network=(0, 1)), [0, 1, 2, 3, 4, 6])

    def test_manual_range(self):
        self.assertEqual(self._resolve("0-2", 8, network=(0, 1)), [3, 4, 5, 6, 7])

    def test_off_disables(self):
        for value in ("off", "no", ""):
            with self.subTest(value=value):
                self.assertIsNone(self._resolve(value, 8))

    def test_garbage_value_is_ignored(self):
        self.assertIsNone(self._resolve("на все ядра", 8))

    def test_small_host_untouched(self):
        """На двухъядерной ноде два ядра под сеть не оставили бы HAProxy ничего."""
        self.assertIsNone(self._resolve("auto", 2))

    def test_majority_of_cpus_reserved_is_refused(self):
        self.assertIsNone(self._resolve("auto", 4, network=(0, 1, 2)))

    def test_irqbalance_blocks_auto(self):
        self.assertIsNone(self._resolve("auto", 8, irqbalance=True))

    def test_irqbalance_does_not_block_manual(self):
        self.assertEqual(self._resolve("0,1", 8, irqbalance=True), [2, 3, 4, 5, 6, 7])


class RenderTests(unittest.TestCase):
    def _apply(self, content, cpu_count=8, network=(0, 1), enabled=True):
        with mock.patch.object(cpu_affinity, "detect_network_cpus", return_value=set(network)), \
             mock.patch.object(cpu_affinity, "_irqbalance_running", return_value=False), \
             mock.patch.object(cpu_affinity, "is_enabled", return_value=enabled):
            return cpu_affinity.apply(content, cpu_count)

    def test_disabled_setting_changes_nothing(self):
        """Выключенная настройка — состояние по умолчанию на всех нодах."""
        self.assertEqual(self._apply(BASE_CONFIG, enabled=False), BASE_CONFIG)

    def test_disabling_removes_previously_applied_block(self):
        """Иначе выключение оставляло бы HAProxy привязанным навсегда."""
        enabled = self._apply(BASE_CONFIG)
        self.assertIn("cpu-map", enabled)
        self.assertEqual(self._apply(enabled, enabled=False), BASE_CONFIG)

    def test_no_marker_means_no_change(self):
        content = "global\n    maxconn 1000\n\ndefaults\n    mode tcp\n"
        self.assertEqual(self._apply(content), content)

    def test_block_inserted_under_marker(self):
        result = self._apply(BASE_CONFIG)
        self.assertIn("nbthread 6", result)
        self.assertIn("cpu-map 1/1 2", result)
        self.assertIn("cpu-map 1/6 7", result)
        self.assertIn("# cpu-affinity (auto)", result)

    def test_indentation_follows_marker(self):
        for line in self._apply(BASE_CONFIG).splitlines():
            if line.lstrip().startswith(("nbthread", "cpu-map")):
                self.assertTrue(line.startswith("    "), line)

    def test_repeated_apply_is_idempotent(self):
        once = self._apply(BASE_CONFIG)
        self.assertEqual(self._apply(once), once)

    def test_stale_block_recomputed_after_cpu_change(self):
        """Смена тарифа меняет число ядер: старый блок обязан исчезнуть целиком,
        иначе HAProxy останется привязан к ядрам, которых уже нет."""
        eight = self._apply(BASE_CONFIG, cpu_count=8)
        four = self._apply(eight, cpu_count=4)
        self.assertIn("nbthread 2", four)
        self.assertNotIn("nbthread 6", four)
        self.assertEqual(four.count(cpu_affinity.BLOCK_START), 1)

    def test_disabled_marker_removes_previous_block(self):
        enabled = self._apply(BASE_CONFIG)
        disabled = self._apply(enabled.replace("(auto)", "(off)"))
        self.assertNotIn("cpu-map", disabled)
        self.assertNotIn("nbthread", disabled)

    def test_marker_survives_when_detection_fails(self):
        result = self._apply(BASE_CONFIG, network=())
        self.assertIn("# cpu-affinity (auto)", result)
        self.assertNotIn("cpu-map", result)


class StateTests(unittest.TestCase):
    def _with_state(self, text):
        return mock.patch.object(
            cpu_affinity, "_read",
            lambda p: text if str(p).endswith("cpu-affinity.env") else "",
        )

    def test_missing_file_means_disabled(self):
        with mock.patch.object(cpu_affinity, "_read", lambda p: ""):
            self.assertFalse(cpu_affinity.is_enabled())

    def test_enabled_forms(self):
        for value in ("1", "true", "yes", "on", "TRUE"):
            with self.subTest(value=value), self._with_state(f"CPU_AFFINITY_ENABLED={value}\n"):
                self.assertTrue(cpu_affinity.is_enabled())

    def test_disabled_forms(self):
        for value in ("0", "false", "no", "off", ""):
            with self.subTest(value=value), self._with_state(f"CPU_AFFINITY_ENABLED={value}\n"):
                self.assertFalse(cpu_affinity.is_enabled())

    def test_containers_default_when_unset(self):
        with self._with_state("CPU_AFFINITY_ENABLED=1\n"):
            self.assertEqual(cpu_affinity.container_names(), list(cpu_affinity.DEFAULT_CONTAINERS))

    def test_containers_override(self):
        with self._with_state("CPU_AFFINITY_ENABLED=1\nCPU_AFFINITY_CONTAINERS=a, b ,c\n"):
            self.assertEqual(cpu_affinity.container_names(), ["a", "b", "c"])

    def test_render_state_roundtrip(self):
        rendered = cpu_affinity.render_state(True, ["x"])
        with self._with_state(rendered):
            self.assertTrue(cpu_affinity.is_enabled())
            self.assertEqual(cpu_affinity.container_names(), ["x"])

    def test_to_cpuset(self):
        self.assertEqual(cpu_affinity.to_cpuset([0, 1, 2, 4, 6]), "0,1,2,4,6")


class _FakeResult:
    def __init__(self, success=True, stdout="", stderr=""):
        self.success = success
        self.stdout = stdout
        self.stderr = stderr


class _FakeExecutor:
    """Возвращает cpuset по имени контейнера и записывает выполненные update."""

    def __init__(self, cpusets):
        self.cpusets = cpusets
        self.updates = []

    async def execute(self, cmd, timeout=None):
        if "docker inspect" in cmd:
            name = cmd.split()[-1].strip("'\"")
            if name not in self.cpusets:
                return _FakeResult(success=False)
            return _FakeResult(stdout=self.cpusets[name])
        if "docker update" in cmd:
            self.updates.append(cmd)
            return _FakeResult()
        return _FakeResult(success=False)


class ContainerTests(unittest.IsolatedAsyncioTestCase):
    def _patches(self, enabled=True, network=(0, 1)):
        return (
            mock.patch.object(cpu_affinity, "is_enabled", return_value=enabled),
            mock.patch.object(cpu_affinity, "detect_network_cpus", return_value=set(network)),
            mock.patch.object(cpu_affinity, "_irqbalance_running", return_value=False),
            mock.patch.object(cpu_affinity, "container_names", return_value=["remnanode"]),
        )

    async def test_pins_container_when_cpuset_differs(self):
        ex = _FakeExecutor({"remnanode": ""})
        with self._patches()[0], self._patches()[1], self._patches()[2], self._patches()[3]:
            changed = await cpu_affinity.sync_containers(ex, 8)
        self.assertEqual(changed, {"remnanode": "2,3,4,5,6,7"})
        self.assertEqual(len(ex.updates), 1)

    async def test_no_update_when_already_correct(self):
        """Проверка идёт по таймеру — лишний docker update каждые 5 минут не нужен."""
        ex = _FakeExecutor({"remnanode": "2,3,4,5,6,7"})
        with self._patches()[0], self._patches()[1], self._patches()[2], self._patches()[3]:
            changed = await cpu_affinity.sync_containers(ex, 8)
        self.assertEqual(changed, {})
        self.assertEqual(ex.updates, [])

    async def test_missing_container_is_skipped(self):
        """На ноде без Remnawave контейнера просто нет — это не ошибка."""
        ex = _FakeExecutor({})
        with self._patches()[0], self._patches()[1], self._patches()[2], self._patches()[3]:
            changed = await cpu_affinity.sync_containers(ex, 8)
        self.assertEqual(changed, {})
        self.assertEqual(ex.updates, [])

    async def test_disabled_setting_touches_nothing(self):
        ex = _FakeExecutor({"remnanode": ""})
        p = self._patches(enabled=False)
        with p[0], p[1], p[2], p[3]:
            changed = await cpu_affinity.sync_containers(ex, 8)
        self.assertEqual(changed, {})
        self.assertEqual(ex.updates, [])

    async def test_reset_unpins_only_pinned(self):
        ex = _FakeExecutor({"remnanode": "2,3,4,5,6,7"})
        with mock.patch.object(cpu_affinity, "container_names", return_value=["remnanode"]):
            changed = await cpu_affinity.reset_containers(ex)
        self.assertEqual(changed, {"remnanode": ""})
        self.assertIn("--cpuset-cpus ''", ex.updates[0])


if __name__ == "__main__":
    unittest.main()
