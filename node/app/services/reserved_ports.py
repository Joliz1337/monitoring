"""Резервация сервисных портов от эфемерной выдачи ядра.

Профиль vpn опускает пол ip_local_port_range до 1024, и сервисные порты
оказываются внутри эфемерного окна: при рестарте сервиса исходящее соединение
может занять его порт как source-порт, и bind после рестарта не проходит —
ровно так нода теряла свой mTLS-порт при обновлении. Ключ
net.ipv4.ip_local_reserved_ports исключает порты из автоматической выдачи;
явный bind() при этом работает как обычно, поэтому резервация бесплатна.

Сам sysctl считает и применяет рендерер tune-sysctl.sh — единственный владелец
/etc/sysctl.d/99-vless-tuning.conf. Базовые порты (7500 — внутренний uvicorn,
NODE_API_PORT, 2222 — SSH ноды Remnawave) рендерер добавляет сам; агент здесь
только управляет файлом дополнительных портов от панели/оператора и запускает
ре-рендер. Файл переживает ребут, рендерер перечитывает его на каждой загрузке.
"""

from pathlib import Path

from app.config import get_settings

INTERNAL_API_PORT = 7500
REMNAWAVE_SSH_PORT = 2222

# Пишется через write_host_file (каталог смонтирован в контейнер только на
# чтение), читается и рендерером на хосте, и агентом напрямую.
RESERVED_EXTRA_FILE = Path("/opt/monitoring/configs/reserved-ports.conf")

# Текущее значение в ядре. Контейнер агента в сетевом namespace хоста
# (network_mode: host), поэтому это хостовый sysctl — как ip_forward у DNAT.
PROC_RESERVED_PATH = Path("/proc/sys/net/ipv4/ip_local_reserved_ports")

# Потолки совпадают с инвариантом рендерера: файл, забирающий заметную долю
# эфемерного диапазона, оставил бы исходящим соединениям нечего выдавать.
MAX_ENTRIES = 64
MAX_TOTAL_PORTS = 4096


def _parse_entry(raw: str) -> tuple[int, int]:
    """Один токен «порт» или «начало-конец» → (start, end). ValueError на мусор."""
    token = raw.strip()
    if not token:
        raise ValueError("Пустая запись порта")
    start_str, sep, end_str = token.partition("-")
    if not start_str.strip().isdigit() or (sep and not end_str.strip().isdigit()):
        raise ValueError(f"Не порт и не диапазон: {token!r}")
    start = int(start_str)
    end = int(end_str) if sep else start
    if not (1 <= start <= 65535 and 1 <= end <= 65535):
        raise ValueError(f"Порт вне диапазона 1–65535: {token!r}")
    if start > end:
        raise ValueError(f"Начало диапазона больше конца: {token!r}")
    return start, end


def normalize_entries(entries: list[str]) -> list[str]:
    """Проверить и нормализовать список от панели: дедуп, потолки, формат.

    Возвращает записи в каноничном виде ("5201", "8443-8450"), порядок — по
    возрастанию. Слияние пересечений не делается — этим владеет рендерер,
    здесь только защита от мусора и от файла, съедающего весь диапазон.
    """
    if len(entries) > MAX_ENTRIES:
        raise ValueError(f"Слишком много записей: {len(entries)} > {MAX_ENTRIES}")

    parsed = sorted({_parse_entry(raw) for raw in entries})
    total = sum(end - start + 1 for start, end in parsed)
    if total > MAX_TOTAL_PORTS:
        raise ValueError(
            f"Резервируется {total} портов — больше потолка {MAX_TOTAL_PORTS}, "
            "эфемерному диапазону ничего не останется"
        )
    return [
        str(start) if start == end else f"{start}-{end}"
        for start, end in parsed
    ]


def render_extra_file(entries: list[str]) -> str:
    """Содержимое reserved-ports.conf: по записи на строку, читается рендерером."""
    lines = [
        "# Managed by the panel — extra ports excluded from ephemeral allocation.",
        "# The renderer (tune-sysctl.sh) merges this with the base service ports.",
    ]
    lines += entries
    return "\n".join(lines) + "\n"


def read_extra_entries(path: Path = RESERVED_EXTRA_FILE) -> list[str]:
    """Записи из файла доп. портов; битые токены молча пропускаются —
    файл мог править оператор руками, и одна опечатка не должна прятать
    остальные записи из ответа API (рендерер их так же игнорирует)."""
    try:
        content = path.read_text()
    except OSError:
        return []

    entries: list[str] = []
    for line in content.splitlines():
        line = line.split("#", 1)[0]
        for token in line.replace(",", " ").replace(";", " ").split():
            try:
                start, end = _parse_entry(token)
            except ValueError:
                continue
            entries.append(str(start) if start == end else f"{start}-{end}")
    return entries


def base_ports(api_port: int | None = None) -> list[int]:
    """Порты, которые рендерер резервирует всегда, без файла доп. портов."""
    if api_port is None:
        api_port = get_settings().node_api_port
    return sorted({INTERNAL_API_PORT, api_port, REMNAWAVE_SSH_PORT})


def effective_reserved(path: Path = PROC_RESERVED_PATH) -> str | None:
    """Что ядро резервирует прямо сейчас; None — прочитать не удалось."""
    try:
        return path.read_text().strip()
    except OSError:
        return None
