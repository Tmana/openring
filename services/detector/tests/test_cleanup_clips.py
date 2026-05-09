"""Retention pruning of v0.2 video clips.

Drives RetentionCleaner._run() against fixtures of clip files +
detection_events rows; asserts that:
  * old + unlabelled clips get deleted
  * old + labelled clips are retained
  * fresh clips are always retained
  * clips-table rows are deleted in lockstep with the files
  * a missing clips table doesn't blow up the run
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cleanup import RetentionCleaner


def _create_schema(db_path: str, with_clips_table: bool = True) -> None:
    """Mirror just enough of detection_events + clips to drive _prune_clips."""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE detection_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            class_name TEXT NOT NULL,
            confidence REAL NOT NULL,
            camera_name TEXT NOT NULL,
            snapshot_path TEXT,
            actions_triggered TEXT,
            bbox TEXT,
            frame_size TEXT,
            feedback TEXT,
            corrected_class TEXT,
            feedback_token TEXT
        );
    """)
    if with_clips_table:
        conn.executescript("""
            CREATE TABLE clips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feedback_token TEXT NOT NULL UNIQUE,
                camera_name TEXT NOT NULL,
                clip_path TEXT NOT NULL,
                pre_roll_seconds REAL,
                post_roll_seconds REAL,
                duration_seconds REAL,
                size_bytes INTEGER,
                segments_used INTEGER,
                created_at TEXT NOT NULL,
                error TEXT
            );
        """)
    conn.commit()
    conn.close()


def _insert_event(db_path: str, token: str, feedback: str | None) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO detection_events
            (timestamp, class_name, confidence, camera_name, feedback_token, feedback)
           VALUES ('2025-01-01T00:00:00Z', 'person', 0.9, 'front-door', ?, ?)""",
        (token, feedback),
    )
    conn.commit()
    conn.close()


def _insert_clip(db_path: str, token: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO clips (feedback_token, camera_name, clip_path, created_at)
           VALUES (?, 'front-door', ?, '2025-01-01T00:00:00Z')""",
        (token, f"/data/clips/{token}.mp4"),
    )
    conn.commit()
    conn.close()


def _make_clip_file(clips_dir: Path, token: str, age_days: float) -> Path:
    """Create an empty .mp4 with mtime *age_days* in the past."""
    f = clips_dir / f"{token}.mp4"
    f.write_bytes(b"")
    target = time.time() - age_days * 86400
    os.utime(f, (target, target))
    return f


@pytest.fixture
def setup(tmp_path: Path):
    """Build a RetentionCleaner pointed at temp dirs + a fresh DB."""
    snapshot_dir = tmp_path / "snapshots"
    clips_dir = tmp_path / "clips"
    snapshot_dir.mkdir()
    clips_dir.mkdir()
    db_path = str(tmp_path / "openring.db")
    _create_schema(db_path)

    cleaner = RetentionCleaner(
        snapshot_dir=str(snapshot_dir),
        db_path=db_path,
        retention_days=30,
        clips_dir=str(clips_dir),
    )
    return cleaner, db_path, clips_dir


def _prune_clips_only(cleaner: RetentionCleaner) -> None:
    """Drive just the clips path of _run() — skip the snapshot prune so
    we can isolate the clip behaviour without an empty snapshots dir
    triggering noise."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=cleaner.retention_days)
    cleaner._prune_clips(cutoff)


class TestPruneClips:
    def test_old_unlabelled_clip_deleted(self, setup):
        cleaner, db_path, clips_dir = setup
        _insert_event(db_path, "old-no-label", feedback=None)
        _insert_clip(db_path, "old-no-label")
        _make_clip_file(clips_dir, "old-no-label", age_days=60)

        _prune_clips_only(cleaner)

        # File gone
        assert not (clips_dir / "old-no-label.mp4").exists()
        # clips row gone too
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT * FROM clips").fetchall()
        conn.close()
        assert rows == []

    def test_old_labelled_clip_kept(self, setup):
        cleaner, db_path, clips_dir = setup
        _insert_event(db_path, "old-correct", feedback="correct")
        _insert_clip(db_path, "old-correct")
        _make_clip_file(clips_dir, "old-correct", age_days=60)

        _prune_clips_only(cleaner)

        # File preserved
        assert (clips_dir / "old-correct.mp4").exists()
        # clips row preserved
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT feedback_token FROM clips").fetchall()
        conn.close()
        assert [r[0] for r in rows] == ["old-correct"]

    def test_fresh_unlabelled_clip_kept(self, setup):
        cleaner, db_path, clips_dir = setup
        _insert_event(db_path, "fresh", feedback=None)
        _insert_clip(db_path, "fresh")
        _make_clip_file(clips_dir, "fresh", age_days=1)

        _prune_clips_only(cleaner)

        assert (clips_dir / "fresh.mp4").exists()

    def test_mixed_set(self, setup):
        cleaner, db_path, clips_dir = setup
        # Three clips: old-unlabelled (delete), old-labelled (keep),
        # fresh (keep).
        for tok, age, fb in [
            ("a-old-no-fb", 60, None),
            ("b-old-fp",    60, "false_positive"),
            ("c-fresh",      1, None),
        ]:
            _insert_event(db_path, tok, fb)
            _insert_clip(db_path, tok)
            _make_clip_file(clips_dir, tok, age_days=age)

        _prune_clips_only(cleaner)

        remaining = sorted(p.name for p in clips_dir.iterdir())
        assert remaining == ["b-old-fp.mp4", "c-fresh.mp4"]

    def test_corresponding_db_row_deleted(self, setup):
        cleaner, db_path, clips_dir = setup
        _insert_event(db_path, "tok", None)
        _insert_clip(db_path, "tok")
        _make_clip_file(clips_dir, "tok", age_days=90)

        _prune_clips_only(cleaner)

        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT feedback_token FROM clips").fetchall()
        conn.close()
        assert rows == []

    def test_no_clips_dir_is_noop(self, tmp_path):
        # clips_dir=None → method skipped entirely (the public _run does
        # the gating; here we just verify _prune_clips itself early-exits).
        snapshot_dir = tmp_path / "snapshots"
        snapshot_dir.mkdir()
        db_path = str(tmp_path / "openring.db")
        _create_schema(db_path)

        cleaner = RetentionCleaner(
            snapshot_dir=str(snapshot_dir),
            db_path=db_path,
            retention_days=30,
            clips_dir=None,  # disabled
        )
        # Should not raise
        cleaner._run()

    def test_missing_clips_table_no_crash(self, tmp_path):
        """Pre-clipper deployments don't have the clips table.  The
        prune flow should drop file-system entries cleanly and leave
        the DB-row deletion as a quiet no-op."""
        snapshot_dir = tmp_path / "snapshots"
        clips_dir = tmp_path / "clips"
        snapshot_dir.mkdir()
        clips_dir.mkdir()
        db_path = str(tmp_path / "openring.db")
        _create_schema(db_path, with_clips_table=False)
        _insert_event(db_path, "orphan", None)
        _make_clip_file(clips_dir, "orphan", age_days=60)

        cleaner = RetentionCleaner(
            snapshot_dir=str(snapshot_dir),
            db_path=db_path,
            retention_days=30,
            clips_dir=str(clips_dir),
        )
        _prune_clips_only(cleaner)

        # File still got deleted; DB-row delete was a quiet no-op
        assert not (clips_dir / "orphan.mp4").exists()

    def test_disabled_retention_skips_clips(self, setup):
        cleaner, db_path, clips_dir = setup
        cleaner.retention_days = 0
        _insert_event(db_path, "old", None)
        _insert_clip(db_path, "old")
        _make_clip_file(clips_dir, "old", age_days=999)

        cleaner._run()
        # retention_days <= 0 disables everything, including clip prune
        assert (clips_dir / "old.mp4").exists()
