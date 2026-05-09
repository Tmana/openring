"""Periodic heartbeat → host POST.

Runs as ``openring-heartbeat.service`` (or as a thread inside the
button service — `pi-setup.sh` decides based on the resource budget).

Telemetry shape is intentionally small: just enough for the host to
render a live "doorbell ok" indicator and notice when the device
falls off the network.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

from common import load_settings, post_to_host

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = float(os.environ.get("OPENRING_HEARTBEAT_INTERVAL", "60"))


def _read_cpu_temp() -> float | None:
    """Return SoC temperature in degrees C, or None on non-Pi hardware."""
    candidates = [
        Path("/sys/class/thermal/thermal_zone0/temp"),
    ]
    for path in candidates:
        try:
            return int(path.read_text().strip()) / 1000.0
        except (OSError, ValueError):
            continue
    return None


def _read_wifi_rssi() -> int | None:
    """Best-effort RSSI in dBm from `iw`.  Returns None when unavailable."""
    try:
        out = subprocess.check_output(
            ["iw", "dev", "wlan0", "link"],
            timeout=2,
            stderr=subprocess.DEVNULL,
        ).decode()
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("signal:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _uptime_seconds() -> float:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError):
        return 0.0


def _build_payload(settings: dict[str, str]) -> dict:
    return {
        "device_id": settings.get("DEVICE_ID", "front-door"),
        "version": settings.get("VERSION", "dev"),
        "hostname": socket.gethostname(),
        "uptime_seconds": _uptime_seconds(),
        "cpu_temp_c": _read_cpu_temp(),
        "wifi_rssi_dbm": _read_wifi_rssi(),
    }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stdout,
    )
    settings = load_settings()
    logger.info(
        "Heartbeat service starting (device_id=%s, interval=%.0fs)",
        settings.get("DEVICE_ID", "?"), INTERVAL_SECONDS,
    )

    shutdown = threading.Event()

    def _stop(_sig: int, _frame: object) -> None:
        shutdown.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # Touch a healthcheck file so systemd / monitoring can confirm liveness
    health = Path("/tmp/openring-heartbeat-healthy")
    health.touch(exist_ok=True)

    while not shutdown.is_set():
        payload = _build_payload(settings)
        post_to_host(settings, "/api/doorbell/heartbeat", payload)
        try:
            health.touch(exist_ok=True)
        except OSError:
            pass
        if shutdown.wait(timeout=INTERVAL_SECONDS):
            break
    logger.info("Heartbeat service stopped")


if __name__ == "__main__":
    main()
