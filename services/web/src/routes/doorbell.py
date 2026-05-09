"""Doorbell device API — register, press, heartbeat, admin pairing.

These endpoints are called by the Pi-side ``services/doorbell-firmware``
package.  The wire shape is intentionally minimal so the Pi-side stays
small.

Auth model
----------

* ``POST /api/doorbell/register`` — *no* Bearer auth.  Gated by a 5-minute
  pairing window the operator opens from the web UI.  Returns a fresh
  device token.  Idempotent: re-registering an existing device_id during
  an open window rotates that device's token.

* ``POST /api/doorbell/press`` — Bearer auth via the token returned by
  ``register``.  Publishes an HMAC-signed event on
  ``openring:doorbell`` for the notifier to dispatch.

* ``POST /api/doorbell/heartbeat`` — Bearer auth via the same token.
  Records last_seen + telemetry; fires no notification.

* Admin endpoints under ``/api/doorbell/pair-window/...`` and
  ``/api/doorbell/devices`` are session-authenticated like the rest of
  the admin UI.

Note: the auth middleware in main.py treats this prefix as public
(see ``_PUBLIC_PREFIXES``).  Each route enforces its own auth model.
The CSRF middleware also skips this prefix because mutating requests
arrive from non-browser clients with Bearer tokens or pairing-window
gating instead of cookies.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import auth as auth_module
import db
import redis.asyncio as aioredis
from event_signing import load_key_from_env, sign_event
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from route_auth import require_admin, require_viewer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/doorbell")

DOORBELL_CHANNEL = "openring:doorbell"
AUTH_DB_PATH = os.environ.get("AUTH_DB_PATH", "/data/auth.db")

# device_id format mirrors a hostname: lowercase letters, digits, dashes.
# Length cap defends the SQLite UNIQUE index from accidental megabytes.
_DEVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=400)


def _unauthorized(message: str = "invalid or missing device token") -> JSONResponse:
    return JSONResponse({"error": message}, status_code=401)


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def _device_from_request(request: Request) -> dict[str, Any] | None:
    """Validate the Bearer token and return the device dict, or None."""
    raw = _bearer_token(request)
    if raw is None:
        return None
    db_conn = auth_module.get_db(AUTH_DB_PATH)
    try:
        return auth_module.validate_device_token(db_conn, raw)
    finally:
        db_conn.close()


# ── Pairing window admin (admin-only) ─────────────────────────────────────


@router.post("/pair-window/open")
async def pair_window_open(request: Request) -> Response:
    """Open the 5-minute doorbell pairing window."""
    user_or_resp = require_admin(request, is_api=True)
    if not isinstance(user_or_resp, dict):
        return user_or_resp
    expires_at = db.open_pairing_window()
    logger.info("Doorbell pairing window opened by %s until %s",
                user_or_resp.get("username", "?"), expires_at)
    return JSONResponse({
        "expires_at": expires_at,
        "duration_seconds": db.DEFAULT_PAIRING_WINDOW_SECONDS,
    })


@router.post("/pair-window/close")
async def pair_window_close(request: Request) -> Response:
    """Force-close the pairing window."""
    user_or_resp = require_admin(request, is_api=True)
    if not isinstance(user_or_resp, dict):
        return user_or_resp
    db.close_pairing_window()
    return JSONResponse({"ok": True})


@router.get("/pair-window")
async def pair_window_status(request: Request) -> Response:
    """Read-only: viewer or admin can see whether a window is open."""
    user_or_resp = require_viewer(request, is_api=True)
    if not isinstance(user_or_resp, dict):
        return user_or_resp
    return JSONResponse({
        "open": db.is_pairing_window_open(),
        "expires_at": db.pairing_window_expires_at(),
    })


# ── Device registry (viewer/admin) ────────────────────────────────────────


@router.get("/devices")
async def list_devices(request: Request) -> Response:
    user_or_resp = require_viewer(request, is_api=True)
    if not isinstance(user_or_resp, dict):
        return user_or_resp
    db_conn = auth_module.get_db(AUTH_DB_PATH)
    try:
        devices = auth_module.list_devices(db_conn)
    finally:
        db_conn.close()
    # Strip last_telemetry to a parsed dict so the client doesn't double-decode.
    for d in devices:
        raw = d.get("last_telemetry")
        if isinstance(raw, str) and raw:
            try:
                d["last_telemetry"] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                d["last_telemetry"] = None
    return JSONResponse({"devices": devices})


@router.delete("/devices/{device_id}")
async def remove_device(request: Request, device_id: str) -> Response:
    user_or_resp = require_admin(request, is_api=True)
    if not isinstance(user_or_resp, dict):
        return user_or_resp
    db_conn = auth_module.get_db(AUTH_DB_PATH)
    try:
        deleted = auth_module.delete_device(db_conn, device_id)
    finally:
        db_conn.close()
    if not deleted:
        return JSONResponse({"error": "device not found"}, status_code=404)
    logger.info("Device %s removed by %s", device_id, user_or_resp.get("username", "?"))
    return JSONResponse({"ok": True})


# ── Device-side endpoints ─────────────────────────────────────────────────


@router.post("/register")
async def register(request: Request) -> Response:
    """Pair a new device.  No Bearer auth — gated by the pairing window.

    Body::
      {"device_id": "front-door", "label": "Front door"}

    Response 200::
      {"device_id": "front-door", "device_token": "<raw>",
       "registered_at": "<utc-iso>"}

    The returned token is shown to the operator exactly once.  Server-side
    only the SHA-256 hash is persisted.  If the pairing window is closed
    we return 403 with a clear message so ``pi-setup.sh`` can prompt the
    operator to open one.
    """
    if not db.is_pairing_window_open():
        return JSONResponse(
            {"error": "pairing window is not open — open one from the web UI"},
            status_code=403,
        )
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _bad_request("invalid JSON body")
    if not isinstance(body, dict):
        return _bad_request("body must be a JSON object")
    device_id = str(body.get("device_id", "")).strip().lower()
    label = str(body.get("label", "")).strip() or device_id
    if not _DEVICE_ID_RE.match(device_id):
        return _bad_request(
            "device_id must be lowercase alphanumerics + dashes, "
            "starting with a letter or digit, max 63 chars",
        )
    db_conn = auth_module.get_db(AUTH_DB_PATH)
    try:
        raw_token = auth_module.create_device_token(db_conn, device_id, label)
    finally:
        db_conn.close()
    logger.info("Device registered: device_id=%s label=%s", device_id, label)
    return JSONResponse({
        "device_id": device_id,
        "label": label,
        "device_token": raw_token,
        "registered_at": datetime.now(timezone.utc).isoformat(),
    })


@router.post("/heartbeat")
async def heartbeat(request: Request) -> Response:
    """Record device telemetry.  Bearer auth required."""
    device = _device_from_request(request)
    if device is None:
        return _unauthorized()
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _bad_request("invalid JSON body")
    if not isinstance(body, dict):
        return _bad_request("body must be a JSON object")
    db_conn = auth_module.get_db(AUTH_DB_PATH)
    try:
        auth_module.touch_device(
            db_conn,
            device["device_id"],
            telemetry_json=json.dumps(body, default=str),
        )
    finally:
        db_conn.close()
    return JSONResponse({"ok": True}, status_code=200)


@router.post("/press")
async def press(request: Request) -> Response:
    """Record a button press and publish on ``openring:doorbell``.

    Returns 202 — the notifier picks the event up over Redis and any
    snapshot capture / event-row insert is the detector's job
    (ROADMAP issue #4).
    """
    device = _device_from_request(request)
    if device is None:
        return _unauthorized()
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _bad_request("invalid JSON body")
    if not isinstance(body, dict):
        return _bad_request("body must be a JSON object")

    # Trust the device's reported press timestamp for telemetry only;
    # use the host's clock as the authoritative "received at" so a
    # Pi with a misset clock can't backdate events.
    received_at = datetime.now(timezone.utc).isoformat()
    device_ts = str(body.get("timestamp", "") or received_at)

    # Update last_seen but DON'T overwrite real heartbeat telemetry —
    # a press doesn't carry a uptime/temperature payload.
    db_conn = auth_module.get_db(AUTH_DB_PATH)
    try:
        auth_module.touch_device(db_conn, device["device_id"])
    finally:
        db_conn.close()

    payload: dict[str, Any] = {
        "type": "doorbell_press",
        "device_id": device["device_id"],
        "label": device.get("label") or device["device_id"],
        "timestamp": received_at,
        "device_timestamp": device_ts,
    }

    # HMAC-sign so the notifier can reject spoofed publishes from anything
    # else on the internal Redis bus.
    hmac_key = load_key_from_env()
    if hmac_key is not None:
        payload = sign_event(payload, hmac_key)

    cfg_redis = _redis_cfg(request)
    client = aioredis.Redis(
        host=cfg_redis["host"],
        port=cfg_redis["port"],
        password=os.environ.get("REDIS_PASSWORD", "") or None,
        decode_responses=True,
    )
    try:
        await client.publish(DOORBELL_CHANNEL, json.dumps(payload, default=str))
    finally:
        await client.aclose()

    logger.info("Doorbell press from %s", device["device_id"])
    return JSONResponse({"ok": True}, status_code=202)


def _redis_cfg(_request: Request) -> dict[str, Any]:
    """Read host/port from the cached YAML config; defaults match docker-compose."""
    from config_store import load_cached
    cfg = (load_cached().get("redis") or {})
    return {
        "host": cfg.get("host", "redis"),
        "port": int(cfg.get("port", 6379)),
    }
