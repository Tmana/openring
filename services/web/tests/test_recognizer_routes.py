"""Tests for /admin/recognizer routes (v0.4 PR-B enrollment UI).

Auth is disabled in conftest's MOCK_CONFIG so the admin gate doesn't
need an active session.  We monkeypatch the redis publish to a no-op
because the test environment doesn't run a real broker.
"""

from __future__ import annotations

import importlib
import io
from pathlib import Path

import pytest


@pytest.fixture
def fresh_recognizer_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the web-side recognizer_db at a per-test sqlite file.

    Also points the references_dir at a temp dir so uploaded photos
    don't contaminate the host's /data tree.
    """
    db_path = tmp_path / "recognizer.db"
    refs = tmp_path / "face-references"
    refs.mkdir()
    monkeypatch.setenv("RECOGNIZER_DB_PATH", str(db_path))
    monkeypatch.setenv("FACE_REFERENCES_DIR", str(refs))

    import recognizer_db as _rdb
    importlib.reload(_rdb)
    _rdb.init_db()

    # Stub the publish so tests don't need Redis.
    from routes import recognizer as _rec_route
    importlib.reload(_rec_route)
    monkeypatch.setattr(_rec_route, "_publish_enrollment", lambda *a, **kw: None)

    return _rdb


# tiny valid 1x1 JPEG (header + minimal data) — enough to pass the
# magic-number sniff in _save_photo without dragging Pillow into tests.
_TINY_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n"
    b"\xff\xd9"
)
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class TestListPage:
    def test_empty_list_renders(self, client, fresh_recognizer_db):
        r = client.get("/admin/recognizer")
        assert r.status_code == 200
        assert "No faces enrolled yet" in r.text

    def test_renders_enrolled_face(self, client, fresh_recognizer_db):
        fresh_recognizer_db.insert_known_face("Sarah", "household")
        r = client.get("/admin/recognizer")
        assert r.status_code == 200
        assert "Sarah" in r.text
        assert "household" in r.text


class TestCreate:
    def test_creates_face_with_no_photos(self, client, fresh_recognizer_db):
        r = client.post(
            "/admin/recognizer",
            data={"label": "Bob", "notes": "neighbour"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        # DB row written
        faces = fresh_recognizer_db.list_faces()
        assert len(faces) == 1
        assert faces[0]["label"] == "Bob"
        assert faces[0]["notes"] == "neighbour"
        assert faces[0]["enabled"] is True

    def test_creates_face_with_photos(self, client, fresh_recognizer_db):
        files = [
            ("photos", ("a.jpg", io.BytesIO(_TINY_JPEG), "image/jpeg")),
            ("photos", ("b.png", io.BytesIO(_TINY_PNG), "image/png")),
        ]
        r = client.post(
            "/admin/recognizer",
            data={"label": "Sarah"},
            files=files,
            follow_redirects=False,
        )
        assert r.status_code == 302
        faces = fresh_recognizer_db.list_faces()
        assert len(faces) == 1
        assert len(faces[0]["photos"]) == 2

    def test_rejects_bad_label(self, client, fresh_recognizer_db):
        r = client.post(
            "/admin/recognizer",
            data={"label": "Sarah, the second"},  # comma is forbidden
            follow_redirects=False,
        )
        # Redirect with error message
        assert r.status_code == 302
        assert "error=" in r.headers["location"]
        assert fresh_recognizer_db.list_faces() == []

    def test_rejects_duplicate_label_case_insensitive(self, client, fresh_recognizer_db):
        client.post("/admin/recognizer", data={"label": "Sarah"})
        r = client.post(
            "/admin/recognizer",
            data={"label": "SARAH"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "already%20exists" in r.headers["location"]
        assert len(fresh_recognizer_db.list_faces()) == 1

    def test_rejects_oversized_photo(self, client, fresh_recognizer_db):
        # 11 MB body — over the 10 MB limit
        too_big = _TINY_JPEG + b"\x00" * (11 * 1024 * 1024)
        files = [("photos", ("big.jpg", io.BytesIO(too_big), "image/jpeg"))]
        r = client.post(
            "/admin/recognizer",
            data={"label": "Mallory"},
            files=files,
            follow_redirects=False,
        )
        assert r.status_code == 302
        # Face was created, but photo was rejected
        faces = fresh_recognizer_db.list_faces()
        assert len(faces) == 1
        assert faces[0]["photos"] == []

    def test_rejects_non_image_upload(self, client, fresh_recognizer_db):
        files = [("photos", ("a.txt", io.BytesIO(b"hello world"), "text/plain"))]
        r = client.post(
            "/admin/recognizer",
            data={"label": "Eve"},
            files=files,
            follow_redirects=False,
        )
        assert r.status_code == 302
        faces = fresh_recognizer_db.list_faces()
        assert faces[0]["photos"] == []


class TestEdit:
    def test_toggle_enabled(self, client, fresh_recognizer_db):
        fid = fresh_recognizer_db.insert_known_face("Bob", "")
        r = client.post(
            f"/admin/recognizer/{fid}/edit",
            data={"label": "Bob", "notes": "", "enabled": ""},  # no checkbox = disabled
            follow_redirects=False,
        )
        assert r.status_code == 302
        face = fresh_recognizer_db.get_face(fid)
        assert face is not None
        assert face["enabled"] == 0

    def test_rename(self, client, fresh_recognizer_db):
        fid = fresh_recognizer_db.insert_known_face("Bob", "")
        r = client.post(
            f"/admin/recognizer/{fid}/edit",
            data={"label": "Robert", "notes": "renamed", "enabled": "on"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        face = fresh_recognizer_db.get_face(fid)
        assert face is not None
        assert face["label"] == "Robert"
        assert face["notes"] == "renamed"


class TestDelete:
    def test_deletes_face_and_photos(self, client, fresh_recognizer_db, tmp_path):
        fid = fresh_recognizer_db.insert_known_face("Sarah", "")
        # Drop a fake photo so we can verify the cleanup.
        face_dir = Path(fresh_recognizer_db.REFERENCES_DIR) / str(fid)
        face_dir.mkdir()
        (face_dir / "1.jpg").write_bytes(_TINY_JPEG)

        r = client.post(f"/admin/recognizer/{fid}/delete", follow_redirects=False)
        assert r.status_code == 302
        assert fresh_recognizer_db.get_face(fid) is None
        assert not face_dir.exists()


class TestPhotoServing:
    def test_serves_existing_photo(self, client, fresh_recognizer_db):
        fid = fresh_recognizer_db.insert_known_face("Sarah", "")
        face_dir = Path(fresh_recognizer_db.REFERENCES_DIR) / str(fid)
        face_dir.mkdir()
        (face_dir / "1.jpg").write_bytes(_TINY_JPEG)

        r = client.get(f"/admin/recognizer/{fid}/photo/1.jpg")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/")
        assert r.content == _TINY_JPEG

    def test_404_on_missing(self, client, fresh_recognizer_db):
        fid = fresh_recognizer_db.insert_known_face("Sarah", "")
        r = client.get(f"/admin/recognizer/{fid}/photo/nope.jpg")
        assert r.status_code == 404

    def test_path_traversal_attempt_404s(self, client, fresh_recognizer_db):
        fid = fresh_recognizer_db.insert_known_face("Sarah", "")
        face_dir = Path(fresh_recognizer_db.REFERENCES_DIR) / str(fid)
        face_dir.mkdir()
        (face_dir / "1.jpg").write_bytes(_TINY_JPEG)

        # FastAPI normalises path components in route matching, so a literal
        # ".." won't even reach our handler with most clients — but verify
        # that a filename that escapes the listing returns 404 either way.
        r = client.get(f"/admin/recognizer/{fid}/photo/..%2F..%2Fetc%2Fpasswd")
        assert r.status_code == 404
