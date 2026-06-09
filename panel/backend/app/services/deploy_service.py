"""Авторазвёртывание ноды на удалённом сервере по SSH.

Панель подключается к серверу, скачивает install.sh и запускает его в режиме
`--unattended`: ставится нода мониторинга и, по желанию, WARP / нода Remnawave /
HTTP-прокси установщика. Пароль SSH живёт только в памяти на время установки.

Свежие образы OVH (и ряда других хостеров) отдают root с просроченным паролем —
PAM форсирует смену при первом входе. Без TTY команда не выполняется
(`Password change required but no TTY available`), поэтому при детекте этой
ситуации пароль автоматически меняется через интерактивную PTY-сессию, после
чего установка повторяется уже с новым паролем.
"""
from __future__ import annotations

import asyncio
import secrets
import shlex
import string
from dataclasses import dataclass
from typing import AsyncIterator

import asyncssh

INSTALLER_URL = "https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh"
REMOTE_SCRIPT = "/tmp/mon-install.sh"

# Установка с нуля (apt, docker, pull образов) укладывается в ~25 минут
DEPLOY_TIMEOUT = 1500
CONNECT_TIMEOUT = 30
# Интерактивная смена пароля — короткий диалог из трёх промптов
PWCHANGE_TIMEOUT = 60
PWCHANGE_READ_TIMEOUT = 8

# Признаки форсированной смены просроченного пароля (PAM/OpenSSH без TTY)
_EXPIRED_MARKERS = (
    "your password has expired",
    "password change required",
    "you are required to change your password",
    "no tty available",
)
# Признаки отклонения нового пароля во время смены
_PWCHANGE_FAIL_MARKERS = (
    "do not match",
    "password unchanged",
    "authentication token manipulation error",
    "no password supplied",
    "password is too simple",
    "bad password",
    "too short",
    "weak password",
)
_PWCHANGE_OK_MARKERS = (
    "password updated successfully",
    "password changed",
    "successfully changed",
    "all authentication tokens updated successfully",
)


@dataclass
class DeployParams:
    host: str
    ssh_port: int
    ssh_user: str
    node_secret: str
    panel_ip: str | None = None
    ssh_password: str | None = None
    ssh_private_key: str | None = None
    ssh_key_passphrase: str | None = None
    install_warp: bool = False
    install_remnawave: bool = False
    remnawave_cert: str | None = None
    proxy_url: str | None = None
    install_optimizations: bool = False
    opt_profile: str = "vpn"
    nic_mode: str = "auto"  # auto | multiqueue | hybrid | rps
    # Желаемый пароль root: если задан и текущий просрочен — ставится сразу при смене
    new_password: str | None = None


def _generate_strong_password(length: int = 20) -> str:
    """Случайный пароль, гарантированно с буквами разных регистров, цифрой и
    спецсимволом — чтобы пройти типовой pam_pwquality и отличаться от старого."""
    lowers, uppers, digits = string.ascii_lowercase, string.ascii_uppercase, string.digits
    specials = "!@#%^*-_=+"
    pools = lowers + uppers + digits + specials
    chars = [
        secrets.choice(lowers),
        secrets.choice(uppers),
        secrets.choice(digits),
        secrets.choice(specials),
        *(secrets.choice(pools) for _ in range(length - 4)),
    ]
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


def _looks_expired(line: str) -> bool:
    low = line.lower()
    return any(marker in low for marker in _EXPIRED_MARKERS)


def _build_inner_command(params: DeployParams) -> str:
    """Команда установки: скачать install.sh и запустить --unattended.

    Все значения env экранируются shlex.quote — защита от инъекций.
    """
    env: dict[str, str] = {
        "MON_INSTALL_NODE": "1",
        "NODE_SECRET": params.node_secret,
    }
    if params.panel_ip:
        env["PANEL_IP"] = params.panel_ip
    if params.proxy_url:
        env["MON_PROXY_URL"] = params.proxy_url
    if params.install_optimizations:
        env["MON_INSTALL_OPTIMIZATIONS"] = "1"
        env["MON_OPT_PROFILE"] = params.opt_profile
        if params.nic_mode and params.nic_mode != "auto":
            env["MON_NIC_MODE"] = params.nic_mode
    if params.install_warp:
        env["MON_INSTALL_WARP"] = "1"
    if params.install_remnawave:
        env["MON_INSTALL_REMNAWAVE"] = "1"
        if params.remnawave_cert:
            # install.sh ждёт сертификат с экранированными переводами строк
            cert = params.remnawave_cert.replace("\r\n", "\n").replace("\n", "\\n")
            env["REMNAWAVE_CERT"] = cert

    assignments = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
    return (
        f"curl -fsSL {shlex.quote(INSTALLER_URL)} -o {shlex.quote(REMOTE_SCRIPT)} && "
        f"{assignments} bash {shlex.quote(REMOTE_SCRIPT)} --unattended"
    )


def _build_command(params: DeployParams) -> str:
    """Оборачивает команду установки в sudo, если вход не под root."""
    inner = _build_inner_command(params)
    if params.ssh_user.strip() == "root":
        return inner
    # с паролем — sudo -S читает его из stdin; без пароля — sudo -n (нужен NOPASSWD)
    sudo_flag = "-S -p ''" if params.ssh_password else "-n"
    return f"sudo {sudo_flag} bash -c " + shlex.quote(inner)


def _connect_kwargs(params: DeployParams) -> dict:
    """Параметры asyncssh.connect. known_hosts=None — целевые серверы заранее
    неизвестны панели, ключ хоста не проверяем (оператор вводит доверенные креды)."""
    kwargs: dict = {
        "host": params.host,
        "port": params.ssh_port,
        "username": params.ssh_user,
        "known_hosts": None,
        "connect_timeout": CONNECT_TIMEOUT,
    }
    if params.ssh_private_key:
        key = asyncssh.import_private_key(
            params.ssh_private_key, params.ssh_key_passphrase or None
        )
        kwargs["client_keys"] = [key]
    elif params.ssh_password:
        kwargs["password"] = params.ssh_password
    return kwargs


async def _run_install_once(connect_kwargs: dict, params: DeployParams) -> AsyncIterator[dict]:
    """Один прогон установки. Стримит лог построчно, в конце отдаёт служебное
    событие {"type": "_result", "exit_code", "expired"}. Фатальные сбои
    соединения отдаются как {"type": "error"} без `_result`.
    """
    command = _build_command(params)
    needs_sudo = params.ssh_user.strip() != "root"
    expired = False

    try:
        async with asyncssh.connect(**connect_kwargs) as conn:
            yield {
                "type": "log",
                "line": f"[panel] SSH-подключение к {params.host}:{params.ssh_port} установлено",
            }
            process = await conn.create_process(command, stderr=asyncssh.STDOUT)

            if needs_sudo and params.ssh_password:
                process.stdin.write(params.ssh_password + "\n")

            loop = asyncio.get_event_loop()
            deadline = loop.time() + DEPLOY_TIMEOUT

            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    process.terminate()
                    yield {"type": "error", "message": "Превышен таймаут установки"}
                    return
                try:
                    line = await asyncio.wait_for(
                        process.stdout.readline(), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    process.terminate()
                    yield {"type": "error", "message": "Превышен таймаут установки"}
                    return
                if not line:
                    break
                stripped = line.rstrip("\n")
                if _looks_expired(stripped):
                    expired = True
                yield {"type": "log", "line": stripped}

            await process.wait()
            # returncode: код выхода (0..255) или отрицательный номер сигнала
            exit_code = process.returncode
            yield {
                "type": "_result",
                "exit_code": exit_code if exit_code is not None else 1,
                "expired": expired,
            }
    except asyncssh.PermissionDenied:
        yield {"type": "error", "message": "SSH: неверный логин, пароль или ключ"}
    except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
        yield {"type": "error", "message": f"Ошибка SSH-подключения: {exc}"}


def _wants_current_password(text: str) -> bool:
    return ("current" in text or "old password" in text) and "password" in text


def _wants_new_password(text: str) -> bool:
    return "new password" in text and not _wants_retype(text)


def _wants_retype(text: str) -> bool:
    return any(k in text for k in ("retype", "re-enter", "reenter", "confirm new"))


async def _change_expired_password(
    connect_kwargs: dict, old_password: str, new_password: str
) -> AsyncIterator[dict]:
    """Меняет просроченный пароль через PTY-логин: отвечает на промпты PAM
    (текущий → новый → повтор нового). Финальное событие — {"type": "pwchange",
    "ok": bool}. Главная проверка успеха — последующее переподключение с новым
    паролем, поэтому здесь достаточно отработать диалог и отсечь явный отказ.
    """
    try:
        async with asyncssh.connect(**connect_kwargs) as conn:
            process = await conn.create_process(term_type="ansi", term_size=(120, 40))
            answered_current = answered_new = answered_retype = False
            buffer = ""
            loop = asyncio.get_event_loop()
            deadline = loop.time() + PWCHANGE_TIMEOUT

            while loop.time() < deadline:
                try:
                    chunk = await asyncio.wait_for(
                        process.stdout.read(1024), timeout=PWCHANGE_READ_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    break  # промптов больше нет — диалог завершён
                if not chunk:
                    break  # сессия закрылась (часть систем требует релогин)
                buffer += chunk
                low = buffer.lower()

                if any(m in low for m in _PWCHANGE_FAIL_MARKERS):
                    yield {"type": "pwchange", "ok": False, "message": "сервер отклонил новый пароль"}
                    return
                if answered_retype and any(m in low for m in _PWCHANGE_OK_MARKERS):
                    yield {"type": "pwchange", "ok": True}
                    return

                # порядок важен: «retype new password» содержит «new password»
                if not answered_retype and _wants_retype(low):
                    process.stdin.write(new_password + "\n")
                    answered_retype, buffer = True, ""
                    continue
                if not answered_new and _wants_new_password(low):
                    process.stdin.write(new_password + "\n")
                    answered_new, buffer = True, ""
                    continue
                if not answered_current and _wants_current_password(low):
                    process.stdin.write(old_password + "\n")
                    answered_current, buffer = True, ""
                    continue
                # повторный запрос пароля после ответа на повтор = PAM отверг смену
                if answered_retype and (_wants_current_password(low) or _wants_new_password(low)):
                    yield {"type": "pwchange", "ok": False, "message": "смена пароля не принята"}
                    return

            # вышли по таймауту/EOF: если повтор отправлен — считаем смену выполненной
            ok = answered_retype
            yield {
                "type": "pwchange",
                "ok": ok,
                "message": None if ok else "не дождались промптов смены пароля",
            }
    except asyncssh.PermissionDenied:
        yield {"type": "pwchange", "ok": False, "message": "неверный текущий пароль"}
    except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
        yield {"type": "pwchange", "ok": False, "message": f"ошибка смены пароля: {exc}"}


async def deploy_node(params: DeployParams) -> AsyncIterator[dict]:
    """Подключается по SSH, ставит ноду и стримит лог установки построчно.

    При просроченном пароле (вход по паролю) автоматически меняет его и
    повторяет установку. Yields события: {"type": "log"|"done"|"error", ...}.
    """
    if not params.ssh_password and not params.ssh_private_key:
        yield {"type": "error", "message": "Не указан пароль или приватный ключ SSH"}
        return

    try:
        connect_kwargs = _connect_kwargs(params)
    except (asyncssh.KeyImportError, ValueError) as exc:
        yield {"type": "error", "message": f"Некорректный SSH-ключ: {exc}"}
        return

    # Сменить просроченный пароль можно только зная текущий — то есть при входе по паролю
    can_recover_password = bool(params.ssh_password) and not params.ssh_private_key
    password_changed = False

    while True:
        result: dict | None = None
        async for event in _run_install_once(connect_kwargs, params):
            if event.get("type") == "_result":
                result = event
                break
            yield event

        if result is None:
            return  # фатальная ошибка соединения уже отправлена в лог

        password_expired = result.get("expired") and result.get("exit_code", 1) != 0

        if password_expired and can_recover_password and not password_changed:
            new_password = params.new_password or _generate_strong_password()
            generated = not params.new_password
            yield {"type": "log", "line": "[panel] Пароль root просрочен — меняю через TTY (OVH)"}

            ok, message = False, None
            async for ev in _change_expired_password(connect_kwargs, params.ssh_password, new_password):
                if ev.get("type") == "pwchange":
                    ok, message = ev.get("ok", False), ev.get("message")
                    continue
                yield ev

            if not ok:
                yield {"type": "error", "message": f"Не удалось сменить просроченный пароль: {message or 'неизвестная ошибка'}"}
                return

            if generated:
                yield {"type": "log", "line": f"[panel] ⚠ Установлен новый пароль root: {new_password} — сохраните его"}
            else:
                yield {"type": "log", "line": "[panel] Пароль root изменён на заданный"}

            params.ssh_password = new_password
            connect_kwargs = _connect_kwargs(params)
            password_changed = True
            continue  # повторяем установку с новым паролем

        if password_expired and not can_recover_password:
            yield {"type": "log", "line": "[panel] Пароль просрочен — авто-смена доступна только при входе по паролю"}

        yield {"type": "done", "exit_code": result.get("exit_code", 1)}
        return
