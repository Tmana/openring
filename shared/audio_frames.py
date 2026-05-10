"""Wire format for the audio relay's WebSocket messages.

Every message is binary.  First byte = frame type, remainder = payload.
Documented authoritatively in ``docs/AUDIO.md`` — keep that file in
lockstep with this module.

Frame types::

    0x01  HELLO     1 byte role + JSON metadata
    0x02  AUDIO     opus payload, raw (no Ogg)
    0x03  STATE     1 byte sub-code (0x01 take floor, 0x02 release)
    0x04  ERROR     1 byte error code + utf-8 reason
    0x05  PING      8 bytes echoed back as PONG
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import IntEnum


class FrameType(IntEnum):
    HELLO = 0x01
    AUDIO = 0x02
    STATE = 0x03
    ERROR = 0x04
    PING = 0x05


class Role(IntEnum):
    PI = 0x01
    BROWSER = 0x02


class StateOp(IntEnum):
    TAKE_FLOOR = 0x01
    RELEASE_FLOOR = 0x02


class ErrorCode(IntEnum):
    AUTH = 0x01
    DEVICE_NOT_CONNECTED = 0x02
    ALREADY_PAIRED = 0x03
    PROTOCOL = 0x04
    INTERNAL = 0x05
    FLOOR_BUSY = 0x06


# ── Encoders ─────────────────────────────────────────────────────────


def encode_hello(role: Role, metadata: dict) -> bytes:
    body = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
    return bytes([FrameType.HELLO, role]) + body


def encode_audio(opus_payload: bytes) -> bytes:
    return bytes([FrameType.AUDIO]) + opus_payload


def encode_state(op: StateOp) -> bytes:
    return bytes([FrameType.STATE, op])


def encode_error(code: ErrorCode, reason: str = "") -> bytes:
    body = reason.encode("utf-8")
    return bytes([FrameType.ERROR, code]) + body


def encode_ping(token: bytes) -> bytes:
    if len(token) != 8:
        raise ValueError("ping token must be exactly 8 bytes")
    return bytes([FrameType.PING]) + token


# ── Decoder ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Hello:
    role: Role
    metadata: dict


@dataclass(frozen=True)
class Audio:
    opus_payload: bytes


@dataclass(frozen=True)
class State:
    op: StateOp


@dataclass(frozen=True)
class Error:
    code: ErrorCode
    reason: str


@dataclass(frozen=True)
class Ping:
    token: bytes


Frame = Hello | Audio | State | Error | Ping


class FrameError(ValueError):
    """Raised when a binary message can't be parsed.  Caller should
    typically respond with an ERROR frame and close."""


def decode(message: bytes) -> Frame:
    """Parse a single binary WebSocket message.  Raises FrameError
    on malformed input — never returns garbage."""
    if not message:
        raise FrameError("empty message")

    type_byte = message[0]
    body = message[1:]

    if type_byte == FrameType.HELLO:
        if not body:
            raise FrameError("hello: missing role byte")
        try:
            role = Role(body[0])
        except ValueError as exc:
            raise FrameError(f"hello: unknown role 0x{body[0]:02x}") from exc
        try:
            metadata_raw = body[1:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FrameError("hello: metadata is not utf-8") from exc
        if not metadata_raw:
            metadata: dict = {}
        else:
            try:
                metadata = json.loads(metadata_raw)
            except json.JSONDecodeError as exc:
                raise FrameError(f"hello: metadata is not JSON: {exc}") from exc
            if not isinstance(metadata, dict):
                raise FrameError("hello: metadata is not a JSON object")
        return Hello(role=role, metadata=metadata)

    if type_byte == FrameType.AUDIO:
        # Empty audio frame is technically valid (silence) but pointless
        # to forward.  Caller can drop, we just decode it.
        return Audio(opus_payload=bytes(body))

    if type_byte == FrameType.STATE:
        if len(body) != 1:
            raise FrameError(
                f"state: expected 1 byte body, got {len(body)}",
            )
        try:
            op = StateOp(body[0])
        except ValueError as exc:
            raise FrameError(f"state: unknown op 0x{body[0]:02x}") from exc
        return State(op=op)

    if type_byte == FrameType.ERROR:
        if not body:
            raise FrameError("error: missing code byte")
        try:
            code = ErrorCode(body[0])
        except ValueError as exc:
            raise FrameError(f"error: unknown code 0x{body[0]:02x}") from exc
        try:
            reason = body[1:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FrameError("error: reason is not utf-8") from exc
        return Error(code=code, reason=reason)

    if type_byte == FrameType.PING:
        if len(body) != 8:
            raise FrameError(
                f"ping: expected 8 byte token, got {len(body)}",
            )
        return Ping(token=bytes(body))

    raise FrameError(f"unknown frame type 0x{type_byte:02x}")
