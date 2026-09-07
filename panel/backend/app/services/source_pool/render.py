"""Чистые функции: конфиг для ноды, его хэш и кусок конфига Xray для Remnawave.

Число меток и их номера зашиты константами и совпадают с нодой
(`node/app/models/source_pool.py`): конфиг Xray один на весь парк, а раскладку
меток по своим адресам каждая нода делает сама — по кругу, поэтому 30 меток
делятся ровно на любое число адресов.
"""

import hashlib
import json
from typing import Optional

from app.models import SourcePoolNode
from app.services.exit_proxy.settings import load_json

MARK_COUNT = 30
MARK_BASE = 101
OUTBOUND_TAG_PREFIX = "pool-"
BALANCER_TAG = "source-pool"


def build_node_config(row: Optional[SourcePoolNode]) -> dict:
    """Схема SourcePoolConfig агента."""
    if row is None:
        return {"enabled": False, "excluded": []}
    return {
        "enabled": bool(row.enabled),
        "excluded": sorted(set(load_json(row.excluded, []))),
    }


def config_hash(config: dict) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def xray_outbounds() -> list[dict]:
    return [
        {
            "tag": f"{OUTBOUND_TAG_PREFIX}{index + 1:02d}",
            "protocol": "freedom",
            "settings": {},
            "streamSettings": {"sockopt": {"mark": MARK_BASE + index}},
        }
        for index in range(MARK_COUNT)
    ]


def xray_routing() -> dict:
    return {
        "balancers": [
            {
                "tag": BALANCER_TAG,
                "selector": [OUTBOUND_TAG_PREFIX],
                "strategy": {"type": "roundRobin"},
            }
        ],
        "rule": {"type": "field", "network": "tcp", "balancerTag": BALANCER_TAG},
    }


def xray_snippet() -> dict:
    outbounds = json.dumps(xray_outbounds(), indent=2, ensure_ascii=False)
    routing = json.dumps(xray_routing(), indent=2, ensure_ascii=False)
    text = (
        f"1. В конфиге Xray (Remnawave → Config Profiles) добавьте {MARK_COUNT} outbound'ов в массив \"outbounds\" — "
        "в конец списка, первый (дефолтный) outbound не трогайте. Если у вашего прямого outbound задан "
        "\"domainStrategy\", повторите его в \"settings\" каждого pool-outbound'а.\n\n"
        f"2. В \"routing\" добавьте массив \"balancers\" (или дополните существующий) балансировщиком \"{BALANCER_TAG}\": "
        f"selector [\"{OUTBOUND_TAG_PREFIX}\"] подхватывает все теги с этим префиксом, roundRobin раздаёт соединения по очереди.\n\n"
        "3. Правило с \"balancerTag\" поставьте ПОСЛЕДНИМ в \"routing\".\"rules\" — после всех блокировок и правил "
        "exit-прокси: оно ловит весь оставшийся TCP. UDP (QUIC) под правило не попадает и идёт как шло — "
        "метки на UDP не действуют, а потолок портов касается только TCP.\n\n"
        f"Метки {MARK_BASE}–{MARK_BASE + MARK_COUNT - 1} одинаковы на всех нодах, поэтому кусок конфига общий. "
        "Какой метке какой адрес — решает сама нода: 30 меток раскладываются по её адресам по кругу, "
        "на ноде с одним адресом всё идёт как раньше. Включать пул на нодах — на странице сервера, "
        "«Исходящие адреса»."
    )
    return {"outbounds_json": outbounds, "routing_json": routing, "text": text}
