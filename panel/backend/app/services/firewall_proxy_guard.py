"""Защита от потери ноды за SOCKS5-прокси при раскатке firewall-профиля.

Нода за прокси видит адрес прокси, а не панели. Профиль, где порт API ноды открыт
только отдельным адресам без адреса прокси, после apply отрезал бы ноду от панели.
Правило на ноде не подменяем: это ломает сверку хэшей профиля — отказываем в раскатке.
"""

import ipaddress
from typing import Optional
from urllib.parse import urlsplit

from app.models import Server
from app.services.http_client import parse_proxy_input
from app.services.net_utils import host_to_ip

NODE_API_DEFAULT_PORT = 9100
ANY_SOURCE_NAMES = ("", "any", "anywhere")
BROKEN_PROXY_MESSAGE = (
    "Адрес прокси в настройках сервера не разобрать (ожидается ip:port или ip:port@login:pass) — "
    "профиль на этот сервер не применён. Исправьте прокси сервера и повторите синхронизацию."
)


def node_api_port(server_url: str) -> int:
    try:
        return urlsplit(server_url).port or NODE_API_DEFAULT_PORT
    except ValueError:
        return NODE_API_DEFAULT_PORT


def _is_api_allow_rule(rule: dict, port: int) -> bool:
    return (
        int(rule.get("port", 0)) == port
        and (rule.get("protocol") or "tcp").lower() in ("tcp", "any")
        and (rule.get("action") or "allow").lower() == "allow"
        and (rule.get("direction") or "in").lower() == "in"
    )


def _source_covers(from_ip: Optional[str], proxy_ip: Optional[str]) -> bool:
    source = (from_ip or "").strip().lower()
    if source in ANY_SOURCE_NAMES:
        return True
    if proxy_ip is None:
        return False
    try:
        return ipaddress.ip_address(proxy_ip) in ipaddress.ip_network(source, strict=False)
    except ValueError:
        return False


async def proxy_lockout_message(
    server: Server, rules: list[dict], default_incoming: str,
) -> Optional[str]:
    """Текст отказа, если профиль отрежет прокси-ноду от панели; иначе None.

    Отказ только когда у порта API есть allow-правила и все они ограничены
    источниками без адреса прокси. Профиль вовсе без allow на порт API не трогаем —
    это отдельная, уже существующая проверка `node_port_allowed`.
    Прокси-хост, который не удалось привести к IP, считается не покрытым.
    Битое значение прокси — отказ этому серверу, а не исключение: раскатка идёт
    на все ноды разом, и одно такое значение не должно сорвать её остальным.
    """
    if not server.proxy_url or (default_incoming or "deny").lower() == "allow":
        return None

    port = node_api_port(server.url)
    api_rules = [r for r in rules if _is_api_allow_rule(r, port)]
    if not api_rules:
        return None

    try:
        proxy_host = parse_proxy_input(server.proxy_url)[0]
        proxy_ip = await host_to_ip(proxy_host)
    except ValueError:
        return BROKEN_PROXY_MESSAGE
    if any(_source_covers(r.get("from_ip"), proxy_ip) for r in api_rules):
        return None

    return (
        f"Профиль открывает порт API ноды ({port}) только для отдельных адресов, среди которых нет "
        f"прокси панели ({proxy_host}) — нода потеряла бы связь с панелью. "
        f"Добавьте адрес прокси в правило порта {port} или откройте порт для всех."
    )
