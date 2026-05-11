"""camera-bridge config-generation tests.

Pure functions over dicts — no MediaMTX, no ffmpeg, no Docker.  The
``execvp`` to MediaMTX at the end of ``main()`` is the only piece we
can't unit-test here; it gets exercised by the smoke test on a real
``docker compose up``.
"""

from __future__ import annotations

import main as bridge


class TestBridgedCameras:
    def test_ipcam_skipped(self) -> None:
        cfg = {"cameras": [
            {"name": "porch", "source": "ipcam", "rtsp_url": "rtsp://x/y", "enabled": True},
        ]}
        assert bridge.bridged_cameras(cfg) == []

    def test_pi_skipped(self) -> None:
        cfg = {"cameras": [
            {"name": "front-door", "source": "pi", "rtsp_url": "rtsp://p/d", "enabled": True},
        ]}
        assert bridge.bridged_cameras(cfg) == []

    def test_no_source_field_defaults_to_ipcam(self) -> None:
        # v0.1 backwards-compat: pre-existing configs with just rtsp_url
        # and no source field are treated as ipcam.
        cfg = {"cameras": [
            {"name": "old-style", "rtsp_url": "rtsp://x/y", "enabled": True},
        ]}
        assert bridge.bridged_cameras(cfg) == []

    def test_disabled_skipped(self) -> None:
        cfg = {"cameras": [
            {"name": "porch", "source": "file", "file": "/data/x.mp4", "enabled": False},
        ]}
        assert bridge.bridged_cameras(cfg) == []

    def test_webcam_kept(self) -> None:
        cfg = {"cameras": [
            {"name": "porch", "source": "webcam", "device": "/dev/video0", "enabled": True},
        ]}
        result = bridge.bridged_cameras(cfg)
        assert len(result) == 1
        assert result[0]["name"] == "porch"

    def test_file_kept(self) -> None:
        cfg = {"cameras": [
            {"name": "demo", "source": "file", "file": "/data/sample.mp4", "enabled": True},
        ]}
        result = bridge.bridged_cameras(cfg)
        assert len(result) == 1

    def test_unsupported_source_dropped(self) -> None:
        cfg = {"cameras": [
            {"name": "bad", "source": "magic", "enabled": True},
        ]}
        assert bridge.bridged_cameras(cfg) == []

    def test_garbage_entries_skipped(self) -> None:
        cfg = {"cameras": [
            "not a dict",
            None,
            {"name": "ok", "source": "file", "file": "/data/x.mp4", "enabled": True},
        ]}
        assert [c["name"] for c in bridge.bridged_cameras(cfg)] == ["ok"]


class TestValidation:
    def test_webcam_rejects_non_video_device(self) -> None:
        cfg = {"cameras": [
            {"name": "x", "source": "webcam", "device": "/etc/passwd", "enabled": True},
        ]}
        assert bridge.bridged_cameras(cfg) == []

    def test_file_rejects_path_outside_data(self) -> None:
        cfg = {"cameras": [
            {"name": "x", "source": "file", "file": "/etc/passwd", "enabled": True},
        ]}
        assert bridge.bridged_cameras(cfg) == []

    def test_file_rejects_relative_path(self) -> None:
        cfg = {"cameras": [
            {"name": "x", "source": "file", "file": "../etc/passwd", "enabled": True},
        ]}
        assert bridge.bridged_cameras(cfg) == []

    def test_name_must_match_regex(self) -> None:
        # Spaces, slashes, dots — none allowed.  Names land in URL
        # segments and MediaMTX YAML keys.
        for bad in ("front door", "../escape", "front.door", "_starts_underscore", ""):
            cfg = {"cameras": [
                {"name": bad, "source": "file", "file": "/data/x.mp4", "enabled": True},
            ]}
            assert bridge.bridged_cameras(cfg) == [], f"should reject name={bad!r}"

    def test_name_accepts_normal_shapes(self) -> None:
        for good in ("front-door", "porch_cam", "Cam1", "a"):
            cfg = {"cameras": [
                {"name": good, "source": "file", "file": "/data/x.mp4", "enabled": True},
            ]}
            assert len(bridge.bridged_cameras(cfg)) == 1, f"should accept name={good!r}"


class TestPathBlock:
    def test_webcam_ffmpeg_command(self) -> None:
        block = bridge._path_block(
            {"name": "porch", "source": "webcam", "device": "/dev/video0"}
        )
        cmd = block["runOnInit"]
        assert "ffmpeg" in cmd
        assert "-f v4l2" in cmd
        assert "-i /dev/video0" in cmd
        assert "rtsp://localhost:8554/porch" in cmd
        assert block["runOnInitRestart"] is True

    def test_file_loop_default(self) -> None:
        block = bridge._path_block(
            {"name": "demo", "source": "file", "file": "/data/x.mp4"}
        )
        cmd = block["runOnInit"]
        assert "-stream_loop -1" in cmd
        assert "-i /data/x.mp4" in cmd
        assert "rtsp://localhost:8554/demo" in cmd

    def test_file_loop_false(self) -> None:
        block = bridge._path_block(
            {"name": "demo", "source": "file", "file": "/data/x.mp4", "loop": False}
        )
        cmd = block["runOnInit"]
        assert "-stream_loop" not in cmd
        assert "-i /data/x.mp4" in cmd


class TestBuildMediamtxConfig:
    def test_empty_cameras_empty_paths(self) -> None:
        cfg = bridge.build_mediamtx_config({})
        assert cfg["paths"] == {}
        assert cfg["rtspAddress"] == ":8554"
        # All non-RTSP protocols disabled — minimal attack surface
        # since the bridge sits on the internal Docker network.
        assert cfg["hlsDisable"] is True
        assert cfg["webrtcDisable"] is True
        assert cfg["rtmpDisable"] is True
        assert cfg["srtDisable"] is True

    def test_mixed_sources_only_bridged_pass(self) -> None:
        cfg = bridge.build_mediamtx_config({"cameras": [
            {"name": "front-door", "source": "pi", "rtsp_url": "rtsp://p/d", "enabled": True},
            {"name": "driveway", "source": "ipcam", "rtsp_url": "rtsp://i/d", "enabled": True},
            {"name": "demo", "source": "file", "file": "/data/x.mp4", "enabled": True},
        ]})
        assert list(cfg["paths"].keys()) == ["demo"]
