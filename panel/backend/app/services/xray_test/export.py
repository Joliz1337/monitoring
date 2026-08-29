"""Выгрузка результатов: рабочие ключи и отчёт.

Смысл раздела не в том, чтобы посмотреть таблицу, а в том, чтобы из подписки на
двести ключей забрать те, что работают. Ссылки берутся исходные — пересобирать
их из модели значило бы рисковать потерей параметров, которых мы не поняли.
"""
from __future__ import annotations

import csv
import io
import json
from base64 import b64encode
from typing import Iterable

from app.services.xray_test.models import Verdict

REPORT_COLUMNS = (
    ("remark", "Название"),
    ("protocol", "Протокол"),
    ("address", "Адрес"),
    ("port", "Порт"),
    ("sni", "SNI"),
    ("transport", "Транспорт"),
    ("security", "Шифрование"),
    ("core", "Ядро"),
    ("verdict", "Итог"),
    ("reason", "Причина"),
    ("tcp_min_ms", "TCP, мс"),
    ("handshake_ms", "Подключение, мс"),
    ("rtt_ms", "Задержка, мс"),
    ("speed_mbps", "Скорость, Мбит/с"),
    ("exit_ip", "Выходной IP"),
    ("exit_country", "Страна"),
    ("http_status", "HTTP"),
)

WORKING_VERDICTS = frozenset({Verdict.OK.value, Verdict.DEGRADED.value})


def working_links(results: Iterable[dict], *, include_degraded: bool = True) -> list[str]:
    allowed = WORKING_VERDICTS if include_degraded else {Verdict.OK.value}
    seen: set[str] = set()
    links: list[str] = []
    for item in results:
        link = item.get("link")
        if not link or item.get("verdict") not in allowed or link in seen:
            continue
        seen.add(link)
        links.append(link)
    return links


def as_subscription(links: list[str]) -> str:
    """Готовая base64-подписка из рабочих ключей — её можно скормить клиенту."""
    return b64encode("\n".join(links).encode("utf-8")).decode("ascii")


def as_csv(results: Iterable[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\n")
    writer.writerow([title for _, title in REPORT_COLUMNS])
    for item in results:
        writer.writerow([_cell(item.get(key)) for key, _ in REPORT_COLUMNS])
    return buffer.getvalue()


def as_json(results: Iterable[dict]) -> str:
    return json.dumps(list(results), ensure_ascii=False, indent=2)


def _cell(value: object) -> str:
    if value is None:
        return ""
    return str(value)
