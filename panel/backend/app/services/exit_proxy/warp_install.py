"""Установка Cloudflare WARP на ноду через агента: после неё WARP появляется в пуле выходов."""

import logging

from app.services.remnawave_node_install import HostInstallJobManager

logger = logging.getLogger(__name__)


async def _recheck_exit(server_id: int) -> None:
    from app.services.exit_proxy.service import get_exit_proxy_service

    try:
        await get_exit_proxy_service().check_now(server_id)
    except Exception as exc:  # noqa: BLE001 — установка удалась, проверка подхватится циклом
        logger.warning("Exit proxy re-check after WARP install on server %s failed: %s", server_id, exc)


_manager = HostInstallJobManager(
    start_message="Устанавливаю Cloudflare WARP на «{name}» через агента…",
    on_success=_recheck_exit,
)


def get_warp_install_manager() -> HostInstallJobManager:
    return _manager
