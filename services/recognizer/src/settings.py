"""Read-only access to the ``face_recognition:`` config section.

The recognizer service is opt-in via ``face_recognition.enabled``; with the
default config the container starts and idles, doing no work.

Trade-off: ``tolerance`` is given in ``face_recognition`` lib distance
(lower = closer match).  0.6 is the library's recommended default; below
0.5 you'll miss matches across glasses-on/off, above 0.7 you'll start
getting false positives between similar-looking people.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class RecognizerSettings:
    """In-memory snapshot of ``face_recognition:`` config.

    Mirrors ClipperSettings in shape so that the same hot-reload pattern
    via ConfigWatcher + AtomicRef can be reused without surprise.
    """

    enabled: bool = False
    trigger_classes: list[str] = field(default_factory=lambda: ["person"])
    min_confidence: float = 0.40
    bbox_padding_pct: float = 0.20
    tolerance: float = 0.6
    max_concurrent_workers: int = 2
    references_dir: str = "/data/face-references"
    snapshots_dir: str = "/data/snapshots"
    db_path: str = "/data/recognizer.db"

    def applies_to(self, class_name: str, confidence: float) -> bool:
        """True if a detection event should be passed through the recognizer.

        Empty trigger_classes intentionally means "everything"; the
        default config is ``["person"]`` so YOLO bird/cat/raccoon noise
        doesn't trigger face inference.
        """
        if confidence < self.min_confidence:
            return False
        if not self.trigger_classes:
            return True
        target = class_name.lower()
        for entry in self.trigger_classes:
            if str(entry).lower() == target:
                return True
        return False


def from_yaml(cfg: dict) -> RecognizerSettings:
    """Build settings from a parsed openring.yml.  Missing section returns defaults."""
    sec = cfg.get("face_recognition") or {}
    if not isinstance(sec, dict):
        logger.warning("face_recognition config section is not a mapping — using defaults")
        return RecognizerSettings()
    s = RecognizerSettings()
    s.enabled = bool(sec.get("enabled", False))
    s.min_confidence = float(sec.get("min_confidence", s.min_confidence))
    s.bbox_padding_pct = max(0.0, min(1.0, float(sec.get("bbox_padding_pct", s.bbox_padding_pct))))
    s.tolerance = max(0.1, min(1.0, float(sec.get("tolerance", s.tolerance))))
    s.max_concurrent_workers = max(1, int(sec.get("max_concurrent_workers", s.max_concurrent_workers)))
    s.references_dir = str(sec.get("references_dir") or s.references_dir)
    s.snapshots_dir = str(sec.get("snapshots_dir") or s.snapshots_dir)
    s.db_path = str(sec.get("db_path") or s.db_path)
    classes = sec.get("trigger_classes")
    if isinstance(classes, list) and classes:
        s.trigger_classes = [str(c) for c in classes]
    return s
