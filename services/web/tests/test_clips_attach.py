"""Tests for the v0.2 clips attachment in routes/events.py + clips_db.

Both the read-only DB layer and the route-level _attach_clips helper
need to handle the (common) case where the clipper hasn't run yet —
no clips table, no rows, no errors — and the (less common) case where
a clip row exists but recorded a failure.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def fresh_clips_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Reload clips_db pointed at an isolated DB."""
    db_path = str(tmp_path / "openring.db")
    monkeypatch.setenv("DB_PATH", db_path)
    import clips_db
    importlib.reload(clips_db)
    return clips_db, db_path


def _create_clips_table(db_path: str) -> None:
    """Mirror the schema clipper.db.init_db creates."""
    conn = sqlite3.connect(db_path)
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


def _insert_clip(db_path: str, **kwargs) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO clips
            (feedback_token, camera_name, clip_path, duration_seconds,
             size_bytes, segments_used, created_at, error,
             pre_roll_seconds, post_roll_seconds)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            kwargs.get("feedback_token", "tok"),
            kwargs.get("camera_name", "front-door"),
            kwargs.get("clip_path", "/data/clips/tok.mp4"),
            kwargs.get("duration_seconds", 10.0),
            kwargs.get("size_bytes", 12345),
            kwargs.get("segments_used", 10),
            "2026-05-09T18:00:00+00:00",
            kwargs.get("error"),
            kwargs.get("pre_roll_seconds", 5.0),
            kwargs.get("post_roll_seconds", 5.0),
        ),
    )
    conn.commit()
    conn.close()


class TestGetClipsForTokens:
    def test_missing_db_returns_empty(self, monkeypatch, tmp_path):
        # Point at a nonexistent file — _connect returns None; helper
        # returns {} without raising.
        monkeypatch.setenv("DB_PATH", str(tmp_path / "no-such.db"))
        import clips_db
        importlib.reload(clips_db)
        assert clips_db.get_clips_for_tokens(["x"]) == {}

    def test_missing_table_returns_empty(self, fresh_clips_db):
        clips_db, db_path = fresh_clips_db
        # DB exists (empty) but no clips table — OperationalError swallowed.
        # Need to actually create the file first.
        sqlite3.connect(db_path).close()
        assert clips_db.get_clips_for_tokens(["x"]) == {}

    def test_empty_tokens_skips_db(self, fresh_clips_db):
        clips_db, _ = fresh_clips_db
        # Should never even try to open the DB — short-circuits on the
        # empty list.  No exception even though the file doesn't exist.
        assert clips_db.get_clips_for_tokens([]) == {}

    def test_returns_matching_rows(self, fresh_clips_db):
        clips_db, db_path = fresh_clips_db
        _create_clips_table(db_path)
        _insert_clip(db_path, feedback_token="abc", clip_path="/data/clips/abc.mp4")
        _insert_clip(db_path, feedback_token="def", clip_path="/data/clips/def.mp4")
        result = clips_db.get_clips_for_tokens(["abc", "def", "missing"])
        assert set(result.keys()) == {"abc", "def"}
        assert result["abc"]["clip_path"] == "/data/clips/abc.mp4"

    def test_failure_rows_returned_with_error(self, fresh_clips_db):
        clips_db, db_path = fresh_clips_db
        _create_clips_table(db_path)
        _insert_clip(
            db_path,
            feedback_token="bad",
            clip_path="",
            error="ffmpeg concat failed",
            size_bytes=None,
        )
        result = clips_db.get_clips_for_tokens(["bad"])
        assert result["bad"]["error"] == "ffmpeg concat failed"
        assert result["bad"]["clip_path"] == ""


class TestAttachClips:
    """The route-level _attach_clips wraps clips_db.get_clips_for_tokens
    and decides how to render the URL.  Successful clips get a /clips/
    URL; failed clips and no-token events stay None."""

    def _events(self, tokens):
        return [{"feedback_token": t} for t in tokens]

    def test_success_gets_url(self, fresh_clips_db, monkeypatch):
        clips_db, db_path = fresh_clips_db
        _create_clips_table(db_path)
        _insert_clip(db_path, feedback_token="t1", clip_path="/data/clips/t1.mp4")

        from routes.events import _attach_clips
        out = _attach_clips(self._events(["t1"]))
        assert out[0]["clip"] is not None
        assert out[0]["clip"]["url"] == "/clips/t1.mp4"

    def test_failure_row_has_no_url(self, fresh_clips_db, monkeypatch):
        clips_db, db_path = fresh_clips_db
        _create_clips_table(db_path)
        _insert_clip(
            db_path, feedback_token="bad", clip_path="",
            error="boom", size_bytes=None,
        )
        from routes.events import _attach_clips
        out = _attach_clips(self._events(["bad"]))
        # Row exists but has no usable URL — caller falls back to "no clip"
        assert out[0]["clip"] is not None
        assert "url" not in out[0]["clip"]
        assert out[0]["clip"]["error"] == "boom"

    def test_unknown_token_gets_none(self, fresh_clips_db):
        clips_db, db_path = fresh_clips_db
        _create_clips_table(db_path)

        from routes.events import _attach_clips
        out = _attach_clips(self._events(["does-not-exist"]))
        assert out[0]["clip"] is None

    def test_event_without_token_gets_none(self, fresh_clips_db):
        from routes.events import _attach_clips
        out = _attach_clips([{"id": 1}])
        assert out[0]["clip"] is None

    def test_basename_used_in_url(self, fresh_clips_db):
        # Defence-in-depth: even if clip_path includes path traversal
        # bits, only the basename is rendered into the URL.
        clips_db, db_path = fresh_clips_db
        _create_clips_table(db_path)
        _insert_clip(
            db_path, feedback_token="weird",
            clip_path="/data/clips/../../etc/passwd.mp4",
        )
        from routes.events import _attach_clips
        out = _attach_clips(self._events(["weird"]))
        assert out[0]["clip"]["url"] == "/clips/passwd.mp4"
