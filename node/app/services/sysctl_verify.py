"""Верификация системного тюнинга и вычистка чужих sysctl/limits-конфигов.

Числа тюнинга считает рендерер на самом хосте (`configs/tune-sysctl.sh`) и
оставляет их в `tuning-facts.json` — здесь только сверка живых значений ядра
с этим файлом и удаление конфигов, которые перебивали бы результат.
"""

import json
import logging

from app.services.host_files import read_host_file

logger = logging.getLogger(__name__)

TUNING_FACTS_PATH = "/opt/monitoring/configs/tuning-facts.json"

# Keys excluded from verification even though we set them, because another
# writer legitimately changes them at runtime:
#   rp_filter    — Xray's WireGuard outbound writes all/rp_filter=0 when a
#                  tunnel comes up; the next render sets it back to 2.
#   disable_ipv6 — same story (all/disable_ipv6=0).
# Asserting on these produced a permanent false failure on any node with an
# active WireGuard outbound.
VERIFY_EXCLUDED_KEYS = {
    "net.ipv4.conf.all.rp_filter",
    "net.ipv4.conf.default.rp_filter",
    "net.ipv6.conf.all.disable_ipv6",
    "net.ipv6.conf.default.disable_ipv6",
    "net.ipv6.conf.lo.disable_ipv6",
}


def expected_from_facts(facts: dict) -> dict:
    """Build the key->value set to assert on, from a parsed tuning-facts.json.

    Pure function, kept separate so the merge/exclusion rules are testable
    without a host executor. `static` wins over `computed` on a collision:
    static values are read back out of the rendered file, so they reflect what
    was actually written.
    """
    unsupported = set(facts.get("unsupported_keys") or [])
    expected = {}
    expected.update(facts.get("computed") or {})
    expected.update(facts.get("static") or {})
    return {
        k: v for k, v in expected.items()
        if k not in unsupported and k not in VERIFY_EXCLUDED_KEYS
    }


def _normalize_sysctl_value(value: str) -> str:
    """Collapse whitespace so a tab-separated kernel value compares equal.

    `sysctl -n net.ipv4.tcp_mem` returns its three fields TAB-separated while the
    config file uses single spaces. The previous exact-string comparison would
    have failed on tcp_mem, tcp_rmem, tcp_wmem, udp_mem and ip_local_port_range
    the moment any of them was added to the checked set.
    """
    return " ".join((value or "").split())


async def verify_sysctl_values(executor) -> dict:
    """Verify live kernel values against the facts file the renderer wrote.

    Expectations are per-host and computed, so they cannot be hardcoded here:
    the old flat table only worked because every key in it happened to have the
    same value in both profiles. Reading the facts file also means the node and
    install.sh agree by construction instead of by two copies of a constant.
    """
    verification = {"success": True, "checked": {}, "failed": []}

    raw = await read_host_file(TUNING_FACTS_PATH)
    if not raw:
        verification["success"] = False
        verification["failed"].append(
            "tuning-facts.json missing — this node predates the renderer; re-apply optimizations"
        )
        return verification

    try:
        facts = json.loads(raw)
    except (ValueError, TypeError) as e:
        verification["success"] = False
        verification["failed"].append(f"tuning-facts.json is not valid JSON: {e}")
        return verification

    expected = expected_from_facts(facts)

    if not expected:
        verification["success"] = False
        verification["failed"].append("tuning-facts.json contains no verifiable keys")
        return verification

    # One nsenter round-trip for every key instead of one per key: at ~28 keys
    # the old loop spawned 28 processes on each apply.
    keys = sorted(expected)
    script = "; ".join(
        f"printf '%s=%s\\n' {k} \"$(sysctl -n {k} 2>/dev/null || echo MISSING)\""
        for k in keys
    )
    result = await executor.execute(script, timeout=30, shell="bash")

    actual_values = {}
    if result.success and result.exit_code == 0:
        for line in (result.stdout or "").splitlines():
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            actual_values[key.strip()] = value.strip()
    else:
        verification["success"] = False
        verification["failed"].append("failed to read live sysctl values")
        return verification

    for key in keys:
        want = _normalize_sysctl_value(expected[key])
        got = _normalize_sysctl_value(actual_values.get(key, "MISSING"))
        verification["checked"][key] = {"expected": want, "actual": got}
        if want != got:
            verification["failed"].append(f"{key}: expected {want}, got {got}")
            verification["success"] = False

    # Threshold, not equality: the module may already carry a larger table, and
    # the expected size is profile- and RAM-dependent. install.sh reads the same
    # number from the same file, which is what removes the old disagreement
    # (524288/32768 there vs a flat 524288 here, false-failing panel+hybrid).
    want_hashsize = (facts.get("derived") or {}).get("conntrack_hashsize")
    if want_hashsize:
        res = await executor.execute(
            "cat /sys/module/nf_conntrack/parameters/hashsize", timeout=5
        )
        if res.success and res.exit_code == 0:
            try:
                hashsize = int((res.stdout or "0").strip())
            except ValueError:
                hashsize = 0
            verification["checked"]["conntrack_hashsize"] = {
                "expected": f">={want_hashsize}", "actual": str(hashsize)
            }
            if hashsize < int(want_hashsize):
                verification["failed"].append(
                    f"conntrack_hashsize: expected >={want_hashsize}, got {hashsize}"
                )
                verification["success"] = False

    verification["checked_count"] = len(verification["checked"])
    return verification


SYSTEM_SYSCTL_PATTERNS = {"10-", "99-sysctl.conf", "99-cloudimg-", "README"}
OUR_SYSCTL_CONFIG = "99-vless-tuning.conf"
THIRD_PARTY_SERVICES = [
    "3x-ui-tuning", "xray-tuning", "marzban-tuning",
    "network-optimize", "sysctl-tuning", "tcp-tuning", "tcp-bbr",
]
THIRD_PARTY_SCRIPTS = [
    "/usr/local/bin/network-tuning.sh", "/usr/local/bin/tcp-tuning.sh",
    "/usr/local/bin/sysctl-tuning.sh", "/opt/3x-ui/tuning.sh", "/opt/marzban/tuning.sh",
]


def _is_system_sysctl(filename: str) -> bool:
    return any(filename.startswith(p) for p in SYSTEM_SYSCTL_PATTERNS) or filename == OUR_SYSCTL_CONFIG


async def cleanup_conflicting_configs(executor) -> list[str]:
    """Remove ALL non-system sysctl/limits configs and third-party tuning services"""
    cleaned = []
    
    # ---- sysctl.d: remove all non-system configs ----
    ls_result = await executor.execute("ls /etc/sysctl.d/ 2>/dev/null", timeout=5)
    if ls_result.success and ls_result.stdout:
        for fname in ls_result.stdout.strip().split("\n"):
            fname = fname.strip()
            if not fname.endswith(".conf"):
                continue
            if _is_system_sysctl(fname):
                continue
            await executor.execute(f"rm -f /etc/sysctl.d/{fname}", timeout=5)
            cleaned.append(f"sysctl.d/{fname}")
    
    # ---- /etc/sysctl.conf: remove all active parameter lines ----
    result = await executor.execute(
        r"sed -i '/^net\./d; /^fs\./d; /^vm\./d; /^kernel\./d; /^precedence/d' /etc/sysctl.conf",
        timeout=5,
    )
    if result.success and result.exit_code == 0:
        cleaned.append("sysctl.conf (cleaned)")
    
    # ---- limits.d: remove all non-system configs ----
    ls_result = await executor.execute("ls /etc/security/limits.d/ 2>/dev/null", timeout=5)
    if ls_result.success and ls_result.stdout:
        for fname in ls_result.stdout.strip().split("\n"):
            fname = fname.strip()
            if not fname.endswith(".conf") or fname == "99-nofile.conf":
                continue
            await executor.execute(f"rm -f /etc/security/limits.d/{fname}", timeout=5)
            cleaned.append(f"limits.d/{fname}")
    
    # ---- /etc/security/limits.conf: clean custom nofile/nproc/memlock lines ----
    await executor.execute(
        r"sed -i '/^\*.*nofile/d; /^root.*nofile/d; /^\*.*nproc/d; /^root.*nproc/d; "
        r"/^\*.*memlock/d; /^root.*memlock/d' /etc/security/limits.conf",
        timeout=5,
    )
    
    # ---- Stop/disable third-party tuning services ----
    for svc in THIRD_PARTY_SERVICES:
        check = await executor.execute(f"systemctl is-enabled {svc}.service 2>/dev/null", timeout=5)
        if check.exit_code == 0:
            await executor.execute(f"systemctl stop {svc}.service", timeout=10)
            await executor.execute(f"systemctl disable {svc}.service", timeout=10)
            cleaned.append(f"service:{svc}")
    
    # ---- Remove third-party tuning scripts ----
    for script in THIRD_PARTY_SCRIPTS:
        check = await executor.execute(f"test -f {script}", timeout=3)
        if check.exit_code == 0:
            await executor.execute(f"rm -f {script}", timeout=5)
            cleaned.append(f"script:{script}")
    
    # ---- Clean crontab entries that apply sysctl ----
    cron_check = await executor.execute(
        "crontab -l 2>/dev/null | grep -qE 'sysctl|network-tun|tcp-tun'", timeout=5
    )
    if cron_check.exit_code == 0:
        await executor.execute(
            "crontab -l 2>/dev/null | grep -vE 'sysctl|network-tun|tcp-tun' | crontab -",
            timeout=5,
        )
        cleaned.append("crontab (cleaned)")
    
    return cleaned
