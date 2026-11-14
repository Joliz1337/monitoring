import asyncio
import hashlib
import html
import json
import logging
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import docker
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.database import async_session
from app.models import (
    WildcardCertificate, ServerWildcardCertificate, Server, PanelSettings, AlertSettings,
)
from app.services.http_client import get_node_client, node_auth_headers
from app.services.node_capabilities import Capability, server_allows
from app.services.server_status import get_offline_threshold, resolve_status

logger = logging.getLogger(__name__)

DEFAULT_DEPLOY_PATH = "/etc/letsencrypt/live"
PANEL_NGINX_CONTAINER = "panel-nginx"
LETSENCRYPT_LIVE = Path("/etc/letsencrypt/live")
LETSENCRYPT_RENEWAL = Path("/etc/letsencrypt/renewal")
USE_FOR_PANEL_SETTING = "wildcard_use_for_panel"
AUTO_RENEW_SETTING = "wildcard_auto_renew_enabled"
# JSON «отпечаток → id серверов»: раскатки, о провале которых оператор уже знает
FAILED_DEPLOYS_SETTING = "wildcard_failed_deploys"

STARTUP_DELAY = 120
RECONCILE_INTERVAL = 3600
# Сроки сертификатов проверяются раз в сутки — на каждом 24-м проходе сверки
RECONCILES_PER_RENEW_CHECK = 24
DEPLOY_CONCURRENCY = 30

# Лимит Telegram — 4096 символов вместе с заголовком и экранированием
NOTIFY_TEXT_CHARS = 3500
FAILURE_LIST_CHARS = 3000
FAILURE_REASON_CHARS = 200

NO_CERTIFICATES_MESSAGE = "No certificates selected for this server"
CUSTOM_PATHS_SINGLE_CERT_MESSAGE = (
    "Custom paths hold a single certificate: select only one for this server"
)


def wildcard_covers_domain(base_domain: str, domain: str) -> bool:
    """Покрывает ли сертификат (*.base + base) указанный домен.

    Wildcard действует ровно на один уровень: *.example.com покрывает
    panel.example.com, но не a.b.example.com.
    """
    if not base_domain or not domain:
        return False
    if domain == base_domain:
        return True
    suffix = f".{base_domain}"
    return domain.endswith(suffix) and "." not in domain[: -len(suffix)]


def uses_custom_paths(server: Server) -> bool:
    return bool(
        server.wildcard_ssl_custom_path_enabled
        and server.wildcard_ssl_custom_fullchain_path
        and server.wildcard_ssl_custom_privkey_path
    )


def deploy_target(server: Server, cert: WildcardCertificate) -> dict:
    """Куда на сервере ложится сертификат — поля запроса к ноде."""
    if uses_custom_paths(server):
        return {
            "custom_fullchain_path": server.wildcard_ssl_custom_fullchain_path,
            "custom_privkey_path": server.wildcard_ssl_custom_privkey_path,
        }
    base_path = server.wildcard_ssl_deploy_path or DEFAULT_DEPLOY_PATH
    target = {"deploy_path": f"{base_path.rstrip('/')}/{cert.base_domain}"}
    if server.wildcard_ssl_fullchain_name:
        target["fullchain_filename"] = server.wildcard_ssl_fullchain_name
    if server.wildcard_ssl_privkey_name:
        target["privkey_filename"] = server.wildcard_ssl_privkey_name
    return target


def deployment_fingerprint(server: Server, certs: list[WildcardCertificate]) -> str:
    """Отпечаток того, что раскатка кладёт на сервер: цепочки, пути и reload-команда.

    Продление сертификата и смена настроек сервера дают другой отпечаток — по
    расхождению с сохранённым сверка понимает, что сервер надо раскатать заново.
    """
    state = {
        "reload_command": server.wildcard_ssl_reload_cmd or "",
        "certificates": [
            {**deploy_target(server, cert), "fullchain_pem": cert.fullchain_pem}
            for cert in sorted(certs, key=lambda c: c.id)
        ],
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def certificate_expired(cert: WildcardCertificate, now: datetime) -> bool:
    expiry = cert.expiry_date
    if not expiry:
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry <= now


@dataclass(frozen=True)
class ReconcileState:
    offline_threshold: int
    alerts: Optional[AlertSettings]
    # id сервера → отпечаток, раскатка которого уже падала
    failed: dict[int, str]


async def linked_certificates(db, server_ids: list[int]) -> dict[int, list[WildcardCertificate]]:
    """Закреплённые за серверами сертификаты: server_id → список в порядке выпуска."""
    if not server_ids:
        return {}
    # Связи и сертификаты отдельными запросами: JOIN тянул бы PEM на каждую пару
    # сервер–сертификат, на сотнях нод это мегабайты одинаковых строк
    links = (await db.execute(
        select(ServerWildcardCertificate.server_id, ServerWildcardCertificate.certificate_id)
        .where(ServerWildcardCertificate.server_id.in_(server_ids))
        .order_by(ServerWildcardCertificate.certificate_id)
    )).all()
    if not links:
        return {}
    certs = {
        c.id: c for c in (await db.execute(
            select(WildcardCertificate).where(WildcardCertificate.id.in_({cert_id for _, cert_id in links}))
        )).scalars().all()
    }
    by_server: dict[int, list[WildcardCertificate]] = defaultdict(list)
    for server_id, cert_id in links:
        by_server[server_id].append(certs[cert_id])
    return by_server


async def replace_certificate_links(db, server_ids: list[int], cert_ids: list[int]) -> None:
    """Задать серверам ровно этот набор сертификатов (несуществующие id пропускаются)."""
    known = list((await db.execute(
        select(WildcardCertificate.id).where(WildcardCertificate.id.in_(cert_ids))
    )).scalars().all()) if cert_ids else []
    await db.execute(
        delete(ServerWildcardCertificate).where(ServerWildcardCertificate.server_id.in_(server_ids))
    )
    if known:
        await db.execute(
            insert(ServerWildcardCertificate),
            [{"server_id": s, "certificate_id": c} for s in server_ids for c in known],
        )

# Статус выпуска/продления (in-memory, для polling)
_issue_status = {
    "in_progress": False,
    "last_result": None,
    "last_error": None,
    "output": None,
    "started_at": None,
    "completed_at": None,
}


class WildcardSSLManager:

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ─── Lifecycle ───

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Wildcard SSL manager started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Wildcard SSL manager stopped")

    async def _loop(self):
        await asyncio.sleep(STARTUP_DELAY)
        passes = 0
        while self._running:
            await self._run_pass(renew=passes % RECONCILES_PER_RENEW_CHECK == 0)
            passes += 1
            await asyncio.sleep(RECONCILE_INTERVAL)

    async def _run_pass(self, renew: bool) -> None:
        if renew:
            try:
                await self._check_and_renew()
            except Exception as e:
                logger.error(f"Wildcard SSL auto-renew error: {e}")
        try:
            await self.reconcile_deployments(retry_failed=renew)
        except Exception as e:
            logger.error(f"Wildcard SSL deploy reconcile error: {e}")

    # ─── Issue ───

    async def issue_certificate(self, base_domain: str, email: str, cf_token: str) -> tuple[bool, str]:
        _issue_status["in_progress"] = True
        _issue_status["last_error"] = None
        _issue_status["output"] = None
        _issue_status["started_at"] = datetime.now(timezone.utc).isoformat()
        _issue_status["completed_at"] = None

        try:
            result = await self._run_certbot(base_domain, email, cf_token, force_renewal=False)
            if not result[0]:
                _issue_status["last_result"] = "failed"
                _issue_status["last_error"] = result[1]
                return result

            fullchain, privkey, expiry = await self._read_cert_files(base_domain)
            if not fullchain:
                _issue_status["last_result"] = "failed"
                _issue_status["last_error"] = "Certificate files not found after issuance"
                return False, "Certificate files not found after issuance"

            async with async_session() as db:
                # Повторный выпуск домена обновляет запись на месте: id и закреплённые
                # за сертификатом серверы сохраняются. Новый сертификат ни за кем не
                # закрепляется — серверы для него выбирает пользователь
                cert = (await db.execute(
                    select(WildcardCertificate).where(WildcardCertificate.base_domain == base_domain)
                )).scalars().first()
                if cert is None:
                    cert = WildcardCertificate(domain=f"*.{base_domain}", base_domain=base_domain)
                    db.add(cert)
                cert.fullchain_pem = fullchain
                cert.privkey_pem = privkey
                cert.expiry_date = expiry
                cert.issued_at = datetime.now(timezone.utc)
                cert.last_renewed = None
                await db.commit()

            _issue_status["last_result"] = "success"
            _issue_status["output"] = result[1]
            logger.info(f"Wildcard certificate issued for *.{base_domain}")
            await self._apply_to_panel_if_enabled(base_domain)
            return True, f"Certificate issued for *.{base_domain}"

        except Exception as e:
            logger.error(f"Certificate issuance failed: {e}")
            _issue_status["last_result"] = "failed"
            _issue_status["last_error"] = str(e)
            return False, str(e)
        finally:
            _issue_status["in_progress"] = False
            _issue_status["completed_at"] = datetime.now(timezone.utc).isoformat()

    # ─── Renew ───

    async def renew_certificate(self, cert_id: int) -> tuple[bool, str]:
        async with async_session() as db:
            cert = (await db.execute(
                select(WildcardCertificate).where(WildcardCertificate.id == cert_id)
            )).scalar_one_or_none()
            if not cert:
                return False, "Certificate not found"

            cf_token = await self._get_setting(db, "wildcard_cloudflare_api_token")
            email = await self._get_setting(db, "wildcard_email")
            if not cf_token:
                return False, "Cloudflare API token not configured"

            old_expiry = cert.expiry_date
            if old_expiry and old_expiry.tzinfo is None:
                old_expiry = old_expiry.replace(tzinfo=timezone.utc)

            # Целимся в реальную линию certbot: после сбоев её имя может быть
            # base_domain-000X. Продление по точному имени идёт «на месте» и не
            # плодит новые дубли (forking случается, когда named-линию не загрузить).
            lineage = await self._find_cert_lineage(cert.base_domain)
            cert_name = lineage[0] if lineage else cert.base_domain

            result = await self._run_certbot(
                cert.base_domain, email or "", cf_token, force_renewal=True, cert_name=cert_name
            )
            if not result[0]:
                return result

            fullchain, privkey, expiry = await self._read_cert_files(cert.base_domain)
            if not fullchain:
                return False, "Certificate files not found after renewal"

            # certbot вернул 0, но срок не сдвинулся → линия повреждена и сертификат
            # не обновился. Не выдаём это за успех — иначе панель тихо показывает старый.
            if old_expiry and expiry and expiry <= old_expiry:
                logger.error(
                    f"Renewal of *.{cert.base_domain} did not advance expiry "
                    f"(old={old_expiry.isoformat()}, new={expiry.isoformat()}); "
                    f"certbot lineage '{cert_name}' likely broken"
                )
                return False, "Сертификат не продлился: срок не сдвинулся (повреждённая certbot-линия)"

            cert.fullchain_pem = fullchain
            cert.privkey_pem = privkey
            cert.expiry_date = expiry
            cert.last_renewed = datetime.now(timezone.utc)
            await db.commit()

            logger.info(
                f"Wildcard certificate renewed for *.{cert.base_domain} "
                f"(expires {expiry.isoformat() if expiry else '?'})"
            )

        await self._apply_to_panel_if_enabled(cert.base_domain)
        return True, f"Certificate renewed for *.{cert.base_domain}"

    # ─── Deploy ───

    async def deploy_to_server(self, server_id: int) -> dict:
        async with async_session() as db:
            server = (await db.execute(
                select(Server).where(Server.id == server_id)
            )).scalar_one_or_none()
            if not server:
                return {"success": False, "message": "Server not found", "server_id": server_id}

            if not server_allows(server, Capability.SSL, write=True):
                return {"success": False, "message": "Node restricts SSL section",
                        "server_id": server.id, "server_name": server.name}

            linked = (await linked_certificates(db, [server.id])).get(server.id, [])

        return await self.deploy_to_node(server, linked)

    async def get_deploy_targets(
        self, server_ids: Optional[list[int]] = None, cert_id: Optional[int] = None
    ) -> list[tuple[Server, list[WildcardCertificate]]]:
        """Серверы для раскатки и закреплённые за каждым сертификаты.

        server_ids — явный выбор (он и есть согласие, wildcard_ssl_enabled не нужен),
        None — все включённые. cert_id оставляет только серверы с этим сертификатом.
        """
        async with async_session() as db:
            query = select(Server).where(Server.is_active == True)
            if server_ids is None:
                query = query.where(Server.wildcard_ssl_enabled == True)
            else:
                query = query.where(Server.id.in_(server_ids))
            servers = [
                s for s in (await db.execute(query.order_by(Server.position))).scalars().all()
                if server_allows(s, Capability.SSL, write=True)
            ]
            linked = await linked_certificates(db, [s.id for s in servers])

        targets = [(s, linked.get(s.id, [])) for s in servers]
        if cert_id is None:
            return targets
        return [(s, certs) for s, certs in targets if any(c.id == cert_id for c in certs)]

    async def reconcile_deployments(self, retry_failed: bool = False) -> None:
        """Довезти сертификаты на включённые серверы, где лежит не то, что должно.

        Сервер без связи молча ждёт следующего прохода. Упавшую раскатку повторяем
        только с retry_failed: каждая попытка на ноде — это бэкап, запись файлов и
        reload-команда, раз в час это уже вмешательство в работу сервера.
        """
        state = await self._load_reconcile_state()
        if state is None:
            return

        now = datetime.now(timezone.utc)
        still_failed: dict[int, str] = {}
        queue: list[tuple[Server, list[WildcardCertificate], str]] = []
        offline = 0
        for server, linked in await self.get_deploy_targets():
            certs = [c for c in linked if not certificate_expired(c, now)]
            if not certs:
                continue
            fingerprint = deployment_fingerprint(server, certs)
            if server.wildcard_ssl_deployed_fingerprint == fingerprint:
                continue
            if state.failed.get(server.id) == fingerprint:
                still_failed[server.id] = fingerprint
                if not retry_failed:
                    continue
            if resolve_status(server, state.offline_threshold) != "online":
                offline += 1
                continue
            queue.append((server, certs, fingerprint))

        # Bounded fan-out: на сотнях нод не открываем разом сотни TLS-соединений.
        semaphore = asyncio.Semaphore(DEPLOY_CONCURRENCY)

        async def deploy(server: Server, certs: list[WildcardCertificate]) -> dict:
            async with semaphore:
                try:
                    return await self.deploy_to_node(server, certs)
                except Exception as e:  # noqa: BLE001 — одна нода не должна срывать проход по остальным
                    return {
                        "server_id": server.id, "server_name": server.name,
                        "success": False, "message": str(e) or type(e).__name__,
                    }

        results = await asyncio.gather(*(deploy(server, certs) for server, certs, _ in queue))

        new_failures: list[dict] = []
        for (server, certs, fingerprint), result in zip(queue, results):
            if result["success"]:
                still_failed.pop(server.id, None)
                continue
            if server.id not in still_failed:
                new_failures.append({**result, "domains": ", ".join(c.domain for c in certs)})
            still_failed[server.id] = fingerprint

        if still_failed != state.failed:
            await self._save_failed_deploys(still_failed)
        if queue or offline:
            deployed = sum(1 for result in results if result["success"])
            logger.info(
                f"Wildcard deploy reconcile: {deployed} deployed, {len(queue) - deployed} failed, "
                f"{offline} offline nodes waiting"
            )
        if new_failures:
            logger.error(
                f"Wildcard deploy failed on {len(new_failures)} of {len(queue)} nodes:\n"
                + self._failure_lines(new_failures)
            )
            await self._notify(
                state.alerts, self._msg_deploy_failed(state.alerts, new_failures, len(queue))
            )

    async def _load_reconcile_state(self) -> Optional[ReconcileState]:
        """None — автопродление выключено: без него панель сама ничего не раскатывает."""
        async with async_session() as db:
            if await self._get_setting(db, AUTO_RENEW_SETTING) != "true":
                return None
            return ReconcileState(
                offline_threshold=await get_offline_threshold(db),
                alerts=(await db.execute(select(AlertSettings).limit(1))).scalar_one_or_none(),
                failed=self._parse_failed_deploys(await self._get_setting(db, FAILED_DEPLOYS_SETTING)),
            )

    @staticmethod
    def _parse_failed_deploys(raw: Optional[str]) -> dict[int, str]:
        try:
            stored = json.loads(raw or "{}")
            return {
                int(server_id): fingerprint
                for fingerprint, server_ids in stored.items()
                for server_id in server_ids
            }
        except (ValueError, AttributeError, TypeError):
            return {}

    @staticmethod
    def _dump_failed_deploys(failed: dict[int, str]) -> str:
        # Ключом — отпечаток: у серверов с одинаковыми настройками он общий, и значение
        # не растёт на 64 символа с каждой нодой (его целиком отдаёт GET /api/settings)
        by_fingerprint: dict[str, list[int]] = defaultdict(list)
        for server_id, fingerprint in failed.items():
            by_fingerprint[fingerprint].append(server_id)
        return json.dumps(by_fingerprint)

    async def _save_failed_deploys(self, failed: dict[int, str]) -> None:
        value = self._dump_failed_deploys(failed)
        async with async_session() as db:
            row = (await db.execute(
                select(PanelSettings).where(PanelSettings.key == FAILED_DEPLOYS_SETTING)
            )).scalar_one_or_none()
            if row:
                row.value = value
            else:
                db.add(PanelSettings(key=FAILED_DEPLOYS_SETTING, value=value))
            await db.commit()

    @staticmethod
    async def _remember_deployed(server_id: int, fingerprint: str) -> None:
        try:
            async with async_session() as db:
                await db.execute(
                    update(Server).where(Server.id == server_id)
                    .values(wildcard_ssl_deployed_fingerprint=fingerprint)
                )
                await db.commit()
        except (SQLAlchemyError, OSError) as e:
            # Сертификат уже на ноде: без этой записи сверка лишь раскатает его ещё раз
            logger.error(f"Failed to store deployed wildcard fingerprint for server {server_id}: {e}")

    async def deploy_to_node(
        self, server: Server, linked: list[WildcardCertificate], cert_id: Optional[int] = None
    ) -> dict:
        """Раскатать закреплённые за сервером сертификаты, cert_id — только один из них."""
        result = {"server_id": server.id, "server_name": server.name}
        if not linked:
            return {**result, "success": False, "message": NO_CERTIFICATES_MESSAGE}
        # Свои пути — одна пара файлов на сервер: второй сертификат затирал бы первый,
        # и при каждом продлении они сменяли бы друг друга
        if len(linked) > 1 and uses_custom_paths(server):
            return {**result, "success": False, "message": CUSTOM_PATHS_SINGLE_CERT_MESSAGE}

        certs = [c for c in linked if cert_id is None or c.id == cert_id]
        reload_command = server.wildcard_ssl_reload_cmd or ""
        reload_result = None
        message = ""
        for index, cert in enumerate(certs):
            # Службу перечитываем один раз — после того как легли файлы всех сертификатов
            is_last = index == len(certs) - 1
            ok, message, reload_result = await self._push_certificate(
                server, cert, reload_command if is_last else ""
            )
            if not ok:
                if len(certs) > 1:
                    message = f"{cert.domain}: {message}"
                return {**result, "success": False, "message": message, "reload_result": reload_result}

        if len(certs) > 1:
            message = f"Deployed {', '.join(c.domain for c in certs)}"
        # Отпечаток описывает весь набор сервера: раскатка одного сертификата из
        # нескольких не подтверждает, что остальные на месте
        if len(certs) == len(linked):
            await self._remember_deployed(server.id, deployment_fingerprint(server, linked))
        return {**result, "success": True, "message": message, "reload_result": reload_result}

    async def _push_certificate(
        self, server: Server, cert: WildcardCertificate, reload_command: str
    ) -> tuple[bool, str, Optional[dict]]:
        payload = {
            "fullchain_pem": cert.fullchain_pem,
            "privkey_pem": cert.privkey_pem,
            "reload_command": reload_command,
            **deploy_target(server, cert),
        }

        try:
            client = get_node_client(server)
            response = await client.post(
                f"{server.url}/api/ssl/wildcard/deploy",
                headers=node_auth_headers(server),
                json=payload,
                timeout=60.0,
            )
            if response.status_code != 200:
                # Без этой проверки любой не-200 разбирался как обычный ответ
                # и превращался в «success=False, Unknown» с уведомлением
                detail = ""
                try:
                    detail = response.json().get("detail", "")
                except Exception:
                    pass
                return False, detail or f"HTTP {response.status_code}", None
            data = response.json()
            return data.get("success", False), data.get("message", "Unknown"), data.get("reload_result")
        except Exception as e:
            logger.error(f"Deploy of {cert.domain} to {server.name} failed: {e}")
            return False, str(e), None

    # ─── Deploy to panel itself ───

    async def apply_to_panel(self, base_domain: str) -> dict:
        """Применить wildcard-сертификат к nginx самой панели.

        Nginx панели читает /etc/letsencrypt/live/{DOMAIN}/ — направляем этот
        путь симлинком на линию wildcard-сертификата и перезагружаем nginx.
        После этого каждое продление обновляет файлы «на месте», панели
        достаточно reload.
        """
        panel_domain = get_settings().domain
        if not panel_domain:
            return {"success": False, "message": "Panel domain is not configured"}
        if not wildcard_covers_domain(base_domain, panel_domain):
            return {
                "success": False,
                "message": f"Certificate *.{base_domain} does not cover panel domain {panel_domain}",
            }

        lineage = await self._find_cert_lineage(base_domain)
        if not lineage:
            return {"success": False, "message": "Certificate files not found in /etc/letsencrypt/live"}
        _, lineage_dir = lineage

        try:
            await asyncio.to_thread(self._link_panel_cert, panel_domain, lineage_dir)
        except OSError as e:
            logger.error(f"Failed to link panel cert to wildcard lineage: {e}")
            return {"success": False, "message": f"Failed to link certificate: {e}"}

        ok, detail = await self._reload_panel_nginx()
        if not ok:
            return {"success": False, "message": f"Certificate linked, but nginx reload failed: {detail}"}

        logger.info(f"Wildcard certificate *.{base_domain} applied to panel ({panel_domain})")
        return {"success": True, "message": f"Certificate applied to panel ({panel_domain})"}

    def _link_panel_cert(self, panel_domain: str, lineage_dir: Path) -> None:
        live_path = LETSENCRYPT_LIVE / panel_domain

        if live_path.is_symlink():
            if live_path.exists() and live_path.resolve() == lineage_dir.resolve():
                return
            live_path.unlink()
        elif live_path.exists():
            if live_path.resolve() == lineage_dir.resolve():
                return
            # Собственная standalone-линия панели. Убираем её вместе с
            # renewal-конфигом: иначе ночной certbot renew этой линии перепишет
            # симлинки внутри каталога wildcard-линии на свой архив.
            live_path.rename(self._free_backup_path(live_path.parent, f"{panel_domain}.pre-wildcard"))
            renewal_conf = LETSENCRYPT_RENEWAL / f"{panel_domain}.conf"
            if renewal_conf.exists():
                renewal_conf.rename(
                    self._free_backup_path(renewal_conf.parent, f"{panel_domain}.conf.pre-wildcard")
                )

        live_path.symlink_to(lineage_dir)

    @staticmethod
    def _free_backup_path(parent: Path, name: str) -> Path:
        candidate = parent / name
        counter = 2
        while candidate.exists() or candidate.is_symlink():
            candidate = parent / f"{name}-{counter}"
            counter += 1
        return candidate

    async def _reload_panel_nginx(self) -> tuple[bool, str]:
        def _reload() -> tuple[int, str]:
            client = docker.from_env()
            container = client.containers.get(PANEL_NGINX_CONTAINER)
            # nginx -s reload возвращает 0 даже при битом конфиге (сигнал ушёл,
            # мастер молча остаётся на старом) — валидируем заранее
            code, output = container.exec_run(["nginx", "-t"])
            if code != 0:
                return code, (output or b"").decode(errors="replace")
            code, output = container.exec_run(["nginx", "-s", "reload"])
            return code, (output or b"").decode(errors="replace")

        try:
            code, output = await asyncio.to_thread(_reload)
        except Exception as e:
            return False, str(e)
        if code != 0:
            return False, output.strip() or f"exit code {code}"
        return True, "reloaded"

    async def _apply_to_panel_if_enabled(self, base_domain: str, notify: bool = True) -> Optional[dict]:
        """Применить сертификат к панели, если настройка включена.

        Возвращает None, когда настройка выключена или сертификат выпущен на
        другой домен — из нескольких сертификатов панели подходит только её
        собственный. Сбой применения не роняет выпуск/продление: ошибка
        логируется и (в фоновых сценариях) уходит в Telegram.
        """
        if not wildcard_covers_domain(base_domain, get_settings().domain or ""):
            return None

        async with async_session() as db:
            enabled = await self._get_setting(db, USE_FOR_PANEL_SETTING)
            alerts = None
            if notify:
                alerts = (await db.execute(select(AlertSettings).limit(1))).scalar_one_or_none()

        if enabled != "true":
            return None

        try:
            result = await self.apply_to_panel(base_domain)
        except Exception as e:
            logger.error(f"Panel wildcard SSL apply crashed: {e}")
            result = {"success": False, "message": str(e)}

        if not result["success"]:
            logger.error(f"Panel wildcard SSL apply failed: {result['message']}")
            if notify:
                await self._notify(
                    alerts, self._msg_panel_apply_failed(alerts, base_domain, result["message"])
                )
        return result

    # ─── Auto-renew loop ───

    async def _check_and_renew(self):
        async with async_session() as db:
            enabled = await self._get_setting(db, AUTO_RENEW_SETTING)
            if enabled != "true":
                return

            days_before_str = await self._get_setting(db, "wildcard_renew_days_before")
            days_before = int(days_before_str) if days_before_str else 30

            certs = (await db.execute(
                select(WildcardCertificate).where(WildcardCertificate.auto_renew == True)
            )).scalars().all()
            alerts = (await db.execute(select(AlertSettings).limit(1))).scalar_one_or_none()

            now = datetime.now(timezone.utc)
            for cert in certs:
                if not cert.expiry_date:
                    continue
                days_left = (cert.expiry_date - now).days
                if days_left > days_before:
                    continue

                # Раскатывает продлённый сертификат сверка — она идёт следом в том же проходе
                logger.info(f"Auto-renewing *.{cert.base_domain} ({days_left} days left)")
                ok, msg = await self.renew_certificate(cert.id)
                if not ok:
                    logger.error(f"Auto-renew failed for *.{cert.base_domain}: {msg}")
                    await self._notify(alerts, self._msg_renew_failed(alerts, cert.base_domain, msg))

    # ─── Notifications ───

    @staticmethod
    def _alert_lang(settings: Optional[AlertSettings]) -> str:
        return (settings.language or "en").lower() if settings else "en"

    def _msg_renew_failed(self, settings: Optional[AlertSettings], base_domain: str, reason: str) -> str:
        if self._alert_lang(settings) == "ru":
            return f"Не удалось автоматически продлить сертификат *.{base_domain}:\n{reason}"
        return f"Failed to auto-renew certificate *.{base_domain}:\n{reason}"

    def _msg_deploy_failed(
        self, settings: Optional[AlertSettings], failed: list[dict], attempted: int
    ) -> str:
        lines = self._failure_lines(failed)
        if self._alert_lang(settings) == "ru":
            return (
                f"Сертификаты не развернулись на {len(failed)} из {attempted} нод:\n{lines}\n\n"
                "Панель повторяет раскатку раз в сутки. Новое уведомление об этих нодах "
                "придёт только после продления сертификата или смены настроек раскатки."
            )
        return (
            f"Certificates failed to deploy on {len(failed)} of {attempted} nodes:\n{lines}\n\n"
            "The panel retries the deploy once a day. These nodes will be reported again "
            "only after a certificate renewal or a change of their deploy settings."
        )

    @staticmethod
    def _failure_lines(failed: list[dict]) -> str:
        lines: list[str] = []
        used = 0
        for index, result in enumerate(failed):
            reason = (result.get("message") or "?").strip()[:FAILURE_REASON_CHARS]
            line = f"• {result['server_name']} ({result['domains']}): {reason}"
            if lines and used + len(line) > FAILURE_LIST_CHARS:
                lines.append(f"… +{len(failed) - index}")
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)

    def _msg_panel_apply_failed(
        self, settings: Optional[AlertSettings], base_domain: str, reason: str
    ) -> str:
        if self._alert_lang(settings) == "ru":
            return f"Сертификат *.{base_domain} не удалось применить к самой панели:\n{reason}"
        return f"Failed to apply certificate *.{base_domain} to the panel itself:\n{reason}"

    async def _notify(self, settings: Optional[AlertSettings], message: str):
        if not settings or not settings.telegram_bot_token or not settings.telegram_chat_id:
            return
        # В тексте — вывод certbot и stderr reload-команд: сообщение с неэкранированным
        # «<» или длиннее лимита Telegram отвергает целиком
        body = html.escape(message[:NOTIFY_TEXT_CHARS], quote=False)
        text = f"\U0001f534 <b>Wildcard SSL</b>\n\n{body}"
        try:
            from app.services.telegram_bot import get_telegram_bot_service
            await get_telegram_bot_service().send_message(
                settings.telegram_bot_token, settings.telegram_chat_id, text, retry_later=True,
            )
        except Exception as e:
            logger.error(f"Wildcard SSL notification failed: {e}")

    # ─── Certbot runner ───

    async def _run_certbot(
        self, base_domain: str, email: str, cf_token: str, force_renewal: bool,
        cert_name: Optional[str] = None,
    ) -> tuple[bool, str]:
        # Записать credentials во временный файл
        cf_ini = tempfile.NamedTemporaryFile(
            mode="w", suffix=".ini", prefix="cloudflare_", delete=False
        )
        try:
            cf_ini.write(f"dns_cloudflare_api_token = {cf_token}\n")
            cf_ini.close()
            os.chmod(cf_ini.name, 0o600)

            cmd = [
                "certbot", "certonly",
                "--non-interactive",
                "--agree-tos",
                "--expand",
                # Переиспользуем приватный ключ при продлении: cert обновляется,
                # ключ остаётся прежним. Это нужно внешним потребителям ключа
                # (anti-DDoS фронт Куратор) — им ключ передаётся один раз, а не
                # каждый цикл продления.
                "--reuse-key",
                "--cert-name", cert_name or base_domain,
                "--dns-cloudflare",
                "--dns-cloudflare-credentials", cf_ini.name,
                "--dns-cloudflare-propagation-seconds", "30",
                "-d", f"*.{base_domain}",
                "-d", base_domain,
            ]
            if email:
                cmd.extend(["--email", email])
            else:
                cmd.append("--register-unsafely-without-email")

            if force_renewal:
                cmd.append("--force-renewal")

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
            output = (stdout.decode() + "\n" + stderr.decode()).strip()

            if process.returncode == 0:
                return True, output
            else:
                logger.error(f"Certbot failed (exit {process.returncode}): {output}")
                return False, output
        finally:
            try:
                os.unlink(cf_ini.name)
            except OSError:
                pass

    # ─── Helpers ───

    async def _find_cert_lineage(self, base_domain: str) -> Optional[tuple[str, Path]]:
        """Найти актуальную certbot-линию для домена.

        После сбоев certbot может расплодить дубли base_domain-0001, -0002...
        Берём линию с самым поздним сроком; при равенстве предпочитаем ту, у
        которой цел renewal-конфиг (её certbot сможет продлить «на месте»).
        Возвращает (имя_линии, каталог в live) или None.
        """
        live_root = Path("/etc/letsencrypt/live")
        renewal_root = Path("/etc/letsencrypt/renewal")
        if not live_root.exists():
            return None

        oldest = datetime.min.replace(tzinfo=timezone.utc)
        candidates = []
        for d in live_root.iterdir():
            if not d.is_dir():
                continue
            if d.name != base_domain and not d.name.startswith(f"{base_domain}-"):
                continue
            fullchain = d / "fullchain.pem"
            if not (fullchain.exists() and (d / "privkey.pem").exists()):
                continue
            conf = renewal_root / f"{d.name}.conf"
            conf_valid = conf.exists() and conf.stat().st_size > 0
            expiry = await self._read_expiry(fullchain)
            candidates.append((expiry or oldest, conf_valid, d.name, d))

        if not candidates:
            return None

        candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
        _, _, name, path = candidates[0]
        return name, path

    async def _read_cert_files(self, base_domain: str) -> tuple[Optional[str], Optional[str], Optional[datetime]]:
        lineage = await self._find_cert_lineage(base_domain)
        if not lineage:
            return None, None, None

        _, cert_dir = lineage
        fullchain_path = cert_dir / "fullchain.pem"
        privkey_path = cert_dir / "privkey.pem"

        fullchain = fullchain_path.read_text()
        privkey = privkey_path.read_text()
        expiry = await self._read_expiry(fullchain_path)
        return fullchain, privkey, expiry

    async def _read_expiry(self, fullchain_path: Path) -> Optional[datetime]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "openssl", "x509", "-enddate", "-noout", "-in", str(fullchain_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate()
            if proc.returncode != 0:
                return None
            expiry_str = out.decode().strip().replace("notAfter=", "")
            return datetime.strptime(expiry_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        except Exception as e:
            logger.warning(f"Could not parse cert expiry for {fullchain_path}: {e}")
            return None

    @staticmethod
    async def _get_setting(db, key: str) -> Optional[str]:
        row = (await db.execute(
            select(PanelSettings).where(PanelSettings.key == key)
        )).scalar_one_or_none()
        return row.value if row else None

# ─── Singleton ───

_manager: Optional[WildcardSSLManager] = None


def get_wildcard_ssl_manager() -> WildcardSSLManager:
    global _manager
    if _manager is None:
        _manager = WildcardSSLManager()
    return _manager


def get_issue_status() -> dict:
    return dict(_issue_status)


async def start_wildcard_ssl_manager():
    manager = get_wildcard_ssl_manager()
    await manager.start()


async def stop_wildcard_ssl_manager():
    manager = get_wildcard_ssl_manager()
    await manager.stop()
