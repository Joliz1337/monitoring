import logging
import tempfile
from datetime import datetime
from pathlib import PurePosixPath
from typing import Optional

from app.models.ssl import WildcardDeployRequest, WildcardDeployResponse, WildcardStatusResponse
from app.services.host_executor import get_host_executor

logger = logging.getLogger(__name__)


class SSLManager:

    def __init__(self):
        self._executor = get_host_executor()

    async def deploy_wildcard(self, request: WildcardDeployRequest) -> WildcardDeployResponse:
        fullchain_path, privkey_path, err = self._resolve_target_paths(request)
        if err:
            return WildcardDeployResponse(success=False, message=err)

        valid, verr = await self._validate_pem(request.fullchain_pem)
        if not valid:
            return WildcardDeployResponse(success=False, message=f"Invalid certificate: {verr}")

        for path in {str(PurePosixPath(fullchain_path).parent), str(PurePosixPath(privkey_path).parent)}:
            mk = await self._executor.execute(f'mkdir -p "{path}"', timeout=10)
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
        if request.reload_command.strip():
            reload = await self._executor.execute(request.reload_command, timeout=30)
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
            logger.info(f"Reload command succeeded: {request.reload_command}")

        logger.info(f"Wildcard certificate deployed: {fullchain_path}, {privkey_path}")
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
            fullchain = request.custom_fullchain_path.strip()
            privkey = request.custom_privkey_path.strip()
            if not fullchain.startswith("/") or not privkey.startswith("/"):
                return "", "", "Custom paths must be absolute"
            return fullchain, privkey, None

        if not request.deploy_path.strip():
            return "", "", "deploy_path is required when custom paths are not set"

        base = request.deploy_path.rstrip("/")
        if not base.startswith("/"):
            return "", "", "deploy_path must be absolute"
        fullchain_name = (request.fullchain_filename or "fullchain.pem").strip() or "fullchain.pem"
        privkey_name = (request.privkey_filename or "privkey.pem").strip() or "privkey.pem"
        return f"{base}/{fullchain_name}", f"{base}/{privkey_name}", None

    async def _backup_existing(
        self, fullchain_path: str, privkey_path: str
    ) -> Optional[str]:
        check = await self._executor.execute(
            f'test -f "{fullchain_path}" && echo "f"; test -f "{privkey_path}" && echo "k"; true',
            timeout=5
        )
        has_full = "f" in check.stdout
        has_key = "k" in check.stdout
        if not has_full and not has_key:
            return None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = f"{PurePosixPath(fullchain_path).parent}/backup_{ts}"
        cmds = [f'mkdir -p "{backup_dir}"']
        if has_full:
            cmds.append(f'cp "{fullchain_path}" "{backup_dir}/"')
        if has_key:
            cmds.append(f'cp "{privkey_path}" "{backup_dir}/"')

        bk = await self._executor.execute(" && ".join(cmds), timeout=10)
        if not bk.success:
            logger.warning(f"Backup failed: {bk.stderr}")
            return None
        logger.info(f"Backed up existing certs to {backup_dir}")
        return backup_dir

    async def get_status(self, deploy_path: str) -> WildcardStatusResponse:
        deploy_path = deploy_path.rstrip("/")
        cert_file = f"{deploy_path}/fullchain.pem"

        check = await self._executor.execute(f'test -f "{cert_file}" && echo "exists"', timeout=5)
        if "exists" not in check.stdout:
            return WildcardStatusResponse(deployed=False, cert_path=deploy_path)

        result = await self._executor.execute(
            f'openssl x509 -enddate -subject -noout -in "{cert_file}"',
            timeout=10
        )
        if not result.success:
            return WildcardStatusResponse(deployed=True, cert_path=deploy_path)

        domain = None
        expiry_date = None
        days_left = None

        for line in result.stdout.strip().split("\n"):
            if line.startswith("notAfter="):
                try:
                    expiry_str = line.replace("notAfter=", "")
                    expiry = datetime.strptime(expiry_str, "%b %d %H:%M:%S %Y %Z")
                    expiry_date = expiry.isoformat()
                    days_left = (expiry - datetime.utcnow()).days
                except ValueError:
                    pass
            elif "CN" in line:
                cn_part = line.split("CN")[-1].strip().lstrip("=").strip()
                if cn_part:
                    domain = cn_part

        return WildcardStatusResponse(
            deployed=True,
            domain=domain,
            expiry_date=expiry_date,
            days_left=days_left,
            cert_path=deploy_path
        )

    async def _validate_pem(self, pem_content: str) -> tuple[bool, str]:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
            f.write(pem_content)
            tmp_path = f.name

        result = await self._executor.execute(
            f'openssl x509 -noout -in "{tmp_path}" 2>&1; rm -f "{tmp_path}"',
            timeout=10
        )
        if result.exit_code != 0:
            return False, result.stdout + result.stderr
        return True, ""

    async def _write_cert_files(
        self,
        fullchain_path: str,
        privkey_path: str,
        fullchain: str,
        privkey: str,
    ) -> tuple[bool, str]:
        for filepath, content, mode in [
            (fullchain_path, fullchain, "644"),
            (privkey_path, privkey, "600"),
        ]:
            write_cmd = f"cat > '{filepath}' << 'CERTEOF'\n{content}\nCERTEOF"
            result = await self._executor.execute(write_cmd, timeout=10, shell="bash")
            if not result.success:
                return False, f"{filepath}: {result.stderr}"

            chmod = await self._executor.execute(
                f"chmod {mode} '{filepath}'", timeout=5, shell="bash"
            )
            if not chmod.success:
                logger.warning(
                    f"chmod {mode} {filepath} failed (ignored, filesystem may not support it): {chmod.stderr.strip()}"
                )

        return True, ""

    async def _rollback(
        self, backup_path: str, fullchain_path: str, privkey_path: str
    ) -> None:
        logger.warning(f"Rolling back from {backup_path}")
        fullchain_name = PurePosixPath(fullchain_path).name
        privkey_name = PurePosixPath(privkey_path).name
        await self._executor.execute(
            f'cp "{backup_path}/{fullchain_name}" "{fullchain_path}" 2>/dev/null; '
            f'cp "{backup_path}/{privkey_name}" "{privkey_path}" 2>/dev/null; true',
            timeout=10
        )


_ssl_manager: Optional[SSLManager] = None


def get_ssl_manager() -> SSLManager:
    global _ssl_manager
    if _ssl_manager is None:
        _ssl_manager = SSLManager()
    return _ssl_manager
