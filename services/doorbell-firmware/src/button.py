"""Doorbell button — GPIO press → host POST.

Runs as ``openring-button.service`` on the Pi.  Publishes nothing
locally (no Redis on the device) and owns no state beyond an in-memory
queue of unsent presses for the case where the host is briefly
unreachable.

Tests use ``gpiozero``'s ``MockFactory`` — see
``services/doorbell-firmware/tests/test_button.py``.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from common import load_settings, post_to_host

logger = logging.getLogger(__name__)

GPIO_PIN = int(os.environ.get("OPENRING_BUTTON_GPIO", "17"))
QUEUE_PATH = Path(os.environ.get("OPENRING_PRESS_QUEUE", "/var/lib/openring/queue.json"))
QUEUE_MAX = 16


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stdout,
    )


def _load_queue() -> deque[dict]:
    """Restore queued presses from disk so a service restart doesn't drop them."""
    if not QUEUE_PATH.exists():
        return deque(maxlen=QUEUE_MAX)
    try:
        items = json.loads(QUEUE_PATH.read_text())
        return deque(items[-QUEUE_MAX:], maxlen=QUEUE_MAX)
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not parse %s — starting fresh", QUEUE_PATH)
        return deque(maxlen=QUEUE_MAX)


def _save_queue(queue: deque[dict]) -> None:
    try:
        QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        QUEUE_PATH.write_text(json.dumps(list(queue)))
    except OSError:
        logger.exception("Failed to persist press queue")


def _flush(settings: dict[str, str], queue: deque[dict], lock: threading.Lock) -> None:
    """Send every queued press; drop on permanent (4xx) or success."""
    while True:
        with lock:
            if not queue:
                return
            press = queue[0]
        resp = post_to_host(settings, "/api/doorbell/press", press)
        if resp is None:
            # All retries exhausted — leave queued, try again later
            return
        if resp.ok or 400 <= resp.status_code < 500:
            # Success or unrecoverable — discard
            with lock:
                if queue and queue[0] == press:
                    queue.popleft()
                _save_queue(queue)
            if not resp.ok:
                logger.error(
                    "Discarding press queued for %s — host returned %d",
                    press.get("timestamp"), resp.status_code,
                )
        else:
            return


def main() -> None:
    _setup_logging()
    settings = load_settings()
    device_id = settings.get("DEVICE_ID", "front-door")
    logger.info("Button service starting (device_id=%s, gpio=%d)", device_id, GPIO_PIN)

    # Lazy import — gpiozero's pin factory selection has side effects we
    # don't want at module import time (and we need MockFactory in tests).
    from gpiozero import Button

    button = Button(GPIO_PIN, pull_up=True, bounce_time=0.05)

    queue = _load_queue()
    queue_lock = threading.Lock()

    def on_press() -> None:
        ts = datetime.now(timezone.utc).isoformat()
        press = {"timestamp": ts, "device_id": device_id}
        logger.info("Button pressed at %s", ts)
        with queue_lock:
            queue.append(press)
            _save_queue(queue)
        # Fire flush in a worker so the GPIO callback returns quickly
        threading.Thread(
            target=_flush, args=(settings, queue, queue_lock),
            name="press-flush", daemon=True,
        ).start()

    button.when_pressed = on_press

    shutdown = threading.Event()

    def _stop(_sig: int, _frame: object) -> None:
        logger.info("Shutdown signal received")
        shutdown.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # Background flusher: periodically retry queued presses (e.g.
    # WiFi was down at press time)
    def _periodic_flush() -> None:
        while not shutdown.wait(timeout=30.0):
            _flush(settings, queue, queue_lock)

    threading.Thread(target=_periodic_flush, name="press-retry", daemon=True).start()

    shutdown.wait()
    logger.info("Button service stopped")


if __name__ == "__main__":
    main()
