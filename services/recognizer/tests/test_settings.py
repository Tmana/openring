"""Settings + trigger filter tests."""

from __future__ import annotations

from settings import RecognizerSettings, from_yaml


class TestAppliesTo:
    def test_low_confidence_skipped(self) -> None:
        s = RecognizerSettings(min_confidence=0.5, trigger_classes=["person"])
        assert s.applies_to("person", 0.4) is False

    def test_above_min_passes(self) -> None:
        s = RecognizerSettings(min_confidence=0.4, trigger_classes=["person"])
        assert s.applies_to("person", 0.85) is True

    def test_unrelated_class_skipped(self) -> None:
        s = RecognizerSettings(trigger_classes=["person"])
        assert s.applies_to("raccoon", 0.9) is False

    def test_empty_triggers_match_anything(self) -> None:
        s = RecognizerSettings(trigger_classes=[], min_confidence=0.0)
        assert s.applies_to("anything", 0.5) is True

    def test_case_insensitive(self) -> None:
        s = RecognizerSettings(trigger_classes=["Person"])
        assert s.applies_to("PERSON", 0.9) is True


class TestFromYaml:
    def test_missing_section_defaults(self) -> None:
        s = from_yaml({})
        assert s.enabled is False
        assert s.tolerance == 0.6
        assert s.trigger_classes == ["person"]

    def test_full_section(self) -> None:
        s = from_yaml({"face_recognition": {
            "enabled": True,
            "trigger_classes": ["person", "vehicle"],
            "tolerance": 0.5,
            "bbox_padding_pct": 0.3,
            "max_concurrent_workers": 4,
        }})
        assert s.enabled is True
        assert s.trigger_classes == ["person", "vehicle"]
        assert s.tolerance == 0.5
        assert s.bbox_padding_pct == 0.3
        assert s.max_concurrent_workers == 4

    def test_garbage_section_defaults(self) -> None:
        s = from_yaml({"face_recognition": "not a dict"})
        assert s.enabled is False

    def test_tolerance_clamped_low(self) -> None:
        # Below 0.1 makes no practical sense — clamp.
        s = from_yaml({"face_recognition": {"enabled": True, "tolerance": 0.01}})
        assert s.tolerance == 0.1

    def test_tolerance_clamped_high(self) -> None:
        # Above 1.0 the lib treats every face as a match.
        s = from_yaml({"face_recognition": {"enabled": True, "tolerance": 5.0}})
        assert s.tolerance == 1.0

    def test_padding_clamped(self) -> None:
        s = from_yaml({"face_recognition": {"enabled": True, "bbox_padding_pct": 2.0}})
        assert s.bbox_padding_pct == 1.0

    def test_workers_floor(self) -> None:
        s = from_yaml({"face_recognition": {"enabled": True, "max_concurrent_workers": 0}})
        assert s.max_concurrent_workers == 1

    def test_empty_trigger_list_falls_back_to_default(self) -> None:
        # Empty list isn't applied — it would defeat the "default to person"
        # safety.  Use an explicit trigger_classes if you mean it.
        s = from_yaml({"face_recognition": {"enabled": True, "trigger_classes": []}})
        assert s.trigger_classes == ["person"]
