"""Определение того, что агент работает в контейнере.

Ответ решает, идти ли к хосту через nsenter, и должен быть одинаковым во всех
модулях: пока копий было четыре, sshd-менеджер один знал про containerd и
kubepods — на таком хосте он ходил через nsenter, а файрвол и ipset работали
внутри контейнера, где нужных им правил просто нет.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# /.dockerenv кладёт только docker; под containerd и в подах kubernetes его нет,
# а разделитель cgroup остаётся единственным признаком.
CONTAINER_CGROUP_MARKERS = ("docker", "containerd", "kubepods")


def running_in_container() -> bool:
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text()
    except OSError:
        # Файла может не быть (cgroup v2 в ряде окружений) — это не ошибка,
        # просто значит, что признаков контейнера тут не найти
        return False
    return any(marker in cgroup for marker in CONTAINER_CGROUP_MARKERS)
