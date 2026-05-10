"""Pi-side audio relay client.

Connects to the host's audio-relay WebSocket as a Pi (Bearer-authed
with the device token from /etc/openring/secrets.env), and:

  * When the floor is held by the BROWSER, decodes incoming Opus
    frames and pipes the PCM to `aplay` for the speaker.
  * When the floor is held by the PI (operator pressed "Listen"),
    pipes `arecord` PCM through `opusenc` and sends each Opus frame
    as a 0x02 message.
  * When the floor is NONE, both pipelines drain to /dev/null so the
    arecord buffer doesn't fill while we wait.

See ``docs/AUDIO.md`` for the wire format + state machine.

Implementation note: arecord, opusenc, opusdec, aplay all live in
``opus-tools`` + ``alsa-utils`` packages.  pi-setup.sh adds them to
the apt install in this PR.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys

# These imports come from a vendored copy of frames.py installed
# alongside common.py during pi-setup.  See FILES note at bottom.
from common import load_settings  # type: ignore[import-not-found]
from audio_frames import (  # type: ignore[import-not-found]
    Audio,
    Error,
    FrameError,
    Hello,
    Ping,
    Role,
    State,
    StateOp,
    decode,
    encode_audio,
    encode_hello,
    encode_state,
)

import websockets

logger = logging.getLogger(__name__)

# Audio params — keep in lockstep with docs/AUDIO.md §"Audio frame".
SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_SAMPLES = 320  # 20ms at 16 kHz
PCM_BYTES_PER_FRAME = FRAME_SAMPLES * 2 * CHANNELS  # S16_LE


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stdout,
    )


def _build_ws_url(host_base_url: str) -> str:
    """https://h → wss://h/audio/device, http://h → ws://h/audio/device."""
    if host_base_url.startswith("https://"):
        return f"wss://{host_base_url[len('https://'):]}/audio/device"
    if host_base_url.startswith("http://"):
        return f"ws://{host_base_url[len('http://'):]}/audio/device"
    raise ValueError(f"HOST_BASE_URL must be http(s)://; got {host_base_url!r}")


def _check_tools() -> bool:
    """Confirm arecord/aplay/opusenc/opusdec are on PATH.  Returns True
    iff all four are present; otherwise logs which are missing and
    returns False so the caller can degrade gracefully (refuse to take
    the floor) instead of crashing per-frame."""
    missing = [
        t for t in ("arecord", "aplay", "opusenc", "opusdec")
        if shutil.which(t) is None
    ]
    if missing:
        logger.error(
            "Audio tools missing: %s — install opus-tools + alsa-utils",
            ", ".join(missing),
        )
        return False
    return True


# ── Outgoing pipeline (mic → opus → ws) ──────────────────────────────


async def _stream_mic(
    ws: websockets.WebSocketClientProtocol,
    stop: asyncio.Event,
) -> None:
    """Run ``arecord | opusenc`` and forward each Opus frame as 0x02.

    Spawns the chain on entry, terminates it on stop or WS close.
    Errors from the subprocesses are non-fatal — we log and let the
    floor stay where it is; the operator will hear silence rather
    than the relay crashing.
    """
    arecord_cmd = [
        "arecord",
        "-q",                           # quiet, no progress noise
        "-t", "raw",
        "-f", "S16_LE",
        "-r", str(SAMPLE_RATE),
        "-c", str(CHANNELS),
        "--buffer-size=1024",
    ]
    opusenc_cmd = [
        "opusenc",
        "--quiet",
        "--bitrate", "16",
        "--raw",
        "--raw-rate", str(SAMPLE_RATE),
        "--raw-chan", str(CHANNELS),
        "-",                            # stdin
        "-",                            # stdout
    ]
    logger.info("mic: %s | %s", shlex.join(arecord_cmd), shlex.join(opusenc_cmd))
    try:
        rec = await asyncio.create_subprocess_exec(
            *arecord_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        enc = await asyncio.create_subprocess_exec(
            *opusenc_cmd, stdin=rec.stdout, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        logger.error("mic spawn failed: %s", exc)
        return

    try:
        # opusenc raw output is one frame per stdout chunk because we
        # pinned a 20ms frame size at 16kHz, and stdin is 1024-byte
        # PCM = 320 samples = 20ms.  Reading "any available" gets one
        # frame at a time in practice.
        assert enc.stdout is not None
        while not stop.is_set():
            try:
                chunk = await asyncio.wait_for(enc.stdout.read(2048), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if not chunk:
                break
            try:
                await ws.send(encode_audio(chunk))
            except websockets.ConnectionClosed:
                break
    finally:
        for p in (enc, rec):
            try:
                p.terminate()
                await asyncio.wait_for(p.wait(), timeout=2.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    p.kill()
                except ProcessLookupError:
                    pass


# ── Incoming pipeline (ws → opus → speaker) ──────────────────────────


class SpeakerSink:
    """Lazy ``opusdec | aplay`` chain.  Frames are written to opusdec's
    stdin one at a time; the chain stays running between frames so
    the audio doesn't tear on every new arrival."""

    def __init__(self) -> None:
        self._dec: asyncio.subprocess.Process | None = None
        self._play: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        if self._dec is not None:
            return
        # opusdec reads raw Opus frames on stdin (one per write) and
        # emits S16_LE PCM on stdout.  aplay writes PCM to ALSA.
        opusdec_cmd = [
            "opusdec",
            "--quiet",
            "--rate", str(SAMPLE_RATE),
            "--force-stereo" if CHANNELS == 2 else "--no-dither",
            "-",
            "-",
        ]
        aplay_cmd = [
            "aplay",
            "-q",
            "-t", "raw",
            "-f", "S16_LE",
            "-r", str(SAMPLE_RATE),
            "-c", str(CHANNELS),
        ]
        try:
            self._dec = await asyncio.create_subprocess_exec(
                *opusdec_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            self._play = await asyncio.create_subprocess_exec(
                *aplay_cmd, stdin=self._dec.stdout,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            logger.error("speaker spawn failed: %s", exc)
            await self.stop()

    async def write(self, opus_frame: bytes) -> None:
        if self._dec is None or self._dec.stdin is None:
            return
        try:
            self._dec.stdin.write(opus_frame)
            await self._dec.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            await self.stop()

    async def stop(self) -> None:
        for p in (self._play, self._dec):
            if p is None:
                continue
            try:
                p.terminate()
                await asyncio.wait_for(p.wait(), timeout=2.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    p.kill()
                except ProcessLookupError:
                    pass
        self._dec = None
        self._play = None


# ── Connection loop ──────────────────────────────────────────────────


async def _run_session(ws_url: str, settings: dict[str, str]) -> None:
    """One connect → talk → disconnect cycle.  Returns on disconnect;
    caller's outer loop reconnects with backoff."""
    device_id = settings.get("DEVICE_ID", "")
    token = settings.get("DEVICE_TOKEN", "")
    if not token:
        logger.error("DEVICE_TOKEN missing — cannot connect")
        await asyncio.sleep(30)
        return

    headers = [("Authorization", f"Bearer {token}")]
    logger.info("Connecting to %s as device=%s", ws_url, device_id)

    async with websockets.connect(ws_url, additional_headers=headers) as ws:
        # 1. Send our hello
        await ws.send(encode_hello(Role.PI, {
            "version": "0.3", "device_id": device_id,
        }))

        # 2. Wait for host's hello back (or error)
        first = await ws.recv()
        if isinstance(first, str):
            logger.error("Unexpected text frame at handshake")
            return
        try:
            initial = decode(first)
        except FrameError as exc:
            logger.error("Bad handshake from host: %s", exc)
            return
        if isinstance(initial, Error):
            logger.error(
                "Audio relay rejected us: code=0x%02x reason=%s",
                initial.code, initial.reason,
            )
            return
        if not isinstance(initial, Hello):
            logger.error("Expected HELLO at handshake, got %r", initial)
            return
        logger.info("Audio relay paired (host metadata=%s)",
                    json.dumps(initial.metadata))

        # 3. Run the floor-state loop
        floor_holder: str = "none"   # none | pi | browser
        stop = asyncio.Event()
        speaker = SpeakerSink()
        mic_task: asyncio.Task | None = None

        async def cancel_mic() -> None:
            nonlocal mic_task
            if mic_task is not None and not mic_task.done():
                stop.set()
                try:
                    await mic_task
                except Exception:
                    pass
                stop.clear()
            mic_task = None

        try:
            async for raw in ws:
                if isinstance(raw, str):
                    continue
                try:
                    frame = decode(raw)
                except FrameError as exc:
                    logger.warning("Bad frame from host: %s", exc)
                    continue

                if isinstance(frame, Audio):
                    if floor_holder == "browser":
                        await speaker.start()
                        await speaker.write(frame.opus_payload)
                elif isinstance(frame, State):
                    if frame.op is StateOp.TAKE_FLOOR:
                        # The host tells us the OTHER side took the floor
                        floor_holder = "browser"
                        await speaker.start()
                        await cancel_mic()
                        logger.info("Floor → browser (listen)")
                    else:
                        floor_holder = "none"
                        await speaker.stop()
                        await cancel_mic()
                        logger.info("Floor → idle")
                elif isinstance(frame, Ping):
                    # Already handled at the WS layer; if the host echoes
                    # one back to us we ignore it.
                    pass
                elif isinstance(frame, Error):
                    logger.warning(
                        "Error from host: 0x%02x %s",
                        frame.code, frame.reason,
                    )
                # Hello after handshake is a protocol error from the host
                # — log and drop.
        finally:
            await cancel_mic()
            await speaker.stop()


async def _run() -> None:
    _setup_logging()
    settings = load_settings()
    if not _check_tools():
        # Don't busy-loop if the install is incomplete; just sit idle.
        # The systemd unit will keep us up; an admin can install
        # opus-tools and the next reconnect will pick them up.
        await asyncio.sleep(60)
        return
    ws_url = _build_ws_url(settings.get("HOST_BASE_URL", ""))

    delay = 2.0
    stop = asyncio.Event()

    def _shutdown(*_: object) -> None:
        stop.set()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_event_loop().add_signal_handler(sig, _shutdown)
        except (NotImplementedError, RuntimeError):
            # Windows / non-mainloop fallback
            signal.signal(sig, lambda *_: stop.set())

    while not stop.is_set():
        try:
            await _run_session(ws_url, settings)
            delay = 2.0  # successful connect; reset
        except websockets.InvalidStatus as exc:
            logger.error("Audio relay refused upgrade: %s", exc)
        except websockets.ConnectionClosed:
            logger.info("Audio relay disconnected")
        except OSError as exc:
            logger.warning("Audio relay connect failed: %s", exc)
        except Exception:
            logger.exception("Unexpected error in audio session")
        if stop.is_set():
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        delay = min(delay * 2, 30.0)

    # Defensive: drop any /tmp/healthy state on shutdown
    try:
        os.remove("/tmp/openring-audio-healthy")
    except OSError:
        pass


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()


# FILES note: pi-setup.sh installs services/doorbell-firmware/src/
# wholesale into /opt/openring/src.  frames.py is a clipper-side module,
# so we vendor it during install (cp into /opt/openring/src/) — the
# updated pi-setup.sh below handles this.
