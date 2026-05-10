"""Audio-session JWT issuance.

The browser POSTs ``/api/audio/session`` with the device_id it wants
to talk to; the web service mints a one-shot HS256 JWT signed with
``OPENRING_AUDIO_KEY`` (loaded by ``shared/audio_jwt.py``).  The
audio-relay verifies the same JWT on the WebSocket upgrade.

Single-use enforcement is the audio-relay's responsibility (it
maintains a ``JtiSet`` of consumed jtis).  We just stamp a fresh
random jti per issuance.

Admin-only — this is the only OpenRing surface that gives a browser
the ability to push audio to a doorbell speaker, so we gate it
through the existing admin guard rather than handing tokens to the
viewer role.
"""

from __future__ import annotations

import logging
import os
import secrets

from audio_jwt import issue, load_key_from_env
from auth import get_db
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from route_auth import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/audio")

AUTH_DB_PATH = os.environ.get("AUTH_DB_PATH", "/data/auth.db")
SESSION_LIFETIME_SECONDS = 300  # 5 minutes — matches docs/AUDIO.md


def _audio_url_for(request: Request, jwt: str) -> str:
    """Build the ws(s):// URL the browser opens.

    The audio-relay listens on a separate port (default 8765 —
    overridable via ``AUDIO_RELAY_PORT``).  v0.3 exposes that port
    directly because Caddy proxying is blocked on a missing
    ``config/caddy-entrypoint.sh`` (a pre-existing v0.1 gap).  When
    that lands, this helper will collapse to a same-origin URL and
    Caddy will proxy ``/audio/*``.

    The hostname is taken from the request's Host header so the
    browser targets the same host it's already on — important for
    deployments behind Tailscale, mDNS, or NAT.
    """
    scheme = "wss" if request.url.scheme == "https" else "ws"
    host_header = request.headers.get("host", request.url.netloc)
    bare_host = host_header.split(":")[0]
    relay_port = os.environ.get("AUDIO_RELAY_PORT", "8765")
    return f"{scheme}://{bare_host}:{relay_port}/audio/browser?token={jwt}"


def _device_known(device_id: str) -> bool:
    """Confirm the device_id matches a paired doorbell.

    Refusing unknown device_ids stops a typo or a stale event id from
    minting a JWT that the audio-relay would later reject anyway —
    we'd rather fail fast with a clear 404.
    """
    db = get_db(AUTH_DB_PATH)
    try:
        row = db.execute(
            "SELECT 1 FROM device_tokens WHERE device_id=? AND disabled=0",
            (device_id,),
        ).fetchone()
        return row is not None
    finally:
        db.close()


@router.post("/session")
async def create_session(request: Request) -> Response:
    """Mint a one-shot audio JWT.

    Body::

        {"device_id": "front-door"}

    Response 200::

        {"audio_url": "wss://host/audio/browser?token=<jwt>",
         "expires_in_seconds": 300}

    Returns 503 when ``OPENRING_AUDIO_KEY`` isn't set on the host
    (audio is opt-in; setup.sh generates it on first run, so this
    only fires when the operator explicitly stripped it).
    """
    user_or_resp = require_admin(request, is_api=True)
    if not isinstance(user_or_resp, dict):
        return user_or_resp

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    device_id = str(body.get("device_id", "")).strip().lower()
    if not device_id:
        return JSONResponse({"error": "device_id required"}, status_code=400)

    if not _device_known(device_id):
        return JSONResponse(
            {"error": f"device_id {device_id!r} is not registered"},
            status_code=404,
        )

    key = load_key_from_env()
    if key is None:
        logger.error(
            "Cannot mint audio JWT — OPENRING_AUDIO_KEY not set.  "
            "Re-run setup.sh, or set the env var manually.",
        )
        return JSONResponse(
            {"error": "audio is disabled on this host (OPENRING_AUDIO_KEY missing)"},
            status_code=503,
        )

    jti = secrets.token_urlsafe(16)
    jwt = issue(
        key,
        sub=user_or_resp.get("username", "unknown"),
        device_id=device_id,
        jti=jti,
        lifetime_seconds=SESSION_LIFETIME_SECONDS,
    )
    audio_url = _audio_url_for(request, jwt)
    logger.info(
        "Issued audio session for device=%s by user=%s (jti=%s)",
        device_id, user_or_resp.get("username"), jti,
    )
    return JSONResponse({
        "audio_url": audio_url,
        "expires_in_seconds": SESSION_LIFETIME_SECONDS,
    })
