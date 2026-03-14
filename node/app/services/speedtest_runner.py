"""iperf3 speed test runner for node.

Runs iperf3 tests against a list of servers and returns structured results.
Smart logic: stops early if speed is above threshold, tests all servers if below.
"""

import asyncio
import json
import logging
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
    # Receiver side gives the actual throughput
    received = end.get("sum_received", {})
    bits_per_sec = received.get("bits_per_second", 0)

    sent = end.get("sum_sent", {})
    retransmits = sent.get("retransmits", 0)

    return bits_per_sec / 1_000_000, retransmits


async def _run_single_test(
    host: str,
    port: int,
    duration: int,
    streams: int,
) -> SpeedtestResult:
    """Run a single iperf3 test against one server."""
    result = SpeedtestResult()
    result.server = host
    result.port = port

    cmd = [
        IPERF3_BIN,
        "-c", host,
        "-p", str(port),
        "-t", str(duration),
        "-P", str(streams),
        "-J",                   # JSON output
        "--connect-timeout", "5000",  # 5s connect timeout (ms)
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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


async def run_speedtest(
    servers: list[dict],
    duration: int = 3,
    streams: int = 4,
    threshold_mbps: float = 500.0,
) -> dict:
    """Run speed tests against a list of iperf3 servers.

    Logic:
    - Test servers sequentially
    - If first server gives >= threshold -> stop, return result
    - If < threshold -> test remaining servers to confirm it's a node issue
    - Returns all results with the best one highlighted
    """
    results: list[dict] = []
    best_speed = 0.0
    best_server = ""

    for srv in servers:
        host = srv.get("host", "")
        port = srv.get("port", 5201)

        if not host:
            continue

        logger.info(f"Speedtest: testing {host}:{port} (duration={duration}s, streams={streams})")
        result = await _run_single_test(host, port, duration, streams)
        results.append(result.to_dict())

        if result.error:
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
        "results": results,
        "tested_at": datetime.now(timezone.utc).isoformat(),
    }
