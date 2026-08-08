import logging
import re
import shlex
from datetime import datetime
from pathlib import PurePosixPath
from typing import Optional

from cryptography import x509
from cryptography.x509.oid import NameOID

from app.models.ssl import WildcardDeployRequest, WildcardDeployResponse
from app.services.host_executor import get_host_executor
from app.services.host_files import write_host_file

logger = logging.getLogger(__name__)

# Пути приходят от панели и подставляются в shell-команды nsenter. Помимо
# shlex.quote при подстановке отсекаем всё, что не похоже на обычный
# абсолютный путь: в двойных кавычках shell всё равно раскрыл бы $(...).
_SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")

DEFAULT_FULLCHAIN_NAME = "fullchain.pem"
DEFAULT_PRIVKEY_NAME = "privkey.pem"

# reload_command исполняется на хосте как есть — панель доверенная, но длина
# ограничена, чтобы опечатка в её настройках не ушла в exec целой простынёй.
MAX_RELOAD_COMMAND_LEN = 512


def _describe_certificate(cert: x509.Certificate) -> str:
    common_names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    subject = common_names[0].value if common_names else "no CN"
    return f"{subject}, valid until {cert.not_valid_after_utc:%Y-%m-%d}"


class SSLManager:

    def __init__(self):
        self._executor = get_host_executor()

    async def deploy_wildcard(self, request: WildcardDeployRequest) -> WildcardDeployResponse:
        fullchain_path, privkey_path, err = self._resolve_target_paths(request)
        if err:
            return WildcardDeployResponse(success=False, message=err)

        reload_command = request.reload_command.strip()
        if len(reload_command) > MAX_RELOAD_COMMAND_LEN:
            return WildcardDeployResponse(
                success=False,
                message=f"reload_command is longer than {MAX_RELOAD_COMMAND_LEN} characters",
            )

        try:
            certificate = x509.load_pem_x509_certificate(request.fullchain_pem.encode("utf-8"))
        except ValueError as exc:
            return WildcardDeployResponse(success=False, message=f"Invalid certificate: {exc}")

        for path in {str(PurePosixPath(fullchain_path).parent), str(PurePosixPath(privkey_path).parent)}:
            mk = await self._executor.execute(f"mkdir -p {shlex.quote(path)}", timeout=10)
            if not mk.success:
                return WildcardDeployResponse(
                    success=False,
                    message=f"Cannot create directory {path}: {mk.stderr}"
                )

        backup_path = await self._backup_existing(fullchain_path, privkey_path)

        write_ok, write_err = await self._write_cert_files(
            fullchain_path, privkey_path, request.fullchain_pem, request.privkey_pem
        )
        if not write_ok:
            if backup_path:
                await self._rollback(backup_path, fullchain_path, privkey_path)
            return WildcardDeployResponse(
                success=False,
                message=f"Failed to write certificate files: {write_err}",
                backup_path=backup_path
            )

        reload_result = None
        if reload_command:
            logger.warning(f"Running panel-supplied reload command on host: {reload_command}")
            reload = await self._executor.execute(reload_command, timeout=30)
            reload_result = {
                "exit_code": reload.exit_code,
                "stdout": reload.stdout,
                "stderr": reload.stderr,
            }
            if not reload.success:
                logger.error(f"Reload failed (exit {reload.exit_code}): {reload.stderr}")
                if backup_path:
                    await self._rollback(backup_path, fullchain_path, privkey_path)
                    return WildcardDeployResponse(
                        success=False,
                        message=f"Reload command failed (rolled back): {reload.stderr}",
                        backup_path=backup_path,
                        reload_result=reload_result
                    )
                return WildcardDeployResponse(
                    success=False,
                    message=f"Reload command failed: {reload.stderr}",
                    reload_result=reload_result
                )
            logger.info("Reload command succeeded")

        logger.info(
            f"Wildcard certificate deployed ({_describe_certificate(certificate)}): "
            f"{fullchain_path}, {privkey_path}"
        )
        return WildcardDeployResponse(
            success=True,
            message="Certificate deployed successfully",
            backup_path=backup_path,
            reload_result=reload_result
        )

    def _resolve_target_paths(
        self, request: WildcardDeployRequest
    ) -> tuple[str, str, Optional[str]]:
        if request.custom_fullchain_path and request.custom_privkey_path:
            return self._validated_pair(
                request.custom_fullchain_path.strip(),
                request.custom_privkey_path.strip(),
            )

        base = request.deploy_path.strip().rstrip("/")
        if not base:
            return "", "", "deploy_path is required when custom paths are not set"

        fullchain_name = (request.fullchain_filename or "").strip() or DEFAULT_FULLCHAIN_NAME
        privkey_name = (request.privkey_filename or "").strip() or DEFAULT_PRIVKEY_NAME
        return self._validated_pair(f"{base}/{fullchain_name}", f"{base}/{privkey_name}")

    @staticmethod
    def _validated_pair(fullchain: str, privkey: str) -> tuple[str, str, Optional[str]]:
        for path in (fullchain, privkey):
            if not _SAFE_PATH_RE.match(path):
                return "", "", (
                    f"Unsafe target path {path!r}: expected an absolute path "
                    "without spaces or shell metacharacters"
                )
        return fullchain, privkey, None

    async def _backup_existing(
        self, fullchain_path: str, privkey_path: str
    ) -> Optional[str]:
        check = await self._executor.execute(
            f'test -f {shlex.quote(fullchain_path)} && echo "f"; '
            f'test -f {shlex.quote(privkey_path)} && echo "k"; true',
            timeout=5
        )
        has_full = "f" in check.stdout
        has_key = "k" in check.stdout
        if not has_full and not has_key:
            return None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = f"{PurePosixPath(fullchain_path).parent}/backup_{ts}"
        cmds = [f"mkdir -p {shlex.quote(backup_dir)}"]
        if has_full:
            cmds.append(f"cp {shlex.quote(fullchain_path)} {shlex.quote(backup_dir)}/")
        if has_key:
            cmds.append(f"cp {shlex.quote(privkey_path)} {shlex.quote(backup_dir)}/")

        bk = await self._executor.execute(" && ".join(cmds), timeout=10)
        if not bk.success:
            logger.warning(f"Backup failed: {bk.stderr}")
            return None
        logger.info(f"Backed up existing certs to {backup_dir}")
        return backup_dir

    async def _write_cert_files(
        self,
        fullchain_path: str,
        privkey_path: str,
        fullchain: str,
        privkey: str,
    ) -> tuple[bool, str]:
        # Права выставляются тем же вызовом, что и запись: отдельный chmod оставлял бы
        # приватный ключ читаемым всем на время между двумя командами.
        for filepath, content, mode in [
            (fullchain_path, fullchain, "644"),
            (privkey_path, privkey, "600"),
        ]:
            if not await write_host_file(filepath, content, mode=mode):
                return False, f"{filepath} (причина в логах ноды)"

        return True, ""

    async def _rollback(
        self, backup_path: str, fullchain_path: str, privkey_path: str
    ) -> None:
        logger.warning(f"Rolling back from {backup_path}")
        restores = []
        for path in (fullchain_path, privkey_path):
            saved = f"{backup_path}/{PurePosixPath(path).name}"
            restores.append(f"cp {shlex.quote(saved)} {shlex.quote(path)} 2>/dev/null")
        await self._executor.execute("; ".join(restores) + "; true", timeout=10)


_ssl_manager: Optional[SSLManager] = None


def get_ssl_manager() -> SSLManager:
    global _ssl_manager
    if _ssl_manager is None:
        _ssl_manager = SSLManager()
    return _ssl_manager
