"""Concat ring segments into a final MP4 clip.

Uses ffmpeg's concat demuxer over a temp listing file.  We don't
re-encode — the input segments are MPEG-TS H.264 and the output is
MP4 with the same H.264 stream copied as-is, so writing a 10s clip
is bound by disk speed (sub-second on a modest x86 host).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def write_clip(
    segments: list[Path],
    output_path: Path,
) -> int | None:
    """Concat *segments* into ``output_path`` (MP4).  Returns size in bytes,
    or None on error.  Empty segment list returns None without invoking ffmpeg.

    Caller is responsible for picking which segments belong to which event.
    This function just does the mechanical concat.
    """
    if not segments:
        return None
    if shutil.which("ffmpeg") is None:
        logger.error("ffmpeg missing — cannot write clip")
        return None

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Build a temporary concat list file.  Each line:
    #   file '/abs/path/to/segment.ts'
    # Single quotes in the path are escaped as '\''.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, dir=str(output_path.parent),
    ) as listing:
        listing_path = Path(listing.name)
        for seg in segments:
            escaped = str(seg.resolve()).replace("'", r"'\''")
            listing.write(f"file '{escaped}'\n")

    try:
        cmd = [
            "ffmpeg",
            "-loglevel", "error",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(listing_path),
            "-c", "copy",
            "-bsf:v", "h264_mp4toannexb",
            "-movflags", "+faststart",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            logger.error("ffmpeg concat failed for %s: %s", output_path, stderr)
            return None
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg concat timed out for %s", output_path)
        return None
    except OSError:
        logger.exception("Failed to spawn ffmpeg for clip write")
        return None
    finally:
        try:
            os.unlink(listing_path)
        except OSError:
            pass

    try:
        return output_path.stat().st_size
    except OSError:
        return None
