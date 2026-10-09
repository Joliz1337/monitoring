"""Сборка клиентского конфига под выбранное ядро."""
from __future__ import annotations

import json
from typing import Any, Union

from app.services.xray_test.config_builder import singbox, xray
from app.services.xray_test.config_builder.batch import BatchEntry
from app.services.xray_test.models import Core, ProxyEndpoint

__all__ = [
    "BatchEntry", "FORWARD_HOST_TOKEN", "FORWARD_PORT_TOKEN",
    "build_batch", "build_config", "build_forward", "build_forward_template",
]

# Метки шаблона проброса для ноды: сервер выбирается по стране выхода, а она
# известна только после проверки, поэтому адрес и порт подставляет исполнитель
FORWARD_HOST_TOKEN = "__IPERF_HOST__"
FORWARD_PORT_TOKEN = "__IPERF_PORT__"


def build_config(endpoint: ProxyEndpoint, core: Core, socks_port: int) -> dict[str, Any]:
    builder = singbox.build_config if core is Core.SINGBOX else xray.build_config
    return builder(endpoint, socks_port)


def build_forward(
    endpoint: ProxyEndpoint,
    core: Core,
    listen_port: int,
    target_host: str,
    target_port: Union[int, str],
) -> dict[str, Any]:
    """Тот же ключ, но вместо socks — локальный порт, ведущий на target.

    Нужен iperf3: поддержки SOCKS у него нет, и в ключ он попадает только так.
    Исходящий и маршрут остаются от обычной проверки, меняется лишь входящий.
    """
    config = build_config(endpoint, core, listen_port)
    tag = config["inbounds"][0]["tag"]
    if core is Core.SINGBOX:
        config["inbounds"] = [{
            "type": "direct",
            "tag": tag,
            "listen": "127.0.0.1",
            "listen_port": listen_port,
            "network": "tcp",
            "override_address": target_host,
            "override_port": target_port,
        }]
    else:
        # Имена address/port/network — синонимы rewrite*-полей в Xray 26 и
        # единственные в старых версиях, которые может закрепить оператор
        config["inbounds"] = [{
            "tag": tag,
            "listen": "127.0.0.1",
            "port": listen_port,
            "protocol": "dokodemo-door",
            "settings": {"address": target_host, "port": target_port, "network": "tcp"},
        }]
    return config


def build_forward_template(endpoint: ProxyEndpoint, core: Core, listen_port: int) -> str:
    text = json.dumps(build_forward(
        endpoint, core, listen_port, FORWARD_HOST_TOKEN, FORWARD_PORT_TOKEN,
    ))
    # Порт в конфиге — число: метка без кавычек станет им после подстановки
    return text.replace(f'"{FORWARD_PORT_TOKEN}"', FORWARD_PORT_TOKEN)


def build_batch(entries: list[BatchEntry], core: Core) -> dict[str, Any]:
    """Конфиг одного процесса на всю пачку проверок.

    Ядра в пачке одинаковые: Xray и sing-box в один процесс не сложить, поэтому
    группировка по ядру делается до вызова.
    """
    builder = singbox.build_batch if core is Core.SINGBOX else xray.build_batch
    return builder(entries)
