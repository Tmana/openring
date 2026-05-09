"""Read-only access to the ``clipper:`` config section.

Defaults are conservative — service is opt-in via ``clipper.enabled``.
The pre/post roll values match what's documented in ROADMAP.md #14
(5+5s); the segment-second granularity controls how tight the cut
edges land.  Smaller segments = less wasted footage per clip but
more ffmpeg overhead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ClipperSettings:
    """In-memory snapshot of ``clipper:`` config.

    Pre-roll is bounded by the size of the on-disk ring; if you raise
    PRE_ROLL above ``segment_seconds * (segments_per_camera - 2)`` you
    won't actually get that many seconds because the older ones get
    overwritten before the post-roll wait completes.  The defaults
    below give 30s of ring, plenty for 5+5.
    """

    enabled: bool = False
    clips_dir: str = "/data/clips"
    ring_dir: str = "/var/lib/openring/ring"
    pre_roll_seconds: float = 5.0
    post_roll_seconds: float = 5.0
    segment_seconds: int = 1
    segments_per_camera: int = 30
    trigger_classes: list[str] = field(default_factory=lambda: ["person", "doorbell_press"])
    min_confidence: float = 0.40
    max_concurrent_clips: int = 4

    def applies_to(self, class_name: str, confidence: float) -> bool:
        """True if a detection (or doorbell press) should produce a clip.

        Doorbell presses always carry confidence=1.0 so the threshold
        only really gates real detections.  Empty trigger_classes means
        "every class" — but the default is just person + presses.
        """
        if confidence < self.min_confidence:
            return False
        if not self.trigger_classes:
            return True
        target = class_name.lower()
        for entry in self.trigger_classes:
            ent = entry.lower()
            if ent == target:
                return True
        return False


def from_yaml(cfg: dict) -> ClipperSettings:
    """Build settings from a parsed scarguard.yml.  Missing section returns defaults."""
    sec = cfg.get("clipper") or {}
    if not isinstance(sec, dict):
        logger.warning("clipper config section is not a mapping — using defaults")
        return ClipperSettings()
    s = ClipperSettings()
    s.enabled = bool(sec.get("enabled", False))
    s.clips_dir = str(sec.get("clips_dir") or s.clips_dir)
    s.ring_dir = str(sec.get("ring_dir") or s.ring_dir)
    s.pre_roll_seconds = float(sec.get("pre_roll_seconds", s.pre_roll_seconds))
    s.post_roll_seconds = float(sec.get("post_roll_seconds", s.post_roll_seconds))
    s.segment_seconds = max(1, int(sec.get("segment_seconds", s.segment_seconds)))
    s.segments_per_camera = max(8, int(sec.get("segments_per_camera", s.segments_per_camera)))
    s.min_confidence = float(sec.get("min_confidence", s.min_confidence))
    classes = sec.get("trigger_classes")
    if isinstance(classes, list) and classes:
        s.trigger_classes = [str(c) for c in classes]
    s.max_concurrent_clips = max(1, int(sec.get("max_concurrent_clips", s.max_concurrent_clips)))
    return s


def load_cameras(cfg: dict) -> list[dict]:
    """Return the list of enabled cameras with rtsp_url set."""
    out: list[dict] = []
    for c in cfg.get("cameras") or []:
        if not isinstance(c, dict):
            continue
        if not c.get("enabled", True):
            continue
        if not c.get("rtsp_url"):
            continue
        out.append(c)
    return out
