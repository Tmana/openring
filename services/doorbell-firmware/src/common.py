"""Shared HTTP client for the doorbell device.

Every device-side service that talks to the host (button, heartbeat,
audio relay) uses this module so retry / auth / telemetry policy lives
in exactly one place.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

SECRETS_FILE = Path(os.environ.get("OPENRING_SECRETS", "/etc/openring/secrets.env"))


def load_settings() -> dict[str, str]:
    """Parse the on-disk secrets file written by ``pi-setup.sh``.

    Format is ``KEY=value`` lines, no quoting.  Returns a dict so
    callers can pull individual fields without re-implementing the
    parser.  Missing file raises so the systemd unit fails loudly
    rather than silently degrading.
    """
    if not SECRETS_FILE.exists():
        raise FileNotFoundError(
            f"{SECRETS_FILE} missing — run pi-setup.sh first",
        )
    out: dict[str, str] = {}
    for line in SECRETS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def post_to_host(
    settings: dict[str, str],
    path: str,
    payload: dict,
    *,
    timeout: float = 10.0,
    max_retries: int = 5,
) -> requests.Response | None:
    """POST *payload* JSON to ``${HOST_BASE_URL}{path}`` with auth + retry.

    Returns the final ``Response`` on success, ``None`` after exhausting
    retries.  Backoff is exponential capped at 60 s — typical for a
    doorbell network where the brief WiFi hiccup is more common than a
    sustained outage.
    """
    base = settings.get("HOST_BASE_URL", "").rstrip("/")
    token = settings.get("DEVICE_TOKEN", "")
    if not base or not token:
        logger.error("Cannot POST: HOST_BASE_URL or DEVICE_TOKEN missing")
        return None

    url = f"{base}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": f"openring-device/{settings.get('VERSION', 'dev')}",
        "Content-Type": "application/json",
    }
    delay = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if resp.ok:
                return resp
            # 4xx errors won't be fixed by retry (auth, malformed, etc.) —
            # surface and bail.  5xx and network errors do retry.
            if 400 <= resp.status_code < 500:
                logger.error(
                    "POST %s rejected with %d: %s — not retrying",
                    path, resp.status_code, resp.text[:200],
                )
                return resp
            logger.warning(
                "POST %s returned %d (attempt %d/%d)",
                path, resp.status_code, attempt, max_retries,
            )
        except requests.RequestException as exc:
            logger.warning(
                "POST %s failed: %s (attempt %d/%d)",
                path, exc, attempt, max_retries,
            )
        if attempt < max_retries:
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
    return None
