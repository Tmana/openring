"""Recognizer-side enrollment dispatch tests.

We mock out ``recognizer.embed_image`` so the tests don't need dlib —
the dispatch / persistence path is what we're verifying here.
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


@pytest.fixture
def references_dir(tmp_path: Path) -> Path:
    d = tmp_path / "face-references"
    d.mkdir()
    return d


def _stub_embed_image(monkeypatch, blob: bytes = b"\x00" * 512, error: str | None = None):
    """Replace recognizer.embed_image so the test doesn't need dlib."""
    import recognizer
    monkeypatch.setattr(recognizer, "embed_image", lambda _path: (blob, error))


class TestEmbedOne:
    def test_writes_embedding_row(self, fresh_db, references_dir, monkeypatch):
        import enrollment

        fid = fresh_db.insert_known_face("Sarah")
        face_dir = references_dir / str(fid)
        face_dir.mkdir()
        photo = face_dir / "1.jpg"
        photo.write_bytes(b"fake jpeg")

        _stub_embed_image(monkeypatch)
        enrollment.embed_one(fid, str(photo))

        rows = fresh_db.list_enabled_faces()
        assert len(rows[0]["embeddings"]) == 1

    def test_idempotent(self, fresh_db, references_dir, monkeypatch):
        import enrollment

        fid = fresh_db.insert_known_face("Bob")
        face_dir = references_dir / str(fid)
        face_dir.mkdir()
        photo = face_dir / "1.jpg"
        photo.write_bytes(b"fake jpeg")

        _stub_embed_image(monkeypatch)
        enrollment.embed_one(fid, str(photo))
        enrollment.embed_one(fid, str(photo))  # duplicate

        rows = fresh_db.list_enabled_faces()
        assert len(rows[0]["embeddings"]) == 1  # not 2

    def test_skips_missing_photo(self, fresh_db, references_dir, monkeypatch):
        import enrollment
        _stub_embed_image(monkeypatch)
        # Should not raise, just log
        enrollment.embed_one(99, str(references_dir / "nope" / "1.jpg"))

    def test_skips_when_embed_returns_error(self, fresh_db, references_dir, monkeypatch):
        import enrollment

        fid = fresh_db.insert_known_face("Eve")
        face_dir = references_dir / str(fid)
        face_dir.mkdir()
        photo = face_dir / "1.jpg"
        photo.write_bytes(b"fake jpeg")

        _stub_embed_image(monkeypatch, error="no face detected in reference photo")
        enrollment.embed_one(fid, str(photo))

        rows = fresh_db.list_enabled_faces()
        assert rows[0]["embeddings"] == []


class TestSweep:
    def test_sweep_embeds_orphan_photos(self, fresh_db, references_dir, monkeypatch):
        import enrollment
        from settings import RecognizerSettings

        fid = fresh_db.insert_known_face("Sarah")
        face_dir = references_dir / str(fid)
        face_dir.mkdir()
        (face_dir / "1.jpg").write_bytes(b"fake")
        (face_dir / "2.png").write_bytes(b"fake")

        _stub_embed_image(monkeypatch)
        s = RecognizerSettings(references_dir=str(references_dir))
        n = enrollment.sweep(s)
        assert n == 2

    def test_sweep_skips_already_embedded(self, fresh_db, references_dir, monkeypatch):
        import enrollment
        from settings import RecognizerSettings

        fid = fresh_db.insert_known_face("Sarah")
        face_dir = references_dir / str(fid)
        face_dir.mkdir()
        (face_dir / "1.jpg").write_bytes(b"fake")

        _stub_embed_image(monkeypatch)
        s = RecognizerSettings(references_dir=str(references_dir))
        enrollment.sweep(s)
        # Second sweep: nothing new to do.
        n = enrollment.sweep(s)
        assert n == 0

    def test_sweep_skips_non_integer_dirs(self, fresh_db, references_dir, monkeypatch):
        import enrollment
        from settings import RecognizerSettings

        # Stray directory named "thumbs" — sweep should ignore.
        (references_dir / "thumbs").mkdir()
        (references_dir / "thumbs" / "1.jpg").write_bytes(b"fake")

        _stub_embed_image(monkeypatch)
        s = RecognizerSettings(references_dir=str(references_dir))
        assert enrollment.sweep(s) == 0


class TestEnrollmentMessageGate:
    """Validate _handle_enrollment's path-traversal defence."""

    def test_rejects_path_outside_references_dir(self, fresh_db, monkeypatch):
        import main
        from settings import RecognizerSettings

        called = {"n": 0}

        def fake_embed(*a, **kw):
            called["n"] += 1
        monkeypatch.setattr("enrollment.embed_one", fake_embed)

        s = RecognizerSettings(references_dir="/data/face-references")
        main._handle_enrollment(
            {"face_id": 1, "photo_path": "/etc/passwd"}, s,
        )
        assert called["n"] == 0

    def test_rejects_missing_face_id(self, fresh_db, monkeypatch):
        import main
        from settings import RecognizerSettings

        called = {"n": 0}
        monkeypatch.setattr("enrollment.embed_one", lambda *a, **kw: called.update(n=called["n"] + 1))

        s = RecognizerSettings(references_dir="/data/face-references")
        main._handle_enrollment({"photo_path": "/data/face-references/1/1.jpg"}, s)
        assert called["n"] == 0

    def test_rejects_traversal_attempt(self, fresh_db, monkeypatch):
        import main
        from settings import RecognizerSettings

        called = {"n": 0}
        monkeypatch.setattr("enrollment.embed_one", lambda *a, **kw: called.update(n=called["n"] + 1))

        s = RecognizerSettings(references_dir="/data/face-references")
        main._handle_enrollment(
            {"face_id": 1, "photo_path": "/data/face-references/1/../../etc/passwd"}, s,
        )
        assert called["n"] == 0

    def test_accepts_valid_path(self, fresh_db, monkeypatch):
        import main
        from settings import RecognizerSettings

        called = {"args": None}

        def fake(face_id, photo_path):
            called["args"] = (face_id, photo_path)
        monkeypatch.setattr("enrollment.embed_one", fake)

        s = RecognizerSettings(references_dir="/data/face-references")
        main._handle_enrollment(
            {"face_id": 7, "photo_path": "/data/face-references/7/1.jpg"}, s,
        )
        assert called["args"] == (7, "/data/face-references/7/1.jpg")
