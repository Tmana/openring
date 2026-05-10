"""face_rules.evaluate — rule-evaluation unit tests."""

from __future__ import annotations

import face_rules


def _matched(label: str = "Sarah", face_id: int = 1) -> dict:
    return {"status": "matched", "face_id": face_id, "label": label, "score": 0.32}


def _unknown() -> dict:
    return {"status": "unknown", "face_id": None, "label": None, "score": 0.71}


def _no_face() -> dict:
    return {"status": "no_face", "face_id": None, "label": None, "score": None}


class TestEmptyRulesFallThrough:
    def test_no_rules_returns_none(self) -> None:
        assert face_rules.evaluate(_matched(), []) is None

    def test_garbage_rules_returns_none(self) -> None:
        assert face_rules.evaluate(_matched(), "not a list") is None

    def test_no_recognition_returns_none(self) -> None:
        assert face_rules.evaluate(None, [{"label": "*", "channels": ["a"]}]) is None


class TestSuppressionRule:
    def test_suppress_specific_match(self) -> None:
        rules = [{"label": "Sarah", "channels": []}]
        outcome = face_rules.evaluate(_matched("Sarah"), rules)
        assert outcome is not None
        assert outcome.action == "suppress"
        assert outcome.channels == []
        assert outcome.matched_label == "Sarah"

    def test_case_insensitive_match(self) -> None:
        rules = [{"label": "sarah", "channels": []}]
        assert face_rules.evaluate(_matched("SARAH"), rules) is not None

    def test_suppress_via_unknown_rule(self) -> None:
        rules = [{"label": "unknown", "channels": []}]
        assert face_rules.evaluate(_unknown(), rules).action == "suppress"

    def test_suppress_via_star_rule_only_matches_known(self) -> None:
        # "*" should NOT match unknown — only known faces fall through.
        rules = [{"label": "*", "channels": []}]
        assert face_rules.evaluate(_unknown(), rules) is None
        assert face_rules.evaluate(_matched(), rules) is not None


class TestEscalationRule:
    def test_dispatch_to_listed_channels(self) -> None:
        rules = [{
            "label": "ex-roommate",
            "channels": ["phone-ntfy", "owner-email", "panic-webhook"],
            "priority": "high",
        }]
        outcome = face_rules.evaluate(_matched("ex-roommate"), rules)
        assert outcome is not None
        assert outcome.action == "dispatch"
        assert outcome.channels == ["phone-ntfy", "owner-email", "panic-webhook"]
        assert outcome.priority == "high"

    def test_priority_default_is_normal(self) -> None:
        rules = [{"label": "Sarah", "channels": ["phone-ntfy"]}]
        outcome = face_rules.evaluate(_matched("Sarah"), rules)
        assert outcome.priority == "normal"

    def test_priority_garbage_falls_back_to_normal(self) -> None:
        rules = [{"label": "Sarah", "channels": ["phone-ntfy"], "priority": "URGENT_AAA"}]
        outcome = face_rules.evaluate(_matched("Sarah"), rules)
        assert outcome.priority == "normal"


class TestFirstMatchWins:
    def test_specific_before_star(self) -> None:
        rules = [
            {"label": "Sarah", "channels": []},          # suppress
            {"label": "*", "channels": ["phone-ntfy"]},  # all-known fallback
        ]
        outcome = face_rules.evaluate(_matched("Sarah"), rules)
        assert outcome.action == "suppress"

    def test_star_after_specific(self) -> None:
        rules = [
            {"label": "Sarah", "channels": []},
            {"label": "*", "channels": ["phone-ntfy"]},
        ]
        outcome = face_rules.evaluate(_matched("Bob"), rules)
        assert outcome is not None
        assert outcome.channels == ["phone-ntfy"]

    def test_user_order_respected_even_when_unintuitive(self) -> None:
        # User wrote "*" first by mistake — gets quiet for everyone,
        # including Sarah.  We respect their order so the rule semantics
        # are stable and debuggable.
        rules = [
            {"label": "*", "channels": ["phone-ntfy"]},
            {"label": "Sarah", "channels": []},
        ]
        outcome = face_rules.evaluate(_matched("Sarah"), rules)
        assert outcome.channels == ["phone-ntfy"]


class TestNoFaceFallsThrough:
    def test_no_face_status(self) -> None:
        rules = [{"label": "*", "channels": ["phone-ntfy"]}]
        assert face_rules.evaluate(_no_face(), rules) is None

    def test_error_status(self) -> None:
        rules = [{"label": "unknown", "channels": ["phone-ntfy"]}]
        assert face_rules.evaluate({"status": "error"}, rules) is None


class TestRulesShapeTolerance:
    def test_rule_without_label_skipped(self) -> None:
        rules = [
            {"channels": ["phone-ntfy"]},                # no label → ignored
            {"label": "Sarah", "channels": []},
        ]
        outcome = face_rules.evaluate(_matched("Sarah"), rules)
        assert outcome.action == "suppress"

    def test_rule_with_garbage_channels_treated_as_suppress(self) -> None:
        rules = [{"label": "Sarah", "channels": "not a list"}]
        outcome = face_rules.evaluate(_matched("Sarah"), rules)
        assert outcome.action == "suppress"

    def test_non_dict_rule_skipped(self) -> None:
        rules = ["not a dict", {"label": "Sarah", "channels": []}]
        assert face_rules.evaluate(_matched("Sarah"), rules) is not None


class TestIsFaceClass:
    def test_match(self) -> None:
        assert face_rules.is_face_class("person", ["person"]) is True

    def test_case_insensitive(self) -> None:
        assert face_rules.is_face_class("PERSON", ["person"]) is True

    def test_unrelated(self) -> None:
        assert face_rules.is_face_class("dog", ["person"]) is False

    def test_empty_triggers(self) -> None:
        # Empty/missing triggers means the notifier should NOT buffer —
        # if the recognizer wouldn't act, neither should we.
        assert face_rules.is_face_class("person", []) is False
        assert face_rules.is_face_class("person", None) is False

    def test_missing_class(self) -> None:
        assert face_rules.is_face_class(None, ["person"]) is False
