"""Сборка клиентского конфига под выбранное ядро."""
from __future__ import annotations

from typing import Any

from app.services.xray_test.config_builder import singbox, xray
from app.services.xray_test.models import Core, ProxyEndpoint


def build_config(endpoint: ProxyEndpoint, core: Core, socks_port: int) -> dict[str, Any]:
    builder = singbox.build_config if core is Core.SINGBOX else xray.build_config
    return builder(endpoint, socks_port)
