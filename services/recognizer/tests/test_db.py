"""recognizer.db SQLite schema + writer tests.

Don't test face_recognition here — that's exercised in test_recognizer.py
(skipped when dlib isn't installed).  This file is the cheap, fast
contract between the schema and the rest of the service.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RECOGNIZER_DB_PATH", str(tmp_path / "recognizer.db"))
    import db as _db
    importlib.reload(_db)
    _db.init_db()
    return _db


class TestKnownFaces:
    def test_insert_and_list_empty_no_embeddings(self, fresh_db) -> None:
        fid = fresh_db.insert_known_face("Sarah", "household")
        assert fid > 0
        # No embeddings yet — list_enabled_faces should still surface
        # the row, with an empty embeddings list, so PR-B can show
        # "needs photos" in the UI.
        rows = fresh_db.list_enabled_faces()
        assert len(rows) == 1
        assert rows[0]["label"] == "Sarah"
        assert rows[0]["embeddings"] == []

    def test_label_unique_case_insensitive(self, fresh_db) -> None:
        fresh_db.insert_known_face("Sarah")
        with pytest.raises(Exception):
            fresh_db.insert_known_face("sarah")

    def test_disabled_face_not_listed(self, fresh_db) -> None:
        fid = fresh_db.insert_known_face("Bob")
        # Soft-delete via direct SQL so we don't need an admin API yet.
        import sqlite3
        conn = sqlite3.connect(fresh_db.DB_PATH)
        conn.execute("UPDATE known_faces SET enabled = 0 WHERE id = ?", (fid,))
        conn.commit()
        conn.close()
        rows = fresh_db.list_enabled_faces()
        assert rows == []


class TestEmbeddings:
    def test_insert_and_round_trip(self, fresh_db) -> None:
        fid = fresh_db.insert_known_face("Bob")
        # 128 float32s = 512 bytes — face_recognition's exact shape.
        blob = b"\x00" * 512
        fresh_db.insert_embedding(fid, blob, "bob/1.jpg")
        rows = fresh_db.list_enabled_faces()
        assert len(rows) == 1
        assert len(rows[0]["embeddings"]) == 1
        assert rows[0]["embeddings"][0] == blob

    def test_cascade_on_face_delete(self, fresh_db) -> None:
        # FK ON DELETE CASCADE means deleting a face wipes its
        # embeddings — verifies foreign_keys=ON took effect.
        fid = fresh_db.insert_known_face("Bob")
        fresh_db.insert_embedding(fid, b"\x00" * 512, "bob/1.jpg")
        import sqlite3
        conn = sqlite3.connect(fresh_db.DB_PATH)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM known_faces WHERE id = ?", (fid,))
        conn.commit()
        cnt = conn.execute("SELECT COUNT(*) FROM face_embeddings").fetchone()[0]
        conn.close()
        assert cnt == 0


class TestRecognitions:
    def test_insert_matched_row(self, fresh_db) -> None:
        fid = fresh_db.insert_known_face("Sarah")
        fresh_db.insert_recognition(
            feedback_token="abc12345",
            camera_name="front-door",
            status="matched",
            face_id=fid,
            label="Sarah",
            score=0.42,
            bbox="[10,20,110,180]",
        )
        import sqlite3
        conn = sqlite3.connect(fresh_db.DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM recognitions WHERE feedback_token = ?", ("abc12345",)
        ).fetchone()
        conn.close()
        assert row["status"] == "matched"
        assert row["label"] == "Sarah"
        assert row["face_id"] == fid
        assert row["score"] == pytest.approx(0.42)

    def test_unique_feedback_token_upserts(self, fresh_db) -> None:
        # Re-fired event for the same token must overwrite, not duplicate
        fresh_db.insert_recognition(
            feedback_token="t" * 16,
            camera_name="front-door",
            status="unknown",
        )
        fresh_db.insert_recognition(
            feedback_token="t" * 16,
            camera_name="front-door",
            status="matched",
            face_id=1,
            label="Sarah",
        )
        import sqlite3
        conn = sqlite3.connect(fresh_db.DB_PATH)
        rows = conn.execute(
            "SELECT status FROM recognitions WHERE feedback_token = ?", ("t" * 16,)
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "matched"

    def test_no_face_status(self, fresh_db) -> None:
        # The "we looked, found nothing" outcome is distinct from
        # "we never looked" — verify it persists.
        fresh_db.insert_recognition(
            feedback_token="x" * 16,
            camera_name="back-door",
            status="no_face",
        )
        import sqlite3
        conn = sqlite3.connect(fresh_db.DB_PATH)
        row = conn.execute(
            "SELECT status, face_id, label FROM recognitions WHERE feedback_token = ?",
            ("x" * 16,),
        ).fetchone()
        conn.close()
        assert row[0] == "no_face"
        assert row[1] is None
        assert row[2] is None
