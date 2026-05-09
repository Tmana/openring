"""Background watchdog that flips paired doorbells offline / online.

Web is the authoritative writer of ``device_tokens.last_seen_at``
(it's stamped by the heartbeat endpoint), so it's also the right
place to detect when a device has stopped phoning home.

The watchdog runs as a daemon thread started at app startup.  It
polls device rows on a fixed interval; on every transition (online →
offline or offline → online) it HMAC-signs and publishes a payload to
``openring:device`` for the notifier to dispatch.

Per-device timeout precedence (highest first):
  1. ``doorbells[].heartbeat_timeout_seconds`` for that device_id
  2. ``system.heartbeat_timeout_seconds``
  3. Module default 90s

The watchdog deliberately doesn't write to a "device_state" column
or to app_state; previous-state lives in an in-memory dict.  A web
restart picks up wherever the devices are NOW; any actually-offline
device will fire one ``device_offline`` event shortly after restart,
which is the same UX as a fresh install.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import auth as auth_module
import config_store
import redis as redis_lib
from event_signing import load_key_from_env, sign_event

logger = logging.getLogger(__name__)

DEVICE_CHANNEL = "openring:device"
DEFAULT_TIMEOUT_SECONDS = 90.0
POLL_INTERVAL_SECONDS = 15.0


def _redis_client(cfg: dict) -> redis_lib.Redis | None:
    """Build a sync Redis client from the cached config.  None on failure."""
    redis_cfg = (cfg.get("redis") or {})
    try:
        return redis_lib.Redis(
            host=redis_cfg.get("host", "redis"),
            port=int(redis_cfg.get("port", 6379)),
            password=os.environ.get("REDIS_PASSWORD", "") or None,
            decode_responses=True,
            socket_connect_timeout=3,
        )
    except Exception:
        logger.exception("watchdog: failed to construct redis client")
        return None


def _per_device_timeout(cfg: dict, device_id: str) -> float:
    """Resolve the heartbeat timeout for *device_id* from cached config."""
    system_default = float(
        (cfg.get("system") or {}).get("heartbeat_timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
    )
    for entry in cfg.get("doorbells") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("device_id") == device_id:
            override = entry.get("heartbeat_timeout_seconds")
            if override is not None:
                try:
                    return float(override)
                except (TypeError, ValueError):
                    pass
            break
    return system_default


def _publish(client: redis_lib.Redis, payload: dict, hmac_key: bytes | None) -> None:
    if hmac_key is not None:
        payload = sign_event(payload, hmac_key)
    try:
        client.publish(DEVICE_CHANNEL, json.dumps(payload, default=str))
    except redis_lib.RedisError:
        logger.warning("watchdog: redis publish failed", exc_info=True)


def _evaluate_once(
    cfg: dict,
    state: dict[str, str],
    client: redis_lib.Redis,
    hmac_key: bytes | None,
) -> None:
    """One pass: enumerate devices, decide transitions, publish + update state.

    *state* maps device_id → ``"online"`` | ``"offline"``.  Mutated in place.
    Devices unknown to *state* are bootstrapped without firing — we'd
    rather miss a bootstrap-time alert than fan one out to every
    notification channel on web restart.
    """
    db = auth_module.get_db()
    try:
        devices = auth_module.list_devices(db)
    finally:
        db.close()

    now = datetime.now(timezone.utc)
    for d in devices:
        device_id = d.get("device_id", "")
        if not device_id:
            continue
        last_seen_iso = d.get("last_seen_at")
        timeout = _per_device_timeout(cfg, device_id)

        is_online = False
        offline_seconds = None
        if last_seen_iso:
            try:
                last_seen = datetime.fromisoformat(last_seen_iso)
            except (ValueError, TypeError):
                last_seen = None
            if last_seen is not None:
                offline_seconds = (now - last_seen).total_seconds()
                is_online = offline_seconds <= timeout
        new_state = "online" if is_online else "offline"
        prev_state = state.get(device_id)

        if prev_state is None:
            # Bootstrap — record without publishing.
            state[device_id] = new_state
            continue

        if new_state == prev_state:
            continue

        # Transition.  Publish + update state.
        if new_state == "offline":
            payload = {
                "type": "device_offline",
                "device_id": device_id,
                "label": d.get("label") or device_id,
                "last_seen_at": last_seen_iso,
                "offline_seconds": offline_seconds,
                "timeout_seconds": timeout,
                "timestamp": now.isoformat(),
            }
            logger.warning(
                "Device %s offline (last seen %ss ago, timeout %.0fs)",
                device_id,
                f"{offline_seconds:.0f}" if offline_seconds is not None else "?",
                timeout,
            )
        else:
            payload = {
                "type": "device_recovered",
                "device_id": device_id,
                "label": d.get("label") or device_id,
                "last_seen_at": last_seen_iso,
                "timestamp": now.isoformat(),
            }
            logger.info("Device %s recovered", device_id)
        _publish(client, payload, hmac_key)
        state[device_id] = new_state


def start_watchdog(stop_event: threading.Event | None = None) -> threading.Thread:
    """Start the watchdog as a daemon thread.

    *stop_event* lets tests + a clean-shutdown hook break the loop;
    when None we never stop (the daemon dies with the process).
    """
    state: dict[str, str] = {}

    def _run() -> None:
        hmac_key = load_key_from_env()
        if hmac_key is None:
            logger.warning(
                "DETECTION_HMAC_KEY not set — device offline/recovered events will be unsigned",
            )
        client = _redis_client(config_store.load_cached())
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                cfg = config_store.load_cached()
                if client is None:
                    client = _redis_client(cfg)
                if client is not None:
                    _evaluate_once(cfg, state, client, hmac_key)
            except Exception:
                logger.exception("watchdog: unexpected error in evaluate loop")
                # Drop client so the next iteration tries to reconnect
                client = None
            if stop_event is not None:
                if stop_event.wait(timeout=POLL_INTERVAL_SECONDS):
                    break
            else:
                time.sleep(POLL_INTERVAL_SECONDS)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    thread = threading.Thread(target=_run, name="heartbeat-watchdog", daemon=True)
    thread.start()
    logger.info("Heartbeat watchdog started (poll=%.0fs)", POLL_INTERVAL_SECONDS)
    return thread
