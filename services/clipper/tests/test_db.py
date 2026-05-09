"""clips-table SQLite layer tests."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "openring.db"))
    import db as _db
    importlib.reload(_db)
    _db.init_db()
    return _db


class TestInsertAndLookup:
    def test_insert_then_lookup(self, fresh_db) -> None:
        fresh_db.insert_clip(
            feedback_token="abc123",
            camera_name="front-door",
            clip_path="/data/clips/abc123.mp4",
            pre_roll_seconds=5.0,
            post_roll_seconds=5.0,
            duration_seconds=10.0,
            size_bytes=12345,
            segments_used=10,
        )
        row = fresh_db.get_clip_for_token("abc123")
        assert row is not None
        assert row["camera_name"] == "front-door"
        assert row["clip_path"] == "/data/clips/abc123.mp4"
        assert row["size_bytes"] == 12345
        assert row["error"] is None

    def test_unknown_token_returns_none(self, fresh_db) -> None:
        assert fresh_db.get_clip_for_token("does-not-exist") is None

    def test_empty_token_returns_none(self, fresh_db) -> None:
        assert fresh_db.get_clip_for_token("") is None

    def test_re_insert_updates_in_place(self, fresh_db) -> None:
        # Same feedback_token (rare retry case): values overwrite, no
        # duplicate row.
        for size in (100, 200):
            fresh_db.insert_clip(
                feedback_token="abc",
                camera_name="cam",
                clip_path="/data/clips/abc.mp4",
                pre_roll_seconds=5.0,
                post_roll_seconds=5.0,
                duration_seconds=10.0,
                size_bytes=size,
                segments_used=10,
            )
        # One row, latest size
        rows = []
        import sqlite3
        conn = sqlite3.connect(fresh_db.DB_PATH)
        rows = conn.execute("SELECT size_bytes FROM clips WHERE feedback_token='abc'").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == 200


class TestInsertFailure:
    def test_failure_persists_error(self, fresh_db) -> None:
        fresh_db.insert_failure("xyz", "front-door", "no segments in window")
        row = fresh_db.get_clip_for_token("xyz")
        assert row is not None
        assert row["error"] == "no segments in window"
        assert row["clip_path"] == ""
        assert row["segments_used"] == 0

    def test_failure_overwrites_existing(self, fresh_db) -> None:
        fresh_db.insert_clip(
            feedback_token="t", camera_name="cam", clip_path="/a.mp4",
            pre_roll_seconds=5, post_roll_seconds=5, duration_seconds=10,
            size_bytes=10, segments_used=2,
        )
        fresh_db.insert_failure("t", "cam", "later failure")
        row = fresh_db.get_clip_for_token("t")
        # Failure marked even though the original row had a clip path
        assert row["error"] == "later failure"
        assert row["segments_used"] == 0

    def test_long_error_truncated(self, fresh_db) -> None:
        long_err = "x" * 1000
        fresh_db.insert_failure("t", "cam", long_err)
        row = fresh_db.get_clip_for_token("t")
        assert row is not None
        assert len(row["error"]) <= 500
