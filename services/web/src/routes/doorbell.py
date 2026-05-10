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

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import auth as auth_module
import config_store
import db
import redis.asyncio as aioredis
from event_signing import load_key_from_env, sign_event
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from route_auth import require_admin, require_viewer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/doorbell")

DOORBELL_CHANNEL = "openring:doorbell"
SNAPSHOT_REQUEST_CHANNEL = "openring:snapshot:request"
SNAPSHOT_TIMEOUT_SECONDS = 5.0
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
    """Record a button press: grab a snapshot, write an event row, publish.

    Returns 202 — fast path so the Pi-side button service can return
    quickly.  All of the side effects (snapshot RPC to the detector,
    detection_events row insert, HMAC-signed publish on
    ``openring:doorbell``) happen synchronously here so a successful
    response means the press is fully committed.

    Snapshot capture failure is non-fatal: we still record the press
    with ``snapshot_path=None`` rather than dropping it on the floor.
    A doorbell press with no image is still better signal than no event
    at all (someone *did* push the button).
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
    received_at = datetime.now(timezone.utc)
    received_at_iso = received_at.isoformat()
    device_ts = str(body.get("timestamp", "") or received_at_iso)
    device_id = device["device_id"]
    label = device.get("label") or device_id

    # Update last_seen but DON'T overwrite real heartbeat telemetry —
    # a press doesn't carry an uptime/temperature payload.
    db_conn = auth_module.get_db(AUTH_DB_PATH)
    try:
        auth_module.touch_device(db_conn, device_id)
    finally:
        db_conn.close()

    cfg = config_store.load_cached()
    cfg_redis = (cfg.get("redis") or {})
    redis_host = cfg_redis.get("host", "redis")
    redis_port = int(cfg_redis.get("port", 6379))
    redis_password = os.environ.get("REDIS_PASSWORD", "") or None

    # Open a single Redis connection used for the snapshot RPC and the
    # final publish, so we don't pay a connect twice in the hot path.
    client = aioredis.Redis(
        host=redis_host,
        port=redis_port,
        password=redis_password,
        decode_responses=True,
    )
    try:
        snapshot_path = await _grab_snapshot(client, device_id)

        # The doorbell device_id is convention-matched to a camera name
        # in scarguard.yml — a doorbell is its own camera.  Fall back to
        # the global notifications config when no per-camera rules are
        # set, mirroring the detector's behaviour.
        actions_triggered = _resolve_actions(cfg, device_id)

        event_id, feedback_token = db.insert_doorbell_event(
            camera_name=device_id,
            snapshot_path=snapshot_path,
            actions_triggered=actions_triggered,
            timestamp=received_at,
        )

        payload: dict[str, Any] = {
            "type": "doorbell_press",
            "event_id": event_id,
            "device_id": device_id,
            "label": label,
            "camera_name": device_id,
            "class_name": "doorbell_press",
            "confidence": 1.0,
            "timestamp": received_at_iso,
            "device_timestamp": device_ts,
            "snapshot_path": snapshot_path,
            "feedback_token": feedback_token,
            "actions_triggered": actions_triggered,
        }

        # HMAC-sign so the notifier can reject spoofed publishes from
        # anything else on the internal Redis bus.
        hmac_key = load_key_from_env()
        if hmac_key is not None:
            payload = sign_event(payload, hmac_key)

        await client.publish(DOORBELL_CHANNEL, json.dumps(payload, default=str))
    finally:
        # aclose() is the async-correct cleanup added in redis-py 5.0.1+.
        # Runtime is fine on the pinned redis==7.4.0; the type-stub
        # package (types-redis) lags and reports the method as missing.
        await client.aclose()  # type: ignore[attr-defined, unused-ignore]

    logger.info(
        "Doorbell press: device=%s event_id=%d snapshot=%s actions=%s",
        device_id, event_id, snapshot_path, actions_triggered,
    )
    return JSONResponse({
        "ok": True,
        "event_id": event_id,
        "snapshot_path": snapshot_path,
    }, status_code=202)


async def _grab_snapshot(
    client: aioredis.Redis,
    camera_name: str,
) -> str | None:
    """Request a single frame from the detector via Redis RPC.

    Returns the absolute snapshot path on success, ``None`` on timeout
    or detector error.  We deliberately never raise from here — a
    missing snapshot is logged but does not fail the press as a whole.
    """
    request_id = uuid.uuid4().hex
    result_channel = f"openring:snapshot:result:{request_id}"
    pubsub = client.pubsub()
    try:
        await pubsub.subscribe(result_channel)
        await client.publish(
            SNAPSHOT_REQUEST_CHANNEL,
            json.dumps({"camera_name": camera_name, "request_id": request_id}),
        )
        deadline = SNAPSHOT_TIMEOUT_SECONDS
        elapsed = 0.0
        while elapsed < deadline:
            msg = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0,
            )
            if msg and msg.get("type") == "message":
                try:
                    result = json.loads(msg["data"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if result.get("ok"):
                    path = result.get("snapshot_path")
                    return str(path) if path else None
                logger.warning(
                    "Snapshot grab failed for %s: %s",
                    camera_name, result.get("error", "unknown"),
                )
                return None
            elapsed += 1.0
            await asyncio.sleep(0)
        logger.warning(
            "Snapshot grab timed out for %s after %.1fs",
            camera_name, deadline,
        )
        return None
    except Exception:
        logger.exception("Snapshot grab failed for %s", camera_name)
        return None
    finally:
        try:
            await pubsub.unsubscribe(result_channel)
            await pubsub.aclose()  # type: ignore[attr-defined, unused-ignore]
        except Exception:
            pass


def _resolve_actions(cfg: dict, device_id: str) -> list[str] | None:
    """Apply the camera-side notification_rules for ``doorbell_press``.

    Semantics mirror ``services/detector/src/main.py:_match_notification_rules``:
      * camera not configured → ``[]`` (notify all enabled channels)
      * camera has rules but none match → ``None`` (suppress)
      * matching rule → list of channel names
    """
    cameras = cfg.get("cameras") or []
    cam_cfg: dict | None = None
    for c in cameras:
        if isinstance(c, dict) and c.get("name") == device_id:
            cam_cfg = c
            break
    if cam_cfg is None:
        return []
    rules = cam_cfg.get("notification_rules") or []
    if not rules:
        return []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule_class = rule.get("class_name", "*")
        if rule_class == "*" or rule_class == "doorbell_press":
            return list(rule.get("channels", []))
    return None


