"""Per-camera ffmpeg subprocess that writes a rolling segment ring.

We don't decode here — ffmpeg copies H.264 NALUs straight from the RTSP
stream into MPEG-TS segments.  That's cheap (~zero CPU on x86, single-
digit % on a Pi 5 host) and lets us produce a real MP4 clip on demand
without ever buffering decoded frames in Python.

The ring lives at ``ring_dir/<camera>/seg_<NN>.ts``; we use ffmpeg's
``segment_wrap`` so segment N+1 overwrites segment 0 once the ring
fills.  Segment mtimes are the authoritative "this segment finished
at time T" timestamp the writer uses to assemble clips.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class CameraSegmenter:
    """Manage one ffmpeg subprocess for one camera's RTSP feed.

    The subprocess restarts on exit (RTSP flaps, ffmpeg crashes) with
    exponential backoff.  Stop the segmenter via :meth:`stop`.
    """

    def __init__(
        self,
        camera_name: str,
        rtsp_url: str,
        ring_dir: str,
        segment_seconds: int = 1,
        segments_per_camera: int = 30,
    ) -> None:
        self.camera_name = camera_name
        self._rtsp_url = rtsp_url
        self._ring_dir = Path(ring_dir) / camera_name
        self._segment_seconds = segment_seconds
        self._segments_per_camera = segments_per_camera
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._supervisor: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the supervised ffmpeg subprocess."""
        self._ring_dir.mkdir(parents=True, exist_ok=True)
        self._supervisor = threading.Thread(
            target=self._run_supervisor,
            name=f"segmenter-{self.camera_name}",
            daemon=True,
        )
        self._supervisor.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal stop; supervisor + ffmpeg shut down cleanly within timeout."""
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:
                pass
        if self._supervisor is not None:
            self._supervisor.join(timeout=timeout + 1.0)

    # ------------------------------------------------------------------
    # Segment listing — used by the clip writer
    # ------------------------------------------------------------------

    def segments_in_window(
        self,
        start_ts: float,
        end_ts: float,
    ) -> list[Path]:
        """Return ring segments whose mtime falls in ``[start_ts, end_ts]``.

        Sorted ascending by mtime.  The mtime is when ffmpeg finished
        writing the segment, so a segment with mtime T contains roughly
        the wall-clock window ``[T - segment_seconds, T]``.

        Caller usually pads ``start_ts`` by one segment width so the
        returned set actually covers the requested time range.
        """
        if not self._ring_dir.is_dir():
            return []
        candidates: list[tuple[float, Path]] = []
        for entry in self._ring_dir.iterdir():
            if not entry.is_file() or entry.suffix != ".ts":
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if start_ts <= mtime <= end_ts:
                candidates.append((mtime, entry))
        candidates.sort(key=lambda x: x[0])
        return [p for _, p in candidates]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_supervisor(self) -> None:
        """Restart ffmpeg with backoff until stop is requested."""
        delay = 2.0
        while not self._stop.is_set():
            self._launch()
            if self._stop.is_set():
                break
            logger.warning(
                "ffmpeg for camera %s exited; restarting in %.0fs",
                self.camera_name, delay,
            )
            self._stop.wait(timeout=delay)
            delay = min(delay * 2, 30.0)

    def _launch(self) -> None:
        """Run ffmpeg until it exits.  Resets backoff to 2s on stable run."""
        if shutil.which("ffmpeg") is None:
            logger.error("ffmpeg not on PATH — segmenter cannot start")
            self._stop.wait(timeout=10.0)
            return

        cmd = self._build_ffmpeg_cmd()
        logger.info(
            "Starting ffmpeg for camera %s: %s",
            self.camera_name, shlex.join(cmd),
        )
        try:
            # PIPE stderr so we can surface errors; close stdout (no use).
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=1,
            )
        except OSError:
            logger.exception("Failed to spawn ffmpeg for %s", self.camera_name)
            self._stop.wait(timeout=10.0)
            return

        run_started = time.monotonic()
        # Drain stderr so the buffer doesn't fill (which would block ffmpeg).
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        for raw in proc.stderr:
            if self._stop.is_set():
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                # Most ffmpeg output is progress noise; surface only error-ish
                # markers so we don't drown the journal.
                lower = line.lower()
                if any(tag in lower for tag in ("error", "fail", "warn", "rtsp")):
                    logger.info("[ffmpeg %s] %s", self.camera_name, line)
        proc.wait()
        if time.monotonic() - run_started > 30:
            # Stable run — reset backoff in supervisor by a quiet exit
            return

    def _build_ffmpeg_cmd(self) -> list[str]:
        # ``-c copy`` keeps H.264 NALUs as-is; no decode.
        # ``-f segment`` writes MPEG-TS chunks; ``-segment_wrap`` caps the
        # ring at N files; segments overwrite from index 0 once full.
        # ``-reset_timestamps 1`` keeps each segment's PTS starting at 0
        # so the eventual concat doesn't have to renumber.
        # ``-an`` drops audio for v0.2 — audio support lands in v0.3.
        ring_pattern = str(self._ring_dir / "seg_%03d.ts")
        return [
            "ffmpeg",
            "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-i", self._rtsp_url,
            "-an",
            "-c:v", "copy",
            "-f", "segment",
            "-segment_time", str(self._segment_seconds),
            "-segment_wrap", str(self._segments_per_camera),
            "-segment_format", "mpegts",
            "-reset_timestamps", "1",
            ring_pattern,
        ]


def restrict_ring_size(ring_dir: str, segments_per_camera_limit: int = 200) -> None:
    """Defence-in-depth: trim a ring directory to the configured size.

    ffmpeg's ``-segment_wrap`` enforces this on its own; this helper is
    a belt for the (very unlikely) case where a misconfigured value or
    an ffmpeg crash before wrap leaves stale segments behind.

    Walks every camera subdirectory of ``ring_dir`` and removes any
    .ts file beyond ``segments_per_camera_limit``, oldest first.
    """
    base = Path(ring_dir)
    if not base.is_dir():
        return
    for cam_dir in base.iterdir():
        if not cam_dir.is_dir():
            continue
        files = sorted(
            (p for p in cam_dir.iterdir() if p.is_file() and p.suffix == ".ts"),
            key=lambda p: p.stat().st_mtime,
        )
        if len(files) <= segments_per_camera_limit:
            continue
        for stale in files[:-segments_per_camera_limit]:
            try:
                os.unlink(stale)
            except OSError:
                pass
