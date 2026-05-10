"""OpenRing audio-relay — paires Pi WebSockets with browser WebSockets
and forwards Opus frames between them.

Two URL paths::

    /audio/device     Pi side.  Bearer-authed against device_tokens
                      in /data/auth.db.  One concurrent connection
                      per device_id.
    /audio/browser    Browser side.  ?token=<jwt> parameter signed
                      by OPENRING_AUDIO_KEY; verified before upgrade.
                      One concurrent connection per device_id.

Once paired, every AUDIO frame received from one side is forwarded
unchanged to the other.  STATE frames go through the floor arbiter
(``floor.py``).  See ``docs/AUDIO.md`` for the wire format.

Stateless beyond the per-process maps (no SQLite writes here).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from urllib.parse import parse_qs, urlparse

import audio_frames as frames
import auth as audio_auth
import websockets
from auth_db import get_device_id_for_token  # noqa: F401  (helper imported by tests)
from floor import Floor, Holder
from websockets.legacy.server import WebSocketServerProtocol

logger = logging.getLogger(__name__)

LISTEN_HOST = os.environ.get("AUDIO_RELAY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("AUDIO_RELAY_PORT", "8765"))


# ── Per-process state ────────────────────────────────────────────────


class RelayState:
    """Maps the active Pi + browser sessions and the per-device floors.

    One RelayState per audio-relay process — `main()` constructs it
    and the WebSocket handler closures capture a reference.
    """

    def __init__(self) -> None:
        self.pis: dict[str, WebSocketServerProtocol] = {}
        self.browsers: dict[str, tuple[str, WebSocketServerProtocol]] = {}
        # Maps device_id → Floor.  Lazily created on first pairing.
        self.floors: dict[str, Floor] = {}
        self.jtis = audio_auth.JtiSet()

    def floor_for(self, device_id: str) -> Floor:
        f = self.floors.get(device_id)
        if f is None:
            f = Floor()
            self.floors[device_id] = f
        return f


# ── Pi-side handshake helpers ────────────────────────────────────────


def _bearer_from_request(headers) -> str | None:
    """Extract Bearer token from the upgrade-request Authorization header."""
    auth_header = headers.get("Authorization", "")
    if isinstance(auth_header, list):
        auth_header = auth_header[0] if auth_header else ""
    if not isinstance(auth_header, str):
        return None
    if not auth_header.lower().startswith("bearer "):
        return None
    return auth_header[7:].strip() or None


def _jwt_from_query(path: str) -> str | None:
    """Extract token=… from the upgrade-request query string."""
    parsed = urlparse(path)
    qs = parse_qs(parsed.query)
    values = qs.get("token", [])
    return values[0] if values else None


# ── Frame forwarding ─────────────────────────────────────────────────


async def _forward(
    sender: WebSocketServerProtocol,
    receiver: WebSocketServerProtocol,
    floor: Floor,
    sender_role: Holder,
) -> None:
    """Pump frames from *sender* to *receiver* until *sender* closes.

    AUDIO frames are gated on the floor — when sender_role doesn't
    hold the floor, AUDIO is silently dropped (we don't want a
    suspended push-to-talk mid-air to leak audio when the operator
    looks away).  STATE frames go through the arbiter.

    The other direction runs in a sibling task — see ``_handle_pair``.
    """
    other_role = Holder.BROWSER if sender_role is Holder.PI else Holder.PI
    async for raw in sender:
        if isinstance(raw, str):
            # We expect binary only.  Ignore but log.
            logger.debug("dropping text frame from %s", sender_role)
            continue
        try:
            frame = frames.decode(raw)
        except frames.FrameError as exc:
            logger.warning("Bad frame from %s: %s", sender_role, exc)
            try:
                await sender.send(frames.encode_error(
                    frames.ErrorCode.PROTOCOL, str(exc),
                ))
            except websockets.ConnectionClosed:
                pass
            continue

        if isinstance(frame, frames.Audio):
            if floor.holder is not sender_role:
                continue  # Drop — sender doesn't hold the floor
            try:
                await receiver.send(raw)
            except websockets.ConnectionClosed:
                return
        elif isinstance(frame, frames.State):
            await _handle_state(frame, sender, receiver, floor, sender_role, other_role)
        elif isinstance(frame, frames.Ping):
            try:
                await sender.send(frames.encode_ping(frame.token))
            except websockets.ConnectionClosed:
                return
        elif isinstance(frame, frames.Hello):
            # Hello after the initial handshake is a protocol violation
            await sender.send(frames.encode_error(
                frames.ErrorCode.PROTOCOL, "duplicate hello",
            ))
        # Error frames from the client are diagnostic — log + continue.
        elif isinstance(frame, frames.Error):
            logger.info("Error from %s: 0x%02x %s",
                        sender_role, frame.code, frame.reason)


async def _handle_state(
    frame: frames.State,
    sender: WebSocketServerProtocol,
    receiver: WebSocketServerProtocol,
    floor: Floor,
    sender_role: Holder,
    other_role: Holder,
) -> None:
    if frame.op is frames.StateOp.TAKE_FLOOR:
        result = floor.take(sender_role)
        if not result.accepted:
            await sender.send(frames.encode_error(
                frames.ErrorCode.FLOOR_BUSY,
                f"floor held by {result.new_holder.value}",
            ))
            return
        if result.notify_other:
            try:
                await receiver.send(frames.encode_state(frames.StateOp.TAKE_FLOOR))
            except websockets.ConnectionClosed:
                pass
    else:  # RELEASE_FLOOR
        result = floor.release(sender_role)
        if result.notify_other:
            try:
                await receiver.send(frames.encode_state(frames.StateOp.RELEASE_FLOOR))
            except websockets.ConnectionClosed:
                pass


async def _handle_pair(
    pi_ws: WebSocketServerProtocol,
    browser_ws: WebSocketServerProtocol,
    floor: Floor,
) -> None:
    """Run the two forwarding pumps until either side disconnects."""
    pi_to_browser = asyncio.create_task(
        _forward(pi_ws, browser_ws, floor, Holder.PI),
        name="audio-pi-to-browser",
    )
    browser_to_pi = asyncio.create_task(
        _forward(browser_ws, pi_ws, floor, Holder.BROWSER),
        name="audio-browser-to-pi",
    )
    try:
        done, pending = await asyncio.wait(
            {pi_to_browser, browser_to_pi},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        # Whichever side closed, cancel the other pump and force-release the floor.
        for t in {pi_to_browser, browser_to_pi}:
            if not t.done():
                t.cancel()
        floor.force_release()
        # Tell whoever's still up that the other side left
        for ws in (pi_ws, browser_ws):
            try:
                await ws.send(frames.encode_state(frames.StateOp.RELEASE_FLOOR))
            except (websockets.ConnectionClosed, RuntimeError):
                pass


# ── Connection handlers ──────────────────────────────────────────────


async def _handle_pi(
    state: RelayState,
    ws: WebSocketServerProtocol,
    auth_db_path: str,
) -> None:
    """One Pi-side connection.  Validates Bearer, holds the slot in
    state.pis, blocks waiting for a browser to pair.  When a browser
    joins, both run through ``_handle_pair``."""
    token = _bearer_from_request(ws.request_headers)
    if token is None:
        await ws.send(frames.encode_error(frames.ErrorCode.AUTH, "missing Bearer"))
        await ws.close(code=4401, reason="auth")
        return
    device_id = get_device_id_for_token(auth_db_path, token)
    if device_id is None:
        await ws.send(frames.encode_error(frames.ErrorCode.AUTH, "invalid Bearer"))
        await ws.close(code=4401, reason="auth")
        return

    if device_id in state.pis:
        # Replace the previous connection — newer one wins.  Old one
        # gets a clean "you've been superseded" close.
        old = state.pis[device_id]
        try:
            await old.send(frames.encode_error(
                frames.ErrorCode.ALREADY_PAIRED, "superseded by new connection",
            ))
            await old.close(code=4002, reason="superseded")
        except (websockets.ConnectionClosed, RuntimeError):
            pass
    state.pis[device_id] = ws

    await ws.send(frames.encode_hello(frames.Role.PI, {
        "version": "0.3", "device_id": device_id,
    }))
    logger.info("Pi connected: device=%s", device_id)

    try:
        # If a browser is already waiting for this device, pair now.
        for sid, (br_dev, br_ws) in list(state.browsers.items()):
            if br_dev == device_id:
                await _handle_pair(ws, br_ws, state.floor_for(device_id))
                state.browsers.pop(sid, None)
                return
        # Otherwise wait for one.  We exit when the Pi disconnects or when
        # a sibling task triggered by a /audio/browser handler picks up
        # this Pi via `_handle_pair`.
        async for raw in ws:
            # Pre-pair we accept STATE/PING/HELLO but drop AUDIO (no peer).
            if isinstance(raw, str):
                continue
            try:
                frame = frames.decode(raw)
            except frames.FrameError:
                continue
            if isinstance(frame, frames.Ping):
                await ws.send(frames.encode_ping(frame.token))
    finally:
        if state.pis.get(device_id) is ws:
            state.pis.pop(device_id, None)
        logger.info("Pi disconnected: device=%s", device_id)


async def _handle_browser(
    state: RelayState,
    ws: WebSocketServerProtocol,
    audio_key: bytes | None,
) -> None:
    """One browser-side connection.  Verifies the JWT, picks a unique
    session_id, blocks until the targeted Pi shows up (or returns
    DEVICE_NOT_CONNECTED if there's no Pi already)."""
    if audio_key is None:
        await ws.send(frames.encode_error(
            frames.ErrorCode.AUTH, "OPENRING_AUDIO_KEY not set on host",
        ))
        await ws.close(code=4401, reason="auth")
        return

    raw_jwt = _jwt_from_query(ws.path)
    if not raw_jwt:
        await ws.send(frames.encode_error(frames.ErrorCode.AUTH, "missing token"))
        await ws.close(code=4401, reason="auth")
        return

    try:
        claims = audio_auth.verify(raw_jwt, audio_key)
    except audio_auth.JwtError as exc:
        await ws.send(frames.encode_error(frames.ErrorCode.AUTH, str(exc)))
        await ws.close(code=4401, reason="auth")
        return

    if not state.jtis.claim(claims.jti, claims.exp):
        await ws.send(frames.encode_error(
            frames.ErrorCode.AUTH, "token already used (replay)",
        ))
        await ws.close(code=4401, reason="auth")
        return

    pi_ws = state.pis.get(claims.device_id)
    if pi_ws is None:
        await ws.send(frames.encode_error(
            frames.ErrorCode.DEVICE_NOT_CONNECTED,
            f"no Pi connected for device_id={claims.device_id}",
        ))
        await ws.close(code=4404, reason="device-not-connected")
        return

    if any(dev == claims.device_id for dev, _ in state.browsers.values()):
        await ws.send(frames.encode_error(
            frames.ErrorCode.ALREADY_PAIRED,
            "another browser already paired with this device",
        ))
        await ws.close(code=4409, reason="already-paired")
        return

    state.browsers[claims.jti] = (claims.device_id, ws)
    await ws.send(frames.encode_hello(frames.Role.BROWSER, {
        "version": "0.3", "device_id": claims.device_id,
    }))
    logger.info("Browser paired: device=%s sub=%s", claims.device_id, claims.sub)

    try:
        await _handle_pair(pi_ws, ws, state.floor_for(claims.device_id))
    finally:
        state.browsers.pop(claims.jti, None)
        logger.info("Browser disconnected: device=%s", claims.device_id)


# ── Entrypoint ───────────────────────────────────────────────────────


def setup_logging(level: str = "info") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stdout,
    )


async def serve(state: RelayState, audio_key: bytes | None, auth_db_path: str) -> None:
    async def router(ws: WebSocketServerProtocol, path: str) -> None:
        clean_path = urlparse(path).path
        if clean_path == "/audio/device":
            await _handle_pi(state, ws, auth_db_path)
        elif clean_path == "/audio/browser":
            await _handle_browser(state, ws, audio_key)
        else:
            await ws.close(code=4404, reason="not found")

    async with websockets.serve(
        router, host=LISTEN_HOST, port=LISTEN_PORT,
        ping_interval=20, ping_timeout=20, max_size=2**20,
    ):
        logger.info(
            "audio-relay listening on ws://%s:%d/audio/{device,browser}",
            LISTEN_HOST, LISTEN_PORT,
        )
        await asyncio.Future()


def main() -> None:
    setup_logging(os.environ.get("LOG_LEVEL", "info"))
    audio_key = audio_auth.load_key_from_env()
    if audio_key is None:
        logger.warning(
            "OPENRING_AUDIO_KEY not set — browser sessions will be rejected",
        )
    auth_db_path = os.environ.get("AUTH_DB_PATH", "/data/auth.db")
    state = RelayState()
    asyncio.run(serve(state, audio_key, auth_db_path))


if __name__ == "__main__":
    main()
