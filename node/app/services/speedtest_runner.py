"""Speed test runner for node.

Supports two methods:
  - iperf3: single-process with -P parallel streams and -w window size
  - ookla: Ookla Speedtest CLI on host via nsenter (auto-install)

Two test modes:
  - quick: fewer streams, shorter duration, nice priority
  - full: more streams, longer duration, accurate max throughput
"""

import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

IPERF3_BIN = shutil.which("iperf3") or "/usr/bin/iperf3"
NICE_BIN = shutil.which("nice") or "/usr/bin/nice"


class SpeedtestResult:
    def __init__(self):
        self.server: str = ""
        self.port: int = 5201
        self.download_mbps: float = 0.0
        self.upload_mbps: float = 0.0
        self.latency_ms: float = 0.0
        self.server_name: str = ""
        self.error: Optional[str] = None
        self.retransmits: int = 0

    def to_dict(self) -> dict:
        d = {
            "server": self.server,
            "port": self.port,
            "download_mbps": round(self.download_mbps, 2),
        }
        if self.upload_mbps > 0:
            d["upload_mbps"] = round(self.upload_mbps, 2)
        if self.latency_ms > 0:
            d["latency_ms"] = round(self.latency_ms, 2)
        if self.server_name:
            d["server_name"] = self.server_name
        if self.retransmits > 0:
            d["retransmits"] = self.retransmits
        if self.error:
            d["error"] = self.error
        return d


def _parse_iperf3_json(raw: str) -> tuple[float, int]:
    """Parse iperf3 JSON output, return (mbps, retransmits)."""
    data = json.loads(raw)

    if "error" in data:
        raise ValueError(data["error"])

    end = data.get("end", {})
    received = end.get("sum_received", {})
    bits_per_sec = received.get("bits_per_second", 0)

    sent = end.get("sum_sent", {})
    retransmits = sent.get("retransmits", 0)

    return bits_per_sec / 1_000_000, retransmits


async def _run_iperf3_process(
    host: str,
    port: int,
    duration: int,
    streams: int = 4,
    window_size: str = "4M",
    use_nice: bool = False,
) -> SpeedtestResult:
    """Run a single iperf3 process with -P parallel streams and -w window."""
    result = SpeedtestResult()
    result.server = host
    result.port = port

    iperf_args = [
        IPERF3_BIN,
        "-c", host,
        "-p", str(port),
        "-t", str(duration),
        "-P", str(streams),
        "-w", window_size,
        "-J",
        "--connect-timeout", "5000",
    ]

    if use_nice:
        cmd = [NICE_BIN, "-n", "19"] + iperf_args
    else:
        cmd = iperf_args

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=duration + 20,
        )

        if proc.returncode != 0 and not stdout:
            err_text = stderr.decode(errors="replace").strip()
            result.error = err_text[:200] if err_text else f"iperf3 exit code {proc.returncode}"
            return result

        raw_out = stdout.decode(errors="replace")
        mbps, retransmits = _parse_iperf3_json(raw_out)
        result.download_mbps = mbps
        result.retransmits = retransmits

    except asyncio.TimeoutError:
        result.error = "Test timed out"
    except json.JSONDecodeError:
        result.error = "Failed to parse iperf3 output"
    except ValueError as e:
        result.error = str(e)[:200]
    except FileNotFoundError:
        result.error = "iperf3 binary not found"
    except Exception as e:
        result.error = f"Unexpected error: {str(e)[:150]}"

    return result


async def run_iperf3_speedtest(
    servers: list[dict],
    duration: int = 5,
    streams: int = 4,
    window_size: str = "4M",
    threshold_mbps: float = 500.0,
    test_mode: str = "quick",
) -> dict:
    """Run iperf3 speed test — single process with -P parallel streams.

    test_mode:
      - "quick": 4 streams, 4M window, 5s, nice priority
      - "full": 16 streams, 8M window, 10s, no nice
    """
    is_full = test_mode == "full"
    use_nice = not is_full

    if is_full:
        effective_streams = max(streams, 16)
        effective_duration = max(duration, 10)
        effective_window = "8M"
    else:
        effective_streams = max(streams, 4)
        effective_duration = max(duration, 5)
        effective_window = window_size

    results: list[dict] = []
    best_speed = 0.0
    best_server = ""

    for srv in servers:
        host = srv.get("host", "")
        port = srv.get("port", 5201)

        if not host:
            continue

        logger.info(
            f"Speedtest [iperf3/{test_mode}]: {host}:{port} "
            f"(P={effective_streams}, w={effective_window}, t={effective_duration}s)"
        )
        result = await _run_iperf3_process(
            host, port, effective_duration,
            streams=effective_streams,
            window_size=effective_window,
            use_nice=use_nice,
        )

        results.append(result.to_dict())

        if result.error and result.download_mbps == 0:
            logger.warning(f"Speedtest {host}:{port} failed: {result.error}")
            continue

        logger.info(f"Speedtest {host}:{port}: {result.download_mbps:.1f} Mbit/s")

        if result.download_mbps > best_speed:
            best_speed = result.download_mbps
            best_server = f"{host}:{port}"

        if result.download_mbps >= threshold_mbps:
            break

    return {
        "best_speed_mbps": round(best_speed, 2),
        "best_server": best_server,
        "threshold_mbps": threshold_mbps,
        "ok": best_speed >= threshold_mbps,
        "test_mode": test_mode,
        "method": "iperf3",
        "results": results,
        "tested_at": datetime.now(timezone.utc).isoformat(),
    }


# --------------- Ookla Speedtest CLI ---------------

async def _install_ookla_cli() -> bool:
    """Auto-install Ookla Speedtest CLI on the host."""
    from app.services.host_executor import get_host_executor
    executor = get_host_executor()

    pkg_cmd = (
        "curl -s https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh | bash "
        "&& apt-get install -y speedtest 2>/dev/null"
    )
    result = await executor.execute(pkg_cmd, timeout=90, shell="bash")
    if result.success:
        logger.info("Ookla speedtest CLI installed via packagecloud")
        return True

    fallback_cmd = (
        "cd /tmp && "
        "curl -sL https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-x86_64.tgz -o speedtest.tgz && "
        "tar xzf speedtest.tgz && "
        "mv speedtest /usr/local/bin/speedtest && "
        "chmod +x /usr/local/bin/speedtest && "
        "rm -f speedtest.tgz speedtest.md"
    )
    result = await executor.execute(fallback_cmd, timeout=60, shell="bash")
    if result.success:
        logger.info("Ookla speedtest CLI installed via direct download")
        return True

    logger.warning("Failed to install Ookla speedtest CLI")
    return False


async def run_ookla_speedtest(
    test_mode: str = "quick",
    threshold_mbps: float = 500.0,
) -> dict:
    """Run Ookla Speedtest CLI on the host via nsenter.

    test_mode:
      - "quick": download only (--no-upload), ~15s
      - "full": download + upload, ~30s
    """
    from app.services.host_executor import get_host_executor
    executor = get_host_executor()

    check = await executor.execute(
        "which speedtest 2>/dev/null && speedtest --version 2>&1 | head -1",
        timeout=10,
    )

    if not check.success or "Speedtest by Ookla" not in (check.stdout or ""):
        installed = await _install_ookla_cli()
        if not installed:
            return {
                "best_speed_mbps": 0,
                "best_server": "",
                "threshold_mbps": threshold_mbps,
                "ok": False,
                "test_mode": test_mode,
                "method": "ookla",
                "error": "Ookla speedtest CLI not found and auto-install failed",
                "results": [],
                "tested_at": datetime.now(timezone.utc).isoformat(),
            }

    cmd = "speedtest --format=json --accept-license --accept-gdpr"
    if test_mode == "quick":
        cmd += " --no-upload"

    timeout = 60 if test_mode == "quick" else 120
    logger.info(f"Speedtest [ookla/{test_mode}]: running on host")

    result = await executor.execute(cmd, timeout=timeout, shell="bash")

    if not result.success:
        error = result.stderr or result.error or "Unknown error"
        logger.warning(f"Ookla speedtest failed: {error[:200]}")
        return {
            "best_speed_mbps": 0,
            "best_server": "",
            "threshold_mbps": threshold_mbps,
            "ok": False,
            "test_mode": test_mode,
            "method": "ookla",
            "error": f"speedtest failed: {error[:200]}",
            "results": [],
            "tested_at": datetime.now(timezone.utc).isoformat(),
        }

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return {
            "best_speed_mbps": 0,
            "best_server": "",
            "threshold_mbps": threshold_mbps,
            "ok": False,
            "test_mode": test_mode,
            "method": "ookla",
            "error": "Failed to parse speedtest JSON output",
            "results": [],
            "tested_at": datetime.now(timezone.utc).isoformat(),
        }

    dl_bw = data.get("download", {}).get("bandwidth", 0)
    ul_bw = data.get("upload", {}).get("bandwidth", 0)
    download_mbps = dl_bw * 8 / 1_000_000
    upload_mbps = ul_bw * 8 / 1_000_000

    server_info = data.get("server", {})
    server_host = server_info.get("host", "")
    server_name = server_info.get("name", "")
    server_location = server_info.get("location", "")
    if server_location:
        server_name = f"{server_name} ({server_location})"

    latency = data.get("ping", {}).get("latency", 0)

    result_entry = SpeedtestResult()
    result_entry.server = server_host
    result_entry.port = server_info.get("port", 0)
    result_entry.download_mbps = download_mbps
    result_entry.upload_mbps = upload_mbps
    result_entry.latency_ms = latency
    result_entry.server_name = server_name

    logger.info(
        f"Ookla speedtest: {download_mbps:.1f} Mbit/s down, "
        f"{upload_mbps:.1f} Mbit/s up, server: {server_name}"
    )

    return {
        "best_speed_mbps": round(download_mbps, 2),
        "upload_mbps": round(upload_mbps, 2),
        "best_server": server_host,
        "threshold_mbps": threshold_mbps,
        "ok": download_mbps >= threshold_mbps,
        "test_mode": test_mode,
        "method": "ookla",
        "results": [result_entry.to_dict()],
        "tested_at": datetime.now(timezone.utc).isoformat(),
    }


# --------------- Dispatcher ---------------

async def run_speedtest(
    servers: list[dict] | None = None,
    duration: int = 5,
    streams: int = 4,
    threshold_mbps: float = 500.0,
    test_mode: str = "quick",
    method: str = "iperf3",
    **_kwargs,
) -> dict:
    """Dispatch speed test to the appropriate method.

    method:
      - "iperf3": improved iperf3 with -P parallel streams and -w window
      - "ookla": Ookla Speedtest CLI on host
      - "auto": try ookla first, fallback to iperf3
    """
    if test_mode == "light":
        test_mode = "quick"

    if method == "ookla":
        return await run_ookla_speedtest(test_mode=test_mode, threshold_mbps=threshold_mbps)

    if method == "auto":
        ookla_result = await run_ookla_speedtest(test_mode=test_mode, threshold_mbps=threshold_mbps)
        if ookla_result.get("best_speed_mbps", 0) > 0:
            return ookla_result
        logger.info("Ookla failed, falling back to iperf3")

    if not servers:
        return {
            "best_speed_mbps": 0,
            "best_server": "",
            "threshold_mbps": threshold_mbps,
            "ok": False,
            "test_mode": test_mode,
            "method": method,
            "error": "No iperf3 servers provided",
            "results": [],
            "tested_at": datetime.now(timezone.utc).isoformat(),
        }

    return await run_iperf3_speedtest(
        servers=servers,
        duration=duration,
        streams=streams,
        threshold_mbps=threshold_mbps,
        test_mode=test_mode,
    )
