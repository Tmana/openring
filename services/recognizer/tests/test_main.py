"""Recognizer dispatch / publish tests — no dlib required.

We mock recognize_crop entirely so the unit tests stay fast and don't
need a face-recognition wheel at test time.  The real model is exercised
by an integration smoke test on a deployment, not in pytest.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RECOGNIZER_DB_PATH", str(tmp_path / "recognizer.db"))
    import db as _db
    importlib.reload(_db)
    _db.init_db()
    return _db


@pytest.fixture
def snapshot_dir(tmp_path: Path) -> Path:
    d = tmp_path / "snapshots"
    d.mkdir()
    return d


def _detection_event(token: str = "abcd1234efgh5678") -> dict:
    return {
        "feedback_token": token,
        "camera_name": "front-door",
        "class_name": "person",
        "confidence": 0.85,
        "bbox": [10, 20, 110, 180],
        "timestamp": "2026-05-05T12:00:00+00:00",
    }


class TestProcessEvent:
    def test_rejects_invalid_token(self, fresh_db, snapshot_dir, monkeypatch) -> None:
        # Path-traversal defence: any feedback_token outside the regex
        # is dropped without touching the filesystem.
        import main
        from settings import RecognizerSettings

        s = RecognizerSettings(enabled=True, snapshots_dir=str(snapshot_dir))
        evt = _detection_event(token="../etc/passwd")
        redis_mock = MagicMock()

        # No snapshot file created — but with a valid token we'd touch it.
        # With this bad token we should return before that.
        main._process_event(s, evt, redis_mock, hmac_key=None)
        redis_mock.publish.assert_not_called()

    def test_records_error_when_snapshot_missing(
        self, fresh_db, snapshot_dir, monkeypatch
    ) -> None:
        import main
        from settings import RecognizerSettings

        s = RecognizerSettings(enabled=True, snapshots_dir=str(snapshot_dir))
        evt = _detection_event()
        redis_mock = MagicMock()

        main._process_event(s, evt, redis_mock, hmac_key=None)

        import sqlite3
        conn = sqlite3.connect(fresh_db.DB_PATH)
        row = conn.execute(
            "SELECT status, error FROM recognitions WHERE feedback_token = ?",
            (evt["feedback_token"],),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "error"
        assert row[1] is not None and "snapshot" in row[1]

    def test_publishes_recognition_on_match(
        self, fresh_db, snapshot_dir, monkeypatch
    ) -> None:
        import main
        from recognizer import RecognitionResult
        from settings import RecognizerSettings

        # Drop a stub snapshot so the file-existence check passes.
        token = _detection_event()["feedback_token"]
        (snapshot_dir / f"{token}.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        # Stub the actual face recognition — we're testing the
        # plumbing, not dlib.
        def fake_recognize_crop(*args, **kwargs):
            return RecognitionResult(
                status="matched",
                face_id=42,
                label="Sarah",
                score=0.31,
                bbox=(50, 60, 150, 190),
            )
        monkeypatch.setattr(main, "recognize_crop", fake_recognize_crop)

        s = RecognizerSettings(enabled=True, snapshots_dir=str(snapshot_dir))
        evt = _detection_event()
        redis_mock = MagicMock()

        main._process_event(s, evt, redis_mock, hmac_key=None)

        # DB row written
        import sqlite3
        conn = sqlite3.connect(fresh_db.DB_PATH)
        row = conn.execute(
            "SELECT status, label, face_id FROM recognitions WHERE feedback_token = ?",
            (token,),
        ).fetchone()
        conn.close()
        assert row == ("matched", "Sarah", 42)

        # Redis publish fired with parseable JSON to the recognition channel
        assert redis_mock.publish.called
        args, _ = redis_mock.publish.call_args
        assert args[0] == "openring:recognition"
        payload = json.loads(args[1])
        assert payload["status"] == "matched"
        assert payload["label"] == "Sarah"
        assert payload["face_id"] == 42
        assert payload["bbox"] == [50, 60, 150, 190]

    def test_skips_event_with_no_bbox(
        self, fresh_db, snapshot_dir, monkeypatch
    ) -> None:
        import main
        from settings import RecognizerSettings

        s = RecognizerSettings(enabled=True, snapshots_dir=str(snapshot_dir))
        evt = _detection_event()
        del evt["bbox"]
        redis_mock = MagicMock()

        main._process_event(s, evt, redis_mock, hmac_key=None)

        # No DB row, no publish — the event isn't actionable.
        import sqlite3
        conn = sqlite3.connect(fresh_db.DB_PATH)
        cnt = conn.execute("SELECT COUNT(*) FROM recognitions").fetchone()[0]
        conn.close()
        assert cnt == 0
        redis_mock.publish.assert_not_called()


class TestBboxParse:
    def test_valid_bbox(self) -> None:
        import main
        assert main._bbox_from_event({"bbox": [1, 2, 3, 4]}) == (1, 2, 3, 4)

    def test_garbage_bbox(self) -> None:
        import main
        assert main._bbox_from_event({"bbox": "nope"}) is None

    def test_short_bbox(self) -> None:
        import main
        assert main._bbox_from_event({"bbox": [1, 2]}) is None

    def test_no_bbox_field(self) -> None:
        import main
        assert main._bbox_from_event({}) is None
