"""Wire-format codec tests.

Exhaustive coverage of every encode → decode → equality round-trip
path, plus malformed-input rejection.  This module is what the
``docs/AUDIO.md`` wire format compiles to; if a test changes here,
the doc almost certainly needs to change too.
"""

from __future__ import annotations

import pytest
from audio_frames import (
    Audio,
    Error,
    ErrorCode,
    FrameError,
    FrameType,
    Hello,
    Ping,
    Role,
    State,
    StateOp,
    decode,
    encode_audio,
    encode_error,
    encode_hello,
    encode_ping,
    encode_state,
)


class TestEncodeDecode:
    def test_hello_pi_round_trip(self):
        wire = encode_hello(Role.PI, {"version": "0.3", "device_id": "front-door"})
        frame = decode(wire)
        assert isinstance(frame, Hello)
        assert frame.role is Role.PI
        assert frame.metadata == {"version": "0.3", "device_id": "front-door"}

    def test_hello_browser_round_trip(self):
        wire = encode_hello(Role.BROWSER, {"jwt": "eyJ..."})
        frame = decode(wire)
        assert isinstance(frame, Hello)
        assert frame.role is Role.BROWSER

    def test_audio_round_trip(self):
        opus = b"\x78\x07\xff\xfe" + b"\x01" * 80
        frame = decode(encode_audio(opus))
        assert isinstance(frame, Audio)
        assert frame.opus_payload == opus

    def test_audio_empty_payload(self):
        # Edge case: empty audio frame is valid (caller can drop)
        frame = decode(encode_audio(b""))
        assert isinstance(frame, Audio)
        assert frame.opus_payload == b""

    def test_state_take(self):
        frame = decode(encode_state(StateOp.TAKE_FLOOR))
        assert isinstance(frame, State)
        assert frame.op is StateOp.TAKE_FLOOR

    def test_state_release(self):
        frame = decode(encode_state(StateOp.RELEASE_FLOOR))
        assert isinstance(frame, State)
        assert frame.op is StateOp.RELEASE_FLOOR

    def test_error_round_trip(self):
        frame = decode(encode_error(ErrorCode.AUTH, "bad token"))
        assert isinstance(frame, Error)
        assert frame.code is ErrorCode.AUTH
        assert frame.reason == "bad token"

    def test_error_no_reason(self):
        frame = decode(encode_error(ErrorCode.PROTOCOL))
        assert isinstance(frame, Error)
        assert frame.reason == ""

    def test_ping_round_trip(self):
        token = b"abcdefgh"
        frame = decode(encode_ping(token))
        assert isinstance(frame, Ping)
        assert frame.token == token

    def test_ping_wrong_token_size_raises(self):
        with pytest.raises(ValueError):
            encode_ping(b"too-short")
        with pytest.raises(ValueError):
            encode_ping(b"way-too-long-token")


class TestDecodeMalformed:
    """All FrameError paths.  Real WS messages can arrive corrupted; the
    relay must reject without taking down the handler."""

    def test_empty_message(self):
        with pytest.raises(FrameError):
            decode(b"")

    def test_unknown_type_byte(self):
        with pytest.raises(FrameError, match="unknown frame type"):
            decode(b"\xff")

    def test_hello_missing_role(self):
        with pytest.raises(FrameError, match="missing role"):
            decode(bytes([FrameType.HELLO]))

    def test_hello_unknown_role(self):
        with pytest.raises(FrameError, match="unknown role"):
            decode(bytes([FrameType.HELLO, 0xfe]) + b"{}")

    def test_hello_metadata_not_utf8(self):
        with pytest.raises(FrameError, match="not utf-8"):
            decode(bytes([FrameType.HELLO, Role.PI]) + b"\xff\xfe")

    def test_hello_metadata_not_json(self):
        with pytest.raises(FrameError, match="not JSON"):
            decode(bytes([FrameType.HELLO, Role.PI]) + b"not json")

    def test_hello_metadata_array_not_object(self):
        with pytest.raises(FrameError, match="not a JSON object"):
            decode(bytes([FrameType.HELLO, Role.PI]) + b"[1,2,3]")

    def test_state_wrong_body_length(self):
        with pytest.raises(FrameError, match="expected 1 byte body"):
            decode(bytes([FrameType.STATE]))
        with pytest.raises(FrameError, match="expected 1 byte body"):
            decode(bytes([FrameType.STATE, 0x01, 0x02]))

    def test_state_unknown_op(self):
        with pytest.raises(FrameError, match="unknown op"):
            decode(bytes([FrameType.STATE, 0x99]))

    def test_error_missing_code(self):
        with pytest.raises(FrameError, match="missing code"):
            decode(bytes([FrameType.ERROR]))

    def test_error_unknown_code(self):
        with pytest.raises(FrameError, match="unknown code"):
            decode(bytes([FrameType.ERROR, 0xee]))

    def test_ping_wrong_token_length(self):
        with pytest.raises(FrameError, match="expected 8 byte token"):
            decode(bytes([FrameType.PING]) + b"short")
        with pytest.raises(FrameError, match="expected 8 byte token"):
            decode(bytes([FrameType.PING]) + b"way-too-long-token")
