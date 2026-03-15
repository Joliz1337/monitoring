"""iperf3 speed test runner for node.

Runs iperf3 tests against a list of servers and returns structured results.
Smart logic: stops early if speed is above threshold, tests all servers if below.
Two modes: light (low CPU, bandwidth-limited) and full (multi-process, max throughput).
"""

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

IPERF3_BIN = shutil.which("iperf3") or "/usr/bin/iperf3"


class SpeedtestResult:
    def __init__(self):
        self.server: str = ""
        self.port: int = 5201
        self.download_mbps: float = 0.0
        self.upload_mbps: float = 0.0
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
    streams: int = 1,
    bandwidth_limit: str = "",
    affinity: int = -1,
) -> SpeedtestResult:
    """Run a single iperf3 process."""
    result = SpeedtestResult()
    result.server = host
    result.port = port

    cmd = [
        IPERF3_BIN,
        "-c", host,
        "-p", str(port),
        "-t", str(duration),
        "-P", str(streams),
        "-J",
        "--connect-timeout", "5000",
    ]

    if bandwidth_limit:
        cmd.extend(["-b", bandwidth_limit])

    env = None
    if affinity >= 0:
        env = {**os.environ, "IPERF3_AFFINITY": str(affinity)}

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=duration + 15,
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


async def _run_multiprocess_test(
    host: str,
    port: int,
    duration: int,
    processes: int,
) -> SpeedtestResult:
    """Run N separate iperf3 processes in parallel against a server.

    Each process gets its own CPU core via affinity. iperf3 is single-threaded,
    so this is the only way to utilize multiple cores for bandwidth testing.
    Requires the server to support parallel connections (multiple ports or --parallel-streams).
    Falls back to single-process with -P if parallel fails.
    """
    cpu_count = os.cpu_count() or 1
    cores = list(range(min(processes, cpu_count)))

    tasks = [
        _run_iperf3_process(host, port, duration, streams=1, affinity=core)
        for core in cores
    ]

    sub_results = await asyncio.gather(*tasks, return_exceptions=True)

    total_mbps = 0.0
    total_retransmits = 0
    success_count = 0
    errors: list[str] = []

    for r in sub_results:
        if isinstance(r, Exception):
            errors.append(str(r)[:100])
            continue
        if r.error:
            errors.append(r.error)
            continue
        total_mbps += r.download_mbps
        total_retransmits += r.retransmits
        success_count += 1

    result = SpeedtestResult()
    result.server = host
    result.port = port

    if success_count > 0:
        result.download_mbps = total_mbps
        result.retransmits = total_retransmits
        if errors:
            result.error = f"{success_count}/{len(cores)} ok, errors: {'; '.join(errors[:2])}"
    elif success_count == 0 and len(cores) > 1:
        logger.info(f"Multiprocess failed for {host}:{port}, falling back to single process -P {processes}")
        return await _run_iperf3_process(host, port, duration, streams=processes)
    else:
        result.error = errors[0] if errors else "All processes failed"

    return result


async def run_speedtest(
    servers: list[dict],
    duration: int = 2,
    streams: int = 1,
    threshold_mbps: float = 500.0,
    bandwidth_limit: str = "",
    test_mode: str = "light",
) -> dict:
    """Run speed tests against a list of iperf3 servers.

    test_mode:
      - "light": 1 stream, bandwidth limited, low CPU usage
      - "full": multi-process (one per core), no bandwidth limit, accurate max speed
    """
    is_full = test_mode == "full"

    if not is_full and not bandwidth_limit:
        cap = int(threshold_mbps * 2)
        bandwidth_limit = f"{cap}M"

    if is_full:
        bandwidth_limit = ""

    results: list[dict] = []
    best_speed = 0.0
    best_server = ""

    for srv in servers:
        host = srv.get("host", "")
        port = srv.get("port", 5201)

        if not host:
            continue

        if is_full:
            procs = max(2, streams)
            logger.info(f"Speedtest [full]: {host}:{port} (duration={duration}s, processes={procs})")
            result = await _run_multiprocess_test(host, port, duration, procs)
        else:
            logger.info(f"Speedtest [light]: {host}:{port} (duration={duration}s, bw_limit={bandwidth_limit})")
            result = await _run_iperf3_process(host, port, duration, streams=1, bandwidth_limit=bandwidth_limit)

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
        "results": results,
        "tested_at": datetime.now(timezone.utc).isoformat(),
    }
