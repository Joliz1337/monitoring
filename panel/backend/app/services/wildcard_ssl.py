import asyncio
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import select, update

from app.database import async_session
from app.models import WildcardCertificate, Server, PanelSettings, AlertSettings
from app.services.http_client import get_node_client, node_auth_headers

logger = logging.getLogger(__name__)

DEFAULT_DEPLOY_PATH = "/etc/letsencrypt/live"

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
        self._check_interval = 86400  # 24 часа

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
        await asyncio.sleep(120)
        while self._running:
            try:
                await self._check_and_renew()
            except Exception as e:
                logger.error(f"Wildcard SSL auto-renew error: {e}")
            await asyncio.sleep(self._check_interval)

    # ─── Issue ───

    async def issue_certificate(self, base_domain: str, email: str, cf_token: str) -> tuple[bool, str]:
        global _issue_status
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
                # Удалить старый cert для этого домена если есть
                existing = (await db.execute(
                    select(WildcardCertificate).where(WildcardCertificate.base_domain == base_domain)
                )).scalar_one_or_none()
                if existing:
                    await db.delete(existing)
                    await db.flush()

                cert = WildcardCertificate(
                    domain=f"*.{base_domain}",
                    base_domain=base_domain,
                    fullchain_pem=fullchain,
                    privkey_pem=privkey,
                    expiry_date=expiry,
                    issued_at=datetime.now(timezone.utc),
                )
                db.add(cert)
                await db.commit()

            _issue_status["last_result"] = "success"
            _issue_status["output"] = result[1]
            logger.info(f"Wildcard certificate issued for *.{base_domain}")
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
            return True, f"Certificate renewed for *.{cert.base_domain}"

    # ─── Deploy ───

    async def deploy_to_server(self, cert_id: int, server_id: int) -> dict:
        async with async_session() as db:
            cert = (await db.execute(
                select(WildcardCertificate).where(WildcardCertificate.id == cert_id)
            )).scalar_one_or_none()
            if not cert:
                return {"success": False, "message": "Certificate not found", "server_id": server_id}

            server = (await db.execute(
                select(Server).where(Server.id == server_id)
            )).scalar_one_or_none()
            if not server:
                return {"success": False, "message": "Server not found", "server_id": server_id}

            return await self._deploy_to_node(cert, server)

    async def deploy_to_all(self, cert_id: int) -> list[dict]:
        # Читаем cert и серверы в короткой сессии и закрываем её до сетевого fan-out:
        # expire_on_commit=False оставляет объекты доступными, а коннект пула не висит
        # открытым на всё время раскатки.
        async with async_session() as db:
            cert = (await db.execute(
                select(WildcardCertificate).where(WildcardCertificate.id == cert_id)
            )).scalar_one_or_none()
            if not cert:
                return [{"success": False, "message": "Certificate not found"}]

            servers = list((await db.execute(
                select(Server).where(
                    Server.wildcard_ssl_enabled == True,
                    Server.is_active == True,
                )
            )).scalars().all())

        if not servers:
            return [{"success": False, "message": "No servers with wildcard SSL enabled"}]

        # Bounded fan-out: на сотнях нод не открываем разом сотни TLS-соединений.
        sem = asyncio.Semaphore(30)

        async def _guarded(s):
            async with sem:
                return await self._deploy_to_node(cert, s)

        return await asyncio.gather(*[_guarded(s) for s in servers], return_exceptions=False)

    async def _deploy_to_node(self, cert: WildcardCertificate, server: Server) -> dict:
        payload = {
            "fullchain_pem": cert.fullchain_pem,
            "privkey_pem": cert.privkey_pem,
            "reload_command": server.wildcard_ssl_reload_cmd or "",
        }

        if server.wildcard_ssl_custom_path_enabled and server.wildcard_ssl_custom_fullchain_path and server.wildcard_ssl_custom_privkey_path:
            payload["custom_fullchain_path"] = server.wildcard_ssl_custom_fullchain_path
            payload["custom_privkey_path"] = server.wildcard_ssl_custom_privkey_path
        else:
            base_path = server.wildcard_ssl_deploy_path or DEFAULT_DEPLOY_PATH
            payload["deploy_path"] = f"{base_path.rstrip('/')}/{cert.base_domain}"
            if server.wildcard_ssl_fullchain_name:
                payload["fullchain_filename"] = server.wildcard_ssl_fullchain_name
            if server.wildcard_ssl_privkey_name:
                payload["privkey_filename"] = server.wildcard_ssl_privkey_name

        try:
            client = get_node_client(server)
            response = await client.post(
                f"{server.url}/api/ssl/wildcard/deploy",
                headers=node_auth_headers(server),
                json=payload,
                timeout=60.0,
            )
            data = response.json()
            return {
                "success": data.get("success", False),
                "message": data.get("message", "Unknown"),
                "server_id": server.id,
                "server_name": server.name,
                "reload_result": data.get("reload_result"),
            }
        except Exception as e:
            logger.error(f"Deploy to {server.name} failed: {e}")
            return {
                "success": False,
                "message": str(e),
                "server_id": server.id,
                "server_name": server.name,
            }

    # ─── Auto-renew loop ───

    async def _check_and_renew(self):
        async with async_session() as db:
            enabled = await self._get_setting(db, "wildcard_auto_renew_enabled")
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

                logger.info(f"Auto-renewing *.{cert.base_domain} ({days_left} days left)")
                ok, msg = await self.renew_certificate(cert.id)
                if not ok:
                    logger.error(f"Auto-renew failed for *.{cert.base_domain}: {msg}")
                    await self._notify(alerts, self._msg_renew_failed(alerts, cert.base_domain, msg))
                    continue

                results = await self.deploy_to_all(cert.id)
                failed = [r for r in results if r.get("server_id") and not r.get("success")]
                if failed:
                    logger.error(
                        f"Auto-deploy of *.{cert.base_domain} failed on {len(failed)}/{len(results)} nodes"
                    )
                    await self._notify(
                        alerts, self._msg_deploy_failed(alerts, cert.base_domain, failed, len(results))
                    )

    # ─── Notifications ───

    @staticmethod
    def _alert_lang(settings: Optional[AlertSettings]) -> str:
        return (settings.language or "en").lower() if settings else "en"

    def _msg_renew_failed(self, settings: Optional[AlertSettings], base_domain: str, reason: str) -> str:
        if self._alert_lang(settings) == "ru":
            return f"Не удалось автоматически продлить сертификат *.{base_domain}:\n{reason}"
        return f"Failed to auto-renew certificate *.{base_domain}:\n{reason}"

    def _msg_deploy_failed(
        self, settings: Optional[AlertSettings], base_domain: str, failed: list[dict], total: int
    ) -> str:
        lines = "\n".join(
            f"• {r.get('server_name') or r.get('server_id')}: {r.get('message') or '?'}"
            for r in failed
        )
        if self._alert_lang(settings) == "ru":
            return (
                f"Сертификат *.{base_domain} продлён, но не развернулся "
                f"на {len(failed)} из {total} нод:\n{lines}"
            )
        return (
            f"Certificate *.{base_domain} renewed, but deploy failed "
            f"on {len(failed)} of {total} nodes:\n{lines}"
        )

    async def _notify(self, settings: Optional[AlertSettings], message: str):
        if not settings or not settings.telegram_bot_token or not settings.telegram_chat_id:
            return
        text = f"\U0001f534 <b>Wildcard SSL</b>\n\n{message}"
        try:
            from app.services.telegram_bot import get_telegram_bot_service
            await get_telegram_bot_service().send_message(
                settings.telegram_bot_token, settings.telegram_chat_id, text,
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

    @staticmethod
    async def _set_setting(db, key: str, value: str):
        row = (await db.execute(
            select(PanelSettings).where(PanelSettings.key == key)
        )).scalar_one_or_none()
        if row:
            row.value = value
        else:
            db.add(PanelSettings(key=key, value=value))


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
