"""Settings + trigger filter tests."""

from __future__ import annotations

from settings import ClipperSettings, from_yaml, load_cameras


class TestAppliesTo:
    def test_low_confidence_skipped(self) -> None:
        s = ClipperSettings(min_confidence=0.5, trigger_classes=["person"])
        assert s.applies_to("person", 0.4) is False

    def test_above_min_passes(self) -> None:
        s = ClipperSettings(min_confidence=0.4, trigger_classes=["person"])
        assert s.applies_to("person", 0.85) is True

    def test_doorbell_press_passes(self) -> None:
        # Default trigger list includes both
        s = ClipperSettings()
        assert s.applies_to("doorbell_press", 1.0) is True

    def test_unrelated_class_skipped(self) -> None:
        s = ClipperSettings(trigger_classes=["person"])
        assert s.applies_to("raccoon", 0.9) is False

    def test_empty_triggers_match_anything(self) -> None:
        s = ClipperSettings(trigger_classes=[], min_confidence=0.0)
        assert s.applies_to("anything", 0.5) is True


class TestFromYaml:
    def test_missing_section_defaults(self) -> None:
        s = from_yaml({})
        assert s.enabled is False
        assert s.pre_roll_seconds == 5.0

    def test_full_section(self) -> None:
        s = from_yaml({"clipper": {
            "enabled": True,
            "pre_roll_seconds": 3,
            "post_roll_seconds": 7,
            "trigger_classes": ["person", "doorbell_press"],
            "max_concurrent_clips": 8,
        }})
        assert s.enabled is True
        assert s.pre_roll_seconds == 3.0
        assert s.post_roll_seconds == 7.0
        assert s.max_concurrent_clips == 8

    def test_garbage_section_defaults(self) -> None:
        s = from_yaml({"clipper": "not a dict"})
        assert s.enabled is False

    def test_segments_per_camera_floor(self) -> None:
        s = from_yaml({"clipper": {"enabled": True, "segments_per_camera": 1}})
        assert s.segments_per_camera == 8


class TestLoadCameras:
    def test_skips_disabled(self) -> None:
        cfg = {"cameras": [
            {"name": "a", "rtsp_url": "rtsp://h/a", "enabled": False},
            {"name": "b", "rtsp_url": "rtsp://h/b", "enabled": True},
        ]}
        assert [c["name"] for c in load_cameras(cfg)] == ["b"]

    def test_skips_no_rtsp_url(self) -> None:
        cfg = {"cameras": [
            {"name": "a"},
            {"name": "b", "rtsp_url": "rtsp://h/b"},
        ]}
        assert [c["name"] for c in load_cameras(cfg)] == ["b"]

    def test_skips_garbage_entries(self) -> None:
        cfg = {"cameras": ["not a dict", {"name": "ok", "rtsp_url": "rtsp://h/ok"}]}
        assert [c["name"] for c in load_cameras(cfg)] == ["ok"]
