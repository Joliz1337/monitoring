import logging
import os
import re
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)

SSHD_CONFIG_PATH = "/etc/ssh/sshd_config"
FAIL2BAN_JAIL_DIR = "/etc/fail2ban/jail.d"
FAIL2BAN_JAIL_LOCAL = "/etc/fail2ban/jail.local"
FAIL2BAN_SSHD_CONF = f"{FAIL2BAN_JAIL_DIR}/sshd.conf"
MAX_BACKUPS = 5

SSHD_KEY_MAP = {
    "port": "Port",
    "permit_root_login": "PermitRootLogin",
    "password_authentication": "PasswordAuthentication",
    "pubkey_authentication": "PubkeyAuthentication",
    "permit_empty_passwords": "PermitEmptyPasswords",
    "max_auth_tries": "MaxAuthTries",
    "login_grace_time": "LoginGraceTime",
    "client_alive_interval": "ClientAliveInterval",
    "client_alive_count_max": "ClientAliveCountMax",
    "max_sessions": "MaxSessions",
    "max_startups": "MaxStartups",
    "allow_users": "AllowUsers",
    "x11_forwarding": "X11Forwarding",
}

SSHD_DEFAULTS = {
    "port": 22,
    "permit_root_login": "prohibit-password",
    "password_authentication": True,
    "pubkey_authentication": True,
    "permit_empty_passwords": False,
    "max_auth_tries": 6,
    "login_grace_time": 120,
    "client_alive_interval": 0,
    "client_alive_count_max": 3,
    "max_sessions": 10,
    "max_startups": "10:30:100",
    "allow_users": [],
    "x11_forwarding": True,
}

BOOL_KEYS = {
    "password_authentication", "pubkey_authentication",
    "permit_empty_passwords", "x11_forwarding",
}
INT_KEYS = {
    "port", "max_auth_tries", "login_grace_time",
    "client_alive_interval", "client_alive_count_max", "max_sessions",
}


class SSHConfigManager:

    def __init__(self):
        self._use_nsenter = self._detect_container()
        self._os_info = self._detect_os()
        self._ssh_service = self._detect_ssh_service()
        logger.info(
            "ssh_manager_init",
            extra={
                "container": self._use_nsenter,
                "os": self._os_info,
                "ssh": self._ssh_service,
            },
        )

    # ── Environment Detection ──

    def _detect_container(self) -> bool:
        if os.path.exists("/.dockerenv"):
            return True
        try:
            with open("/proc/1/cgroup", "r") as f:
                content = f.read()
                if "docker" in content or "containerd" in content or "kubepods" in content:
                    return True
        except Exception:
            pass
        return False

    def _detect_os(self) -> dict:
        info = {"distro": "unknown", "version": "", "pkg_manager": "apt"}

        ok, content, _ = self._run_cmd(["cat", "/etc/os-release"])
        if ok and content:
            for line in content.splitlines():
                if line.startswith("ID="):
                    info["distro"] = line.split("=", 1)[1].strip('"').lower()
                elif line.startswith("VERSION_ID="):
                    info["version"] = line.split("=", 1)[1].strip('"')

        rpm_distros = ("centos", "rhel", "rocky", "almalinux", "fedora", "oracle")
        if info["distro"] in rpm_distros:
            ok, _, _ = self._run_cmd(["which", "dnf"])
            info["pkg_manager"] = "dnf" if ok else "yum"

        return info

    def _detect_ssh_service(self) -> dict:
        """Определяет имя сервиса, socket unit и версию OpenSSH.

        Не использует `systemctl cat` — он не находит generated units
        (sshd-socket-generator на Ubuntu 22.04+). Вместо этого полагается
        на `is-active` и `is-enabled`, которые работают для всех типов units.
        """
        info: dict = {"service": "ssh", "socket": None, "version": ""}

        # Имя сервиса: проверяем через is-active / is-enabled (не через cat!)
        for name in ("ssh", "sshd"):
            ok, out, _ = self._run_cmd(["systemctl", "is-active", f"{name}.service"])
            if ok and out in ("active", "activating"):
                info["service"] = name
                break
            ok, out, _ = self._run_cmd(["systemctl", "is-enabled", f"{name}.service"])
            if ok and out in ("enabled", "enabled-runtime", "static", "alias"):
                info["service"] = name
                break

        # Socket activation: is-active / is-enabled работают для generated units
        info["socket"] = self._detect_socket_unit()

        # Версия OpenSSH
        ok, _, stderr = self._run_cmd(["sshd", "-V"])
        version_src = stderr if stderr else ""
        if not version_src:
            ok, version_src, _ = self._run_shell("ssh -V 2>&1")
        match = re.search(r"OpenSSH[_\s](\d+\.\d+)", version_src or "")
        if match:
            info["version"] = match.group(1)

        return info

    def _detect_socket_unit(self) -> str | None:
        """Определяет активный SSH socket unit.

        На Ubuntu 22.04+ ssh.socket может быть generated unit
        (sshd-socket-generator) — `systemctl cat` его не находит,
        но `is-active` и `is-enabled` работают.
        """
        for name in ("ssh.socket", "sshd.socket"):
            active_ok, active_out, _ = self._run_cmd(["systemctl", "is-active", name])
            if active_ok and active_out in ("active", "listening"):
                logger.info("socket_detected", extra={"unit": name, "state": active_out})
                return name
            enabled_ok, enabled_out, _ = self._run_cmd(["systemctl", "is-enabled", name])
            if enabled_ok and enabled_out in ("enabled", "enabled-runtime", "static", "generated", "alias"):
                logger.info("socket_detected", extra={"unit": name, "state": enabled_out})
                return name
        return None

    # ── Command Execution ──

    def _run_cmd(self, cmd: list[str], timeout: int = 30, input_data: str | None = None) -> tuple[bool, str, str]:
        if self._use_nsenter:
            cmd = ["nsenter", "-t", "1", "-m", "-u", "-n", "-i", "--"] + cmd
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, input=input_data,
            )
            return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
        except FileNotFoundError:
            return False, "", "Command not found"
        except subprocess.TimeoutExpired:
            return False, "", "Command timed out"
        except Exception as e:
            return False, "", str(e)

    def _run_shell(self, shell_cmd: str, timeout: int = 30) -> tuple[bool, str, str]:
        cmd = ["sh", "-c", shell_cmd]
        if self._use_nsenter:
            cmd = ["nsenter", "-t", "1", "-m", "-u", "-n", "-i", "--"] + cmd
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
        except FileNotFoundError:
            return False, "", "Command not found"
        except subprocess.TimeoutExpired:
            return False, "", "Command timed out"
        except Exception as e:
            return False, "", str(e)

    # ── sshd_config ──

    def _parse_sshd_value(self, key: str, raw_value: str) -> int | bool | str | list[str]:
        if key in BOOL_KEYS:
            return raw_value.lower() == "yes"
        if key in INT_KEYS:
            try:
                return int(raw_value)
            except ValueError:
                return SSHD_DEFAULTS.get(key, raw_value)
        if key == "allow_users":
            return raw_value.split()
        return raw_value

    def _format_sshd_value(self, key: str, value: int | bool | str | list[str]) -> str:
        if key in BOOL_KEYS:
            return "yes" if value else "no"
        if key == "allow_users" and isinstance(value, list):
            return " ".join(value)
        return str(value)

    def _parse_sshd_file(self, content: str, reverse_map: dict) -> dict:
        """Парсит содержимое одного sshd_config файла. Первый match выигрывает."""
        parsed: dict = {}
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            directive, raw_value = parts[0], parts[1]
            py_key = reverse_map.get(directive.lower())
            if py_key is None:
                continue
            # В sshd первый match выигрывает — не перезаписываем
            if py_key not in parsed:
                parsed[py_key] = self._parse_sshd_value(py_key, raw_value)
        return parsed

    def read_sshd_config(self) -> dict:
        """Читает эффективный конфиг sshd с учётом Include sshd_config.d/.

        sshd загружает Include ДО основного файла, и в sshd первый match
        выигрывает. Поэтому файлы из sshd_config.d/ имеют приоритет.
        """
        reverse_map = {v.lower(): k for k, v in SSHD_KEY_MAP.items()}
        parsed: dict = {}

        # Сначала читаем sshd_config.d/ (они загружаются через Include первыми)
        ok, files_out, _ = self._run_shell("ls -1 /etc/ssh/sshd_config.d/*.conf 2>/dev/null")
        if ok and files_out:
            for fpath in sorted(files_out.splitlines()):
                fpath = fpath.strip()
                if not fpath:
                    continue
                ok, content, _ = self._run_cmd(["cat", fpath])
                if ok and content:
                    file_parsed = self._parse_sshd_file(content, reverse_map)
                    for k, v in file_parsed.items():
                        if k not in parsed:
                            parsed[k] = v

        # Потом основной файл
        success, content, stderr = self._run_cmd(["cat", SSHD_CONFIG_PATH])
        if not success:
            logger.error("read_sshd_config_failed", extra={"error": stderr})
            return dict(SSHD_DEFAULTS)

        main_parsed = self._parse_sshd_file(content, reverse_map)
        for k, v in main_parsed.items():
            if k not in parsed:
                parsed[k] = v

        result = dict(SSHD_DEFAULTS)
        result.update(parsed)
        return result

    def _ensure_privsep_dir(self) -> None:
        self._run_cmd(["mkdir", "-p", "/run/sshd"])
        self._run_cmd(["chmod", "0755", "/run/sshd"])
        self._run_cmd(["chown", "root:root", "/run/sshd"])

    def test_sshd_config(self, config: dict) -> tuple[bool, list[str]]:
        tmp_path = "/tmp/sshd_config_test"
        try:
            current_config = self.read_sshd_config()
            merged = {**current_config, **config}
            content = self._build_sshd_content(merged)
            self._run_cmd(["tee", tmp_path], input_data=content)

            self._ensure_privsep_dir()
            success, _, stderr = self._run_cmd(["sshd", "-t", "-f", tmp_path])
            errors = []
            if not success:
                for line in (stderr or "").splitlines():
                    if line.strip():
                        errors.append(line.strip())
            return success, errors
        finally:
            self._run_cmd(["rm", "-f", tmp_path])

    def _build_sshd_content(self, full_config: dict) -> str:
        """Обновляет sshd_config: заменяет АКТИВНЫЕ директивы, добавляет новые.

        Никогда не раскомментирует закомментированные строки — это было
        источником багов, когда закомментированная опция неожиданно включалась.
        Match-блоки копируются как есть, новые ключи вставляются перед ними.
        """
        success, original, _ = self._run_cmd(["cat", SSHD_CONFIG_PATH])
        if not success:
            original = ""

        keys_to_set: dict[str, tuple[str, str]] = {}
        for py_key, value in full_config.items():
            sshd_key = SSHD_KEY_MAP.get(py_key)
            if sshd_key:
                keys_to_set[sshd_key.lower()] = (sshd_key, self._format_sshd_value(py_key, value))

        written_keys: set[str] = set()
        lines = original.splitlines()
        new_lines: list[str] = []
        in_match_block = False
        first_match_index: int | None = None

        for line in lines:
            stripped = line.strip()

            # Обнаруживаем Match-блок
            if stripped and not stripped.startswith("#"):
                parts = stripped.split(None, 1)
                if parts and parts[0].lower() == "match":
                    if first_match_index is None:
                        first_match_index = len(new_lines)
                    in_match_block = True
                    new_lines.append(line)
                    continue

            # Внутри Match-блока — копируем как есть
            if in_match_block:
                new_lines.append(line)
                continue

            # Заменяем только АКТИВНЫЕ (не закомментированные) директивы
            if stripped and not stripped.startswith("#"):
                parts = stripped.split(None, 1)
                if parts:
                    directive_lower = parts[0].lower()
                    if directive_lower in keys_to_set:
                        sshd_key, formatted = keys_to_set[directive_lower]
                        new_lines.append(f"{sshd_key} {formatted}")
                        written_keys.add(directive_lower)
                        continue

            # Всё остальное (комментарии, пустые строки) — оставляем как есть
            new_lines.append(line)

        # Добавляем ключи, которых не было в файле
        missing_lines = [
            f"{sshd_key} {formatted}"
            for key_lower, (sshd_key, formatted) in keys_to_set.items()
            if key_lower not in written_keys
        ]

        if missing_lines:
            if first_match_index is not None:
                new_lines = new_lines[:first_match_index] + missing_lines + new_lines[first_match_index:]
            else:
                new_lines.extend(missing_lines)

        return "\n".join(new_lines) + "\n"

    def _clean_sshd_config_d(self, keys_to_set: dict[str, tuple[str, str]]) -> list[str]:
        """Удаляет конфликтующие директивы из /etc/ssh/sshd_config.d/*.conf.

        sshd использует first-match-wins: файлы из sshd_config.d загружаются
        через Include раньше основного файла. Если cloud-init или другой конфиг
        задаёт PasswordAuthentication no в sshd_config.d/, наш PasswordAuthentication yes
        в основном файле будет проигнорирован.

        Решение: закомментировать конфликтующие строки в drop-in файлах.
        """
        cleaned: list[str] = []
        managed_directives = {k for k in keys_to_set}

        ok, files_out, _ = self._run_shell("ls -1 /etc/ssh/sshd_config.d/*.conf 2>/dev/null")
        if not ok or not files_out:
            return cleaned

        for fpath in sorted(files_out.splitlines()):
            fpath = fpath.strip()
            if not fpath:
                continue

            ok, content, _ = self._run_cmd(["cat", fpath])
            if not ok or not content:
                continue

            new_lines: list[str] = []
            modified = False
            for line in content.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    parts = stripped.split(None, 1)
                    if parts and parts[0].lower() in managed_directives:
                        new_lines.append(f"# Managed by monitoring panel: {line}")
                        modified = True
                        continue
                new_lines.append(line)

            if modified:
                new_content = "\n".join(new_lines) + "\n"
                self._run_cmd(["tee", fpath], input_data=new_content)
                cleaned.append(fpath)
                logger.info("sshd_config_d_cleaned", extra={"file": fpath})

        return cleaned

    def _create_backup(self) -> tuple[bool, str]:
        timestamp = int(time.time())
        backup_path = f"{SSHD_CONFIG_PATH}.bak.{timestamp}"
        success, _, stderr = self._run_cmd(["cp", "-p", SSHD_CONFIG_PATH, backup_path])
        if not success:
            return False, f"Backup failed: {stderr}"
        self._cleanup_old_backups()
        return True, backup_path

    def _cleanup_old_backups(self):
        success, output, _ = self._run_shell(
            f"ls -1t {SSHD_CONFIG_PATH}.bak.* 2>/dev/null"
        )
        if not success or not output:
            return
        backups = [b for b in output.splitlines() if b.strip()]
        for old_backup in backups[MAX_BACKUPS:]:
            self._run_cmd(["rm", "-f", old_backup.strip()])

    def _write_socket_port_override(self, socket_name: str, port: int) -> tuple[bool, str, str]:
        override_dir = f"/etc/systemd/system/{socket_name}.d"
        override_path = f"{override_dir}/listen-port.conf"
        content = (
            "[Socket]\n"
            "ListenStream=\n"
            f"ListenStream=0.0.0.0:{port}\n"
            f"ListenStream=[::]:{port}\n"
        )
        self._run_cmd(["mkdir", "-p", override_dir])
        ok, _, stderr = self._run_cmd(["tee", override_path], input_data=content)
        if not ok:
            logger.error("socket_override_write_failed", extra={"path": override_path, "error": stderr})
            return False, override_path, f"Failed to write socket override: {stderr}"

        ok, _, stderr = self._run_cmd(["systemctl", "daemon-reload"])
        if not ok:
            return False, override_path, f"daemon-reload failed: {stderr}"

        # Верификация: файл записан и читаем
        verify_ok, verify_content, _ = self._run_cmd(["cat", override_path])
        logger.info(
            "socket_override_written",
            extra={"path": override_path, "port": port, "content": verify_content[:200] if verify_ok else "UNREADABLE"},
        )
        return True, override_path, ""

    def _verify_port_listening(self, expected_port: int) -> bool:
        # Первая итерация: логируем что видим для диагностики
        ss_candidates = ["ss", "/usr/sbin/ss", "/sbin/ss", "/usr/bin/ss"]
        for attempt in range(15):
            if attempt == 0:
                _, all_listen, _ = self._run_shell(
                    "ss -tln 2>/dev/null || /usr/sbin/ss -tln 2>/dev/null || echo 'ss unavailable'"
                )
                logger.debug("verify_port_all_listeners", extra={"output": (all_listen or "")[:500]})
            for ss_bin in ss_candidates:
                ok, output, _ = self._run_shell(
                    f"{ss_bin} -Hltn 'sport = :{expected_port}' 2>/dev/null"
                )
                if ok and output.strip():
                    return True
                ok, output, _ = self._run_shell(
                    f"{ss_bin} -tln 2>/dev/null | grep -E ':{expected_port}([[:space:]]|$)'"
                )
                if ok and output.strip():
                    return True

            hex_port = f"{expected_port:04X}"
            ok, output, _ = self._run_shell(
                f"awk 'NR>1 && $4==\"0A\" {{split($2,a,\":\"); if (a[2]==\"{hex_port}\") {{print; exit}}}}' "
                f"/proc/net/tcp /proc/net/tcp6 2>/dev/null"
            )
            if ok and output.strip():
                return True

            time.sleep(0.5)

        _, ss_diag, _ = self._run_shell("ss -tln 2>&1 || /usr/sbin/ss -tln 2>&1 || echo 'ss unavailable'")
        svc = self._ssh_service["service"]
        _, svc_diag, _ = self._run_shell(
            f"systemctl status {svc}.service {svc}.socket 2>&1 | head -40"
        )
        logger.error(
            "port_not_listening",
            extra={
                "port": expected_port,
                "ss_output": ss_diag[:500],
                "svc_status": svc_diag[:500],
            },
        )
        return False

    def _verify_sshd_responding(self, port: int) -> bool:
        """Проверяет что sshd реально отвечает SSH-баннером, а не просто порт открыт."""
        ok, output, _ = self._run_shell(
            f"ssh-keyscan -p {port} -T 3 127.0.0.1 2>/dev/null"
        )
        if ok and output.strip():
            return True
        # Фолбэк: пробуем прочитать баннер напрямую
        ok, output, _ = self._run_shell(
            f"echo '' | timeout 3 bash -c 'cat < /dev/tcp/127.0.0.1/{port}' 2>/dev/null || "
            f"timeout 3 nc -w2 127.0.0.1 {port} 2>/dev/null"
        )
        return ok and "SSH" in (output or "")

    def _get_actual_listening_port(self) -> int:
        """Определяет на каком порту sshd/socket реально слушает.

        Сравнение с конфигом ненадёжно: sshd_config может содержать Port 1794
        от предыдущей неудачной попытки, а socket по-прежнему на 22.
        """
        # Пробуем ss с фильтром по процессу sshd
        for pattern in ("sshd", "ssh"):
            ok, output, _ = self._run_shell(
                f"ss -tlnp 2>/dev/null | grep '{pattern}'"
            )
            if ok and output:
                match = re.search(r":(\d+)\s", output)
                if match:
                    return int(match.group(1))

        # Фолбэк: проверяем типичные порты
        for port in (22, 2222, 1794):
            ok, output, _ = self._run_shell(
                f"ss -Hltn 'sport = :{port}' 2>/dev/null"
            )
            if ok and output.strip():
                return port

        return 22

    def _check_authorized_keys_exist(self, user: str = "root") -> bool:
        path = self._get_authorized_keys_path(user)
        success, output, _ = self._run_shell(f"test -s {path} && echo exists")
        return success and "exists" in output

    def _restart_sshd(self, action: str = "reload") -> tuple[bool, str]:
        """Перезапускает sshd используя определённое имя сервиса.

        Пробует reload → restart. Не перебирает все возможные имена —
        использует то, что нашли при инициализации.
        """
        svc = self._ssh_service["service"]
        ok, _, err = self._run_cmd(["systemctl", action, f"{svc}.service"])
        if ok:
            return True, ""
        if action == "reload":
            ok, _, err = self._run_cmd(["systemctl", "restart", f"{svc}.service"])
            if ok:
                return True, ""
        return False, err

    def write_sshd_config(self, config: dict) -> tuple[bool, str, list[str]]:
        warnings: list[str] = []

        current = self.read_sshd_config()
        merged = {**current, **config}

        # Safety: нельзя отключить оба метода аутентификации
        pwd_auth = merged.get("password_authentication", True)
        key_auth = merged.get("pubkey_authentication", True)
        if not pwd_auth and not key_auth:
            return False, "Cannot disable both password and pubkey authentication", []

        # Safety: если отключаем пароль — проверить наличие ключей
        if not pwd_auth and key_auth:
            if not self._check_authorized_keys_exist():
                return (
                    False,
                    "Cannot disable password auth: no authorized_keys found for root",
                    [],
                )

        new_port = merged.get("port", 22)
        # Сравниваем с РЕАЛЬНЫМ слушающим портом, не с конфигом.
        # sshd_config может содержать Port 1794 от предыдущей неудачной попытки,
        # а socket по-прежнему слушает на 22.
        actual_port = self._get_actual_listening_port()
        config_port = current.get("port", 22)
        port_changed = actual_port != new_port
        logger.info(
            "ssh_port_state",
            extra={"config": config_port, "actual": actual_port, "target": new_port, "changed": port_changed},
        )

        # Бэкап
        bk_ok, backup_path = self._create_backup()
        if not bk_ok:
            return False, backup_path, warnings

        # Чистим sshd_config.d/ от конфликтующих директив (cloud-init и т.п.)
        # В sshd first-match-wins, Include загружается раньше основного файла
        keys_to_set: dict[str, tuple[str, str]] = {}
        for py_key, value in merged.items():
            sshd_key = SSHD_KEY_MAP.get(py_key)
            if sshd_key:
                keys_to_set[sshd_key.lower()] = (sshd_key, self._format_sshd_value(py_key, value))
        cleaned_files = self._clean_sshd_config_d(keys_to_set)
        if cleaned_files:
            warnings.append(f"Cleaned conflicting directives from: {', '.join(cleaned_files)}")

        # Собираем новый конфиг
        new_content = self._build_sshd_content(merged)
        tmp_path = "/tmp/sshd_config_new"

        self._run_cmd(["tee", tmp_path], input_data=new_content)

        # Валидация через sshd -t
        self._ensure_privsep_dir()
        valid, _, stderr = self._run_cmd(["sshd", "-t", "-f", tmp_path])
        if not valid:
            self._run_cmd(["rm", "-f", tmp_path])
            logger.error("sshd_config_validation_failed", extra={"error": stderr})
            return False, f"Config validation failed: {stderr}", warnings

        # При смене порта — открыть новый в UFW до применения
        if port_changed:
            ufw_ok, _, ufw_err = self._run_cmd(["ufw", "allow", f"{new_port}/tcp"])
            if not ufw_ok:
                warnings.append(f"UFW: failed to open port {new_port}: {ufw_err}")

        # Атомарная замена конфига
        mv_ok, _, mv_err = self._run_cmd(["mv", tmp_path, SSHD_CONFIG_PATH])
        if not mv_ok:
            self._run_cmd(["rm", "-f", tmp_path])
            return False, f"Failed to apply config: {mv_err}", warnings

        # Socket override при смене порта (Ubuntu 22.04+)
        # Всегда свежая детекция — кэш мог устареть
        socket_unit = self._detect_socket_unit()
        socket_override_path: str | None = None

        if port_changed and socket_unit:
            logger.info("socket_port_override", extra={"unit": socket_unit, "port": new_port})
            ok, socket_override_path, err = self._write_socket_port_override(socket_unit, new_port)
            if not ok:
                self._rollback(backup_path, None)
                return False, err, warnings

        # Перезапуск sshd
        apply_ok, apply_err = self._apply_sshd(port_changed, socket_unit)

        if not apply_ok:
            logger.error("sshd_apply_failed", extra={"error": apply_err})
            self._rollback(backup_path, socket_override_path)
            return False, f"apply failed, backup restored: {apply_err}", warnings

        # Проверка что порт слушает
        port_ok = self._verify_port_listening(new_port)

        # Auto-recovery: если порт не слушает и socket не был определён —
        # пробуем определить socket заново и применить override
        if not port_ok and port_changed and not socket_unit:
            port_ok, socket_override_path = self._try_socket_recovery(new_port, socket_override_path)

        if not port_ok:
            logger.error("sshd_not_listening", extra={"port": new_port})
            self._rollback(backup_path, socket_override_path)
            return False, f"sshd не слушает порт {new_port} после применения, откат", warnings

        # Проверка что sshd реально отвечает
        if not self._verify_sshd_responding(new_port):
            warnings.append("sshd listening but not responding to SSH handshake — may need manual check")
            logger.warning("sshd_not_responding", extra={"port": new_port})

        # При смене порта — закрыть старый в UFW
        if port_changed:
            self._run_cmd(["ufw", "delete", "allow", f"{actual_port}/tcp"])

        logger.info("sshd_config_applied", extra={"port": new_port, "port_changed": port_changed})
        return True, "Configuration applied successfully", warnings

    def _apply_sshd(self, port_changed: bool, socket_unit: str | None) -> tuple[bool, str]:
        """Применяет конфиг: reload или restart в зависимости от ситуации."""
        svc = f"{self._ssh_service['service']}.service"

        if socket_unit:
            if port_changed:
                # Socket activation + смена порта.
                # Полный стоп → daemon-reload → старт в правильном порядке.
                # restart недостаточен: socket может не подхватить новый ListenStream.
                logger.info("socket_apply_start", extra={"socket": socket_unit, "service": svc})

                self._run_cmd(["systemctl", "stop", svc])
                self._run_cmd(["systemctl", "stop", socket_unit])
                # daemon-reload ещё раз — гарантирует что override и sshd-socket-generator
                # (Ubuntu 24) подхватили новый Port
                self._run_cmd(["systemctl", "daemon-reload"])

                sock_ok, _, sock_err = self._run_cmd(["systemctl", "start", socket_unit])
                if not sock_ok:
                    logger.error("socket_start_failed", extra={"unit": socket_unit, "error": sock_err})
                    return False, f"{socket_unit} start failed: {sock_err}"

                # Проверяем что socket реально стартовал
                _, sock_state, _ = self._run_cmd(["systemctl", "is-active", socket_unit])
                logger.info("socket_state_after_start", extra={"unit": socket_unit, "state": sock_state})

                # Диагностика: на каком порту слушает socket
                _, listen_diag, _ = self._run_shell(
                    f"systemctl show {socket_unit} -p Listen 2>/dev/null || "
                    "ss -tlnp 2>/dev/null | head -20"
                )
                logger.info("socket_listen_info", extra={"info": (listen_diag or "")[:300]})

                ok, _, err = self._run_cmd(["systemctl", "start", svc])
                if not ok:
                    logger.error("service_start_failed", extra={"service": svc, "error": err})
                    # service может не стартовать, но socket слушает — sshd стартует по activation
                    # проверим ниже через verify_port
                _, svc_state, _ = self._run_cmd(["systemctl", "is-active", svc])
                logger.info("service_state_after_start", extra={"service": svc, "state": svc_state})

                return True, ""
            else:
                ok, err = self._restart_sshd("reload")
                return ok, err
        else:
            action = "restart" if port_changed else "reload"
            ok, err = self._restart_sshd(action)
            return ok, err

    def _try_socket_recovery(self, port: int, current_override: str | None) -> tuple[bool, str | None]:
        """Если порт не слушает — пробуем определить socket заново и применить override.

        Типичная причина: ssh.socket — generated unit, не пойманный при инициализации.
        """
        socket_unit = self._detect_socket_unit()
        if not socket_unit:
            return False, current_override

        logger.warning("socket_autorecovery", extra={"unit": socket_unit, "port": port})

        ok, override_path, _ = self._write_socket_port_override(socket_unit, port)
        if not ok:
            return False, current_override

        svc = f"{self._ssh_service['service']}.service"
        self._run_cmd(["systemctl", "stop", svc])
        self._run_cmd(["systemctl", "restart", socket_unit])
        self._run_cmd(["systemctl", "start", svc])

        if self._verify_port_listening(port):
            logger.info("socket_autorecovery_success", extra={"port": port})
            self._ssh_service["socket"] = socket_unit
            return True, override_path

        return False, override_path

    def _rollback(self, backup_path: str, socket_override_path: str | None) -> None:
        """Откат: восстанавливает бэкап и рестартит только известный сервис."""
        logger.warning("sshd_rollback", extra={"backup": backup_path})
        self._run_cmd(["cp", "-p", backup_path, SSHD_CONFIG_PATH])
        if socket_override_path:
            self._run_cmd(["rm", "-f", socket_override_path])
            self._run_cmd(["systemctl", "daemon-reload"])

        svc = self._ssh_service["service"]
        # Свежая детекция: кэш мог быть обновлён через auto-recovery
        socket_unit = self._detect_socket_unit()
        if socket_unit:
            self._run_cmd(["systemctl", "restart", socket_unit])
        self._run_cmd(["systemctl", "restart", f"{svc}.service"])

    # ── fail2ban ──

    def _is_fail2ban_installed(self) -> bool:
        success, _, _ = self._run_cmd(["which", "fail2ban-client"])
        return success

    def _is_fail2ban_running(self) -> bool:
        success, output, _ = self._run_cmd(["systemctl", "is-active", "fail2ban"])
        return success and output == "active"

    def _parse_fail2ban_section(self, content: str, section: str = "sshd") -> dict:
        result: dict = {}
        in_section = False
        section_header = f"[{section}]"

        for line in content.splitlines():
            line = line.strip()
            if line == section_header:
                in_section = True
                continue
            if in_section and line.startswith("["):
                break
            if not in_section or not line or line.startswith("#") or line.startswith(";"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                result[key.strip()] = value.strip()
        return result

    def _convert_ban_time(self, value: str) -> int:
        value = value.strip()
        multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
        match = re.match(r"^(\d+)\s*([smhdw])?$", value, re.IGNORECASE)
        if match:
            num = int(match.group(1))
            suffix = (match.group(2) or "s").lower()
            return num * multipliers.get(suffix, 1)
        try:
            return int(value)
        except ValueError:
            return 0

    def _detect_fail2ban_backend(self) -> str:
        ok, _, _ = self._run_cmd(["journalctl", "--no-pager", "-n", "1"])
        return "systemd" if ok else "auto"

    def read_fail2ban_config(self) -> dict:
        if not self._is_fail2ban_installed():
            return {"installed": False}

        parsed: dict = {}

        # Приоритет: jail.local (глобальные), потом jail.d/sshd.conf (наш файл)
        for path in [FAIL2BAN_JAIL_LOCAL, FAIL2BAN_SSHD_CONF]:
            success, content, _ = self._run_cmd(["cat", path])
            if success and content:
                section = self._parse_fail2ban_section(content)
                parsed.update(section)

        return {
            "installed": True,
            "enabled": parsed.get("enabled", "true").lower() == "true",
            "max_retry": int(parsed.get("maxretry", 5)),
            "ban_time": self._convert_ban_time(parsed.get("bantime", "600")),
            "find_time": self._convert_ban_time(parsed.get("findtime", "600")),
        }

    def _install_fail2ban(self) -> tuple[bool, str]:
        pkg = self._os_info["pkg_manager"]
        if pkg == "apt":
            cmd = ["apt-get", "install", "-y", "fail2ban"]
        elif pkg == "dnf":
            cmd = ["dnf", "install", "-y", "fail2ban"]
        else:
            cmd = ["yum", "install", "-y", "fail2ban"]

        logger.info("installing_fail2ban", extra={"pkg_manager": pkg})
        ok, _, stderr = self._run_cmd(cmd, timeout=120)
        if not ok:
            return False, f"Failed to install fail2ban via {pkg}: {stderr}"
        return True, ""

    def write_fail2ban_config(self, config: dict) -> tuple[bool, str]:
        if not self._is_fail2ban_installed():
            ok, err = self._install_fail2ban()
            if not ok:
                return False, err

        # Мержим с текущими настройками — не сбрасываем то, что не менялось
        current = self.read_fail2ban_config()
        enabled = config.get("enabled", current.get("enabled", True))
        max_retry = config.get("max_retry", current.get("max_retry", 5))
        ban_time = config.get("ban_time", current.get("ban_time", 600))
        find_time = config.get("find_time", current.get("find_time", 600))
        backend = self._detect_fail2ban_backend()

        content = (
            "[sshd]\n"
            f"enabled = {'true' if enabled else 'false'}\n"
            f"maxretry = {max_retry}\n"
            f"bantime = {ban_time}\n"
            f"findtime = {find_time}\n"
            f"backend = {backend}\n"
        )

        self._run_cmd(["mkdir", "-p", FAIL2BAN_JAIL_DIR])
        ok, _, stderr = self._run_cmd(["tee", FAIL2BAN_SSHD_CONF], input_data=content)
        if not ok:
            return False, f"Failed to write config: {stderr}"

        ok, _, stderr = self._run_cmd(["systemctl", "restart", "fail2ban"])
        if not ok:
            return False, f"Failed to restart fail2ban: {stderr}"

        # Ждём старт, проверяем до 5 сек
        for _ in range(5):
            time.sleep(1)
            if self._is_fail2ban_running():
                logger.info("fail2ban_config_applied")
                return True, "fail2ban configuration applied"

        # Диагностика
        _, status_out, _ = self._run_shell("systemctl status fail2ban 2>&1 | tail -20")
        logger.error("fail2ban_start_failed", extra={"status": status_out[:500]})
        return False, "fail2ban failed to start after config change"

    def get_fail2ban_banned(self) -> list[dict]:
        if not self._is_fail2ban_installed() or not self._is_fail2ban_running():
            return []

        success, output, _ = self._run_cmd(["fail2ban-client", "status", "sshd"])
        if not success:
            return []

        banned_ips: list[dict] = []
        for line in output.splitlines():
            line = line.strip()
            if "Banned IP list:" in line:
                _, _, ip_list = line.partition(":")
                for ip in ip_list.strip().split():
                    ip = ip.strip()
                    if ip:
                        banned_ips.append({"ip": ip, "ban_time_remaining": 0})

        return banned_ips

    def unban_ip(self, ip: str) -> tuple[bool, str]:
        ok, _, stderr = self._run_cmd(["fail2ban-client", "set", "sshd", "unbanip", ip])
        if ok:
            logger.info("ip_unbanned", extra={"ip": ip})
            return True, f"IP {ip} unbanned"
        return False, f"Failed to unban {ip}: {stderr}"

    def unban_all(self) -> tuple[bool, str]:
        ok, _, stderr = self._run_cmd(["fail2ban-client", "unban", "--all"])
        if ok:
            logger.info("all_ips_unbanned")
            return True, "All IPs unbanned"
        return False, f"Failed to unban all: {stderr}"

    # ── SSH keys ──

    def _get_ssh_dir(self, user: str) -> str:
        if user == "root":
            return "/root/.ssh"
        return f"/home/{user}/.ssh"

    def _get_authorized_keys_path(self, user: str) -> str:
        return f"{self._get_ssh_dir(user)}/authorized_keys"

    def list_authorized_keys(self, user: str = "root") -> list[dict]:
        ak_path = self._get_authorized_keys_path(user)
        success, content, _ = self._run_cmd(["cat", ak_path])
        if not success or not content:
            return []

        keys: list[dict] = []
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue

            key_type = parts[0]
            key_data = parts[1]
            comment = parts[2] if len(parts) > 2 else ""

            fp_ok, fp_out, _ = self._run_cmd(
                ["ssh-keygen", "-lf", "-"], input_data=line,
            )
            fingerprint = ""
            if fp_ok and fp_out:
                fp_parts = fp_out.split(None, 2)
                if len(fp_parts) >= 2:
                    fingerprint = fp_parts[1]

            keys.append({
                "type": key_type,
                "fingerprint": fingerprint,
                "comment": comment,
                "key_data": key_data,
            })

        return keys

    def _validate_public_key(self, public_key: str) -> bool:
        # Валидация через ssh-keygen — надёжнее чем проверка типа вручную
        ok, _, _ = self._run_cmd(["ssh-keygen", "-lf", "-"], input_data=public_key)
        return ok

    def add_authorized_key(self, user: str, public_key: str) -> tuple[bool, str, str]:
        public_key = public_key.strip()
        if not self._validate_public_key(public_key):
            return False, "Invalid public key format", ""

        ssh_dir = self._get_ssh_dir(user)
        ak_path = self._get_authorized_keys_path(user)

        self._run_cmd(["mkdir", "-p", ssh_dir])
        self._run_cmd(["chmod", "700", ssh_dir])

        if user != "root":
            self._run_cmd(["chown", f"{user}:{user}", ssh_dir])

        # Проверяем дубликат
        existing = self.list_authorized_keys(user)
        new_parts = public_key.split(None, 2)
        if len(new_parts) >= 2:
            new_key_data = new_parts[1]
            for key in existing:
                if key["key_data"] == new_key_data:
                    return False, "Key already exists", key.get("fingerprint", "")

        self._run_cmd(["tee", "-a", ak_path], input_data=public_key + "\n")
        self._run_cmd(["chmod", "600", ak_path])
        if user != "root":
            self._run_cmd(["chown", f"{user}:{user}", ak_path])

        fp_ok, fp_out, _ = self._run_cmd(
            ["ssh-keygen", "-lf", "-"], input_data=public_key,
        )
        fingerprint = ""
        if fp_ok and fp_out:
            fp_parts = fp_out.split(None, 2)
            if len(fp_parts) >= 2:
                fingerprint = fp_parts[1]

        logger.info("ssh_key_added", extra={"user": user, "fingerprint": fingerprint})
        return True, "Key added successfully", fingerprint

    def remove_authorized_key(self, user: str, fingerprint: str) -> tuple[bool, str]:
        ak_path = self._get_authorized_keys_path(user)
        success, content, _ = self._run_cmd(["cat", ak_path])
        if not success or not content:
            return False, "authorized_keys not found or empty"

        new_lines: list[str] = []
        removed = False
        for line in content.splitlines():
            line_stripped = line.strip()
            if not line_stripped or line_stripped.startswith("#"):
                new_lines.append(line)
                continue

            fp_ok, fp_out, _ = self._run_cmd(
                ["ssh-keygen", "-lf", "-"], input_data=line_stripped,
            )
            if fp_ok and fp_out:
                fp_parts = fp_out.split(None, 2)
                if len(fp_parts) >= 2 and fp_parts[1] == fingerprint:
                    removed = True
                    continue
            new_lines.append(line)

        if not removed:
            return False, f"Key with fingerprint {fingerprint} not found"

        new_content = "\n".join(new_lines) + "\n"
        self._run_cmd(["tee", ak_path], input_data=new_content)
        self._run_cmd(["chmod", "600", ak_path])

        logger.info("ssh_key_removed", extra={"user": user, "fingerprint": fingerprint})
        return True, "Key removed successfully"

    # ── Password ──

    def _user_exists(self, user: str) -> bool:
        ok, _, _ = self._run_cmd(["id", user])
        return ok

    def change_password(self, user: str, password: str) -> tuple[bool, str]:
        if not self._user_exists(user):
            return False, f"User '{user}' does not exist"

        ok, _, stderr = self._run_cmd(
            ["chpasswd"], input_data=f"{user}:{password}",
        )
        if not ok:
            logger.error("password_change_failed", extra={"user": user, "error": stderr})
            return False, f"Failed to change password: {stderr}"

        # Верификация: проверяем что пароль-хеш обновился
        ok, shadow_line, _ = self._run_shell(
            f"getent shadow {user} 2>/dev/null | cut -d: -f2"
        )
        if ok and shadow_line and shadow_line not in ("!", "*", "!!"):
            logger.info("password_changed", extra={"user": user})
            return True, "Password changed successfully"

        # getent shadow может быть недоступен — доверяем chpasswd
        logger.info("password_changed_unverified", extra={"user": user})
        return True, "Password changed successfully"

    # ── Status ──

    def get_status(self) -> dict:
        svc = self._ssh_service["service"]
        sshd_active, sshd_out, _ = self._run_cmd(["systemctl", "is-active", f"{svc}.service"])
        sshd_running = sshd_active and sshd_out == "active"

        sshd_config = self.read_sshd_config()

        pwd_auth = sshd_config.get("password_authentication", True)
        key_auth = sshd_config.get("pubkey_authentication", True)
        if pwd_auth and key_auth:
            auth_method = "both"
        elif key_auth:
            auth_method = "key"
        elif pwd_auth:
            auth_method = "password"
        else:
            auth_method = "none"

        f2b_installed = self._is_fail2ban_installed()
        f2b_running = self._is_fail2ban_running() if f2b_installed else False
        f2b_banned = len(self.get_fail2ban_banned()) if f2b_running else 0

        keys = self.list_authorized_keys()

        return {
            "sshd_running": sshd_running,
            "sshd_port": sshd_config.get("port", 22),
            "fail2ban_installed": f2b_installed,
            "fail2ban_running": f2b_running,
            "fail2ban_banned_count": f2b_banned,
            "auth_method": auth_method,
            "authorized_keys_count": len(keys),
            "os": self._os_info,
            "ssh_service": {
                "name": self._ssh_service["service"],
                "socket": self._ssh_service.get("socket"),
                "version": self._ssh_service.get("version", ""),
            },
        }


_manager: Optional[SSHConfigManager] = None


def get_ssh_config_manager() -> SSHConfigManager:
    global _manager
    if _manager is None:
        _manager = SSHConfigManager()
    return _manager
