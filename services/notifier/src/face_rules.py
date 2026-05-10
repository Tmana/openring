"""Pure rule evaluation for v0.4 face_recognition.rules.

Given a recognition outcome and the user's rules block, decide what to
do with a notification:

  * **suppress** — log the event, fan out to zero channels.  Returned
    as ``actions_triggered = None`` to match the notifier's existing
    suppression sentinel (services/notifier/src/main.py:152).
  * **fan-out** — return the explicit channel list from the matched
    rule.  This *replaces* the per-camera ``notification_rules``
    decision because face identity is the more specific signal —
    "Sarah at the front door" outranks "person at the front door".

Rule precedence is first-match-wins, with two special pseudo-labels:

  * ``"unknown"`` — a face was detected but did not match any
    enrolled face's embedding cluster.  ``recognition.status ==
    "unknown"``.
  * ``"*"`` — any matched-known face.  Catch-all for "I have specific
    rules for Sarah but a generic ping for everyone else I've enrolled".

When the recognition status is ``no_face`` or ``error`` (the
recognizer looked but couldn't classify), we explicitly do NOT apply
face rules — the caller must fall through to the existing
``notification_rules`` path.  ``evaluate`` signals this by returning
``None`` for the *outcome* (distinct from the suppress outcome
``("suppress", ...)``).
"""

from __future__ import annotations

import logging
from typing import Literal, NamedTuple

logger = logging.getLogger(__name__)

# Outcome shape: ("suppress", priority) or ("dispatch", channels, priority).
# Priority is "normal" or "high" — passed through to notifiers as a hint;
# implementations may map it to ntfy priority or email subject prefixing.
Priority = Literal["normal", "high"]


class RuleOutcome(NamedTuple):
    """Result of evaluating face_recognition.rules against a recognition.

    * ``action == "suppress"`` — fan out to zero channels.
    * ``action == "dispatch"`` — fan out to ``channels``.
    """

    action: Literal["suppress", "dispatch"]
    channels: list[str]
    priority: Priority
    matched_label: str  # the rule label that matched, for logging


def _normalise_rules(rules: object) -> list[dict]:
    """Coerce a YAML-loaded rules value into a list of dicts.

    Tolerates the user writing scalars in unexpected places — bad
    config shouldn't crash the notifier."""
    if not isinstance(rules, list):
        return []
    out: list[dict] = []
    for entry in rules:
        if not isinstance(entry, dict):
            continue
        if "label" not in entry:
            continue
        out.append(entry)
    return out


def _rule_outcome(rule: dict) -> RuleOutcome:
    """Convert a single matched rule dict into a RuleOutcome."""
    raw_channels = rule.get("channels")
    if isinstance(raw_channels, list):
        channels = [str(c) for c in raw_channels]
    else:
        channels = []
    priority_raw = str(rule.get("priority", "normal")).strip().lower()
    priority: Priority = "high" if priority_raw == "high" else "normal"
    label = str(rule.get("label", ""))
    if not channels:
        return RuleOutcome(action="suppress", channels=[],
                           priority=priority, matched_label=label)
    return RuleOutcome(action="dispatch", channels=channels,
                       priority=priority, matched_label=label)


def evaluate(
    recognition: dict | None,
    rules: object,
) -> RuleOutcome | None:
    """Evaluate the rules block against a recognition outcome.

    Args:
        recognition: a recognition row dict (status, label, face_id,
            score, …) or ``None`` if the recognizer didn't produce one.
        rules: the parsed ``face_recognition.rules`` config value
            (list of dicts ideally; tolerated as anything else).

    Returns:
        ``RuleOutcome`` when a rule matched, ``None`` when the caller
        should fall through to the existing per-camera
        ``notification_rules`` path.

        Returning ``None`` rather than a default rule is deliberate:
        we want operators to roll the recognizer out and observe a
        week of recognitions before changing notification behaviour.
    """
    normalised = _normalise_rules(rules)
    if not normalised:
        return None
    if recognition is None:
        return None

    status = str(recognition.get("status", "")).lower()
    if status not in ("matched", "unknown"):
        # no_face / error / unrecognised — fall through to legacy path.
        return None

    label = str(recognition.get("label") or "").strip()
    label_lower = label.lower()

    # First-match-wins by user-stated rule order.  We honour the rule
    # *as written* so a user with rules ordered:
    #   - label: Sarah   channels: []
    #   - label: '*'     channels: [phone-ntfy]
    # gets Sarah suppression even though "*" comes after.
    for rule in normalised:
        rule_label = str(rule.get("label", "")).strip()
        if not rule_label:
            continue
        if rule_label.lower() == "unknown":
            if status == "unknown":
                return _rule_outcome(rule)
            continue
        if rule_label == "*":
            if status == "matched":
                return _rule_outcome(rule)
            continue
        # Specific label rule — matches only when status is matched
        # AND the label string equals (case-insensitive).
        if status == "matched" and rule_label.lower() == label_lower:
            return _rule_outcome(rule)

    # No rule matched — fall through to legacy path.
    return None


def is_face_class(class_name: str | None, trigger_classes: object) -> bool:
    """True when this detection class should consult face rules.

    Mirrors ``RecognizerSettings.applies_to`` semantics so the notifier
    only buffers events the recognizer would actually look at.
    Empty/missing trigger_classes intentionally means *any* class —
    matches the recognizer's "default to person" YAML guard."""
    if not isinstance(trigger_classes, list) or not trigger_classes:
        return False
    if not class_name:
        return False
    target = class_name.lower()
    for entry in trigger_classes:
        if str(entry).lower() == target:
            return True
    return False
