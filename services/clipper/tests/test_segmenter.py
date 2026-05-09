"""CameraSegmenter tests — mtime-window logic exercised against fake files."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from segmenter import CameraSegmenter, restrict_ring_size


@pytest.fixture
def seg(tmp_path):
    """A segmenter pointed at a temp ring directory.  Doesn't start ffmpeg."""
    return CameraSegmenter(
        camera_name="front-door",
        rtsp_url="rtsp://localhost/test",  # never actually used
        ring_dir=str(tmp_path),
        segment_seconds=1,
        segments_per_camera=10,
    )


def _touch(p: Path, mtime: float) -> None:
    """Create a fake .ts segment with the given mtime."""
    p.write_bytes(b"\x47" + b"\x00" * 187)  # one MPEG-TS sync byte + filler
    os.utime(p, (mtime, mtime))


class TestSegmentsInWindow:
    def test_empty_dir_returns_empty(self, seg) -> None:
        assert seg.segments_in_window(0.0, 1e10) == []

    def test_includes_segments_in_range(self, seg, tmp_path) -> None:
        ring = tmp_path / "front-door"
        ring.mkdir()
        for i, ts in enumerate([100.0, 105.0, 110.0]):
            _touch(ring / f"seg_{i:03d}.ts", ts)
        result = seg.segments_in_window(102.0, 108.0)
        assert [p.name for p in result] == ["seg_001.ts"]

    def test_excludes_outside_range(self, seg, tmp_path) -> None:
        ring = tmp_path / "front-door"
        ring.mkdir()
        _touch(ring / "before.ts", 50.0)
        _touch(ring / "after.ts", 200.0)
        _touch(ring / "in.ts", 100.0)
        result = seg.segments_in_window(90.0, 110.0)
        assert [p.name for p in result] == ["in.ts"]

    def test_sorted_by_mtime(self, seg, tmp_path) -> None:
        ring = tmp_path / "front-door"
        ring.mkdir()
        # Write in reverse mtime order to confirm sorting works
        _touch(ring / "c.ts", 110.0)
        _touch(ring / "a.ts", 100.0)
        _touch(ring / "b.ts", 105.0)
        result = seg.segments_in_window(0.0, 200.0)
        assert [p.name for p in result] == ["a.ts", "b.ts", "c.ts"]

    def test_ignores_non_ts(self, seg, tmp_path) -> None:
        ring = tmp_path / "front-door"
        ring.mkdir()
        _touch(ring / "real.ts", 100.0)
        _touch(ring / "junk.txt", 100.0)
        _touch(ring / "list.m3u8", 100.0)
        result = seg.segments_in_window(0.0, 200.0)
        assert [p.name for p in result] == ["real.ts"]


class TestFFmpegCommand:
    def test_command_uses_copy(self, seg) -> None:
        cmd = seg._build_ffmpeg_cmd()
        # No re-encode — ``-c:v copy`` keeps the H.264 NALUs as-is
        assert "copy" in cmd
        # Audio dropped for v0.2
        assert "-an" in cmd
        # Wrap is set so the ring stays bounded
        assert "-segment_wrap" in cmd
        wrap_idx = cmd.index("-segment_wrap") + 1
        assert int(cmd[wrap_idx]) == seg._segments_per_camera


class TestRestrictRingSize:
    def test_no_op_when_under_limit(self, tmp_path) -> None:
        cam = tmp_path / "cam"
        cam.mkdir()
        for i in range(5):
            _touch(cam / f"seg_{i}.ts", float(i))
        restrict_ring_size(str(tmp_path), segments_per_camera_limit=10)
        assert sum(1 for _ in cam.iterdir()) == 5

    def test_trims_oldest(self, tmp_path) -> None:
        cam = tmp_path / "cam"
        cam.mkdir()
        for i in range(8):
            _touch(cam / f"seg_{i}.ts", float(i))
        restrict_ring_size(str(tmp_path), segments_per_camera_limit=3)
        remaining = sorted(p.name for p in cam.iterdir())
        # The three with the largest mtimes survive
        assert remaining == ["seg_5.ts", "seg_6.ts", "seg_7.ts"]

    def test_handles_missing_dir(self, tmp_path) -> None:
        # Non-existent base dir should be a quiet no-op
        restrict_ring_size(str(tmp_path / "nope"))
