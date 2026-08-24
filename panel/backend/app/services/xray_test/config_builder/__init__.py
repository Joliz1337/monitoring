"""Сборка клиентского конфига под выбранное ядро."""
from __future__ import annotations

from typing import Any

from app.services.xray_test.config_builder import singbox, xray
from app.services.xray_test.config_builder.batch import BatchEntry
from app.services.xray_test.models import Core, ProxyEndpoint

__all__ = ["BatchEntry", "build_config", "build_batch"]


def build_config(endpoint: ProxyEndpoint, core: Core, socks_port: int) -> dict[str, Any]:
    builder = singbox.build_config if core is Core.SINGBOX else xray.build_config
    return builder(endpoint, socks_port)


def build_batch(entries: list[BatchEntry], core: Core) -> dict[str, Any]:
    """Конфиг одного процесса на всю пачку проверок.

    Ядра в пачке одинаковые: Xray и sing-box в один процесс не сложить, поэтому
    группировка по ядру делается до вызова.
    """
    builder = singbox.build_batch if core is Core.SINGBOX else xray.build_batch
    return builder(entries)
