"""Tests for ``services/web/src/routes/audio.py``.

The route mints HS256 JWTs that ``services/audio-relay/src/auth.py``
verifies.  We exercise the route end-to-end with the conftest's
TestClient and round-trip the resulting token through the shared
``audio_jwt.verify`` helper to prove the two services agree.
"""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

import auth as auth_module
import pytest
from audio_jwt import verify


# ── Fixture: enable an audio-key + register a device ─────────────────


_TEST_KEY_BYTES = b"\x33" * 32
_TEST_KEY_B64 = base64.b64encode(_TEST_KEY_BYTES).decode()


@pytest.fixture(autouse=True)
def fresh_auth_db():
    """Init + wipe the conftest's standard AUTH_DB_PATH between tests."""
    path = os.environ["AUTH_DB_PATH"]
    auth_module.init_db(path)
    db = auth_module.get_db()
    try:
        for tbl in ("device_tokens", "sessions", "api_tokens",
                    "audit_events", "users", "login_attempts"):
            try:
                db.execute(f"DELETE FROM {tbl}")
            except Exception:
                pass
        db.commit()
    finally:
        db.close()


@pytest.fixture
def with_audio_key(monkeypatch):
    monkeypatch.setenv("OPENRING_AUDIO_KEY", _TEST_KEY_B64)


def _register_device(device_id: str = "front-door") -> str:
    """Insert a device_tokens row directly so the route's lookup
    succeeds without going through the pairing window."""
    db = auth_module.get_db()
    try:
        return auth_module.create_device_token(db, device_id, device_id)
    finally:
        db.close()


# ── /api/audio/session ───────────────────────────────────────────────


class TestCreateSession:
    def test_unknown_device_404(self, client, with_audio_key):
        resp = client.post("/api/audio/session", json={"device_id": "ghost"})
        assert resp.status_code == 404
        assert "not registered" in resp.json()["error"]

    def test_missing_device_id_400(self, client, with_audio_key):
        resp = client.post("/api/audio/session", json={})
        assert resp.status_code == 400

    def test_invalid_json_400(self, client, with_audio_key):
        resp = client.post(
            "/api/audio/session",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_missing_audio_key_503(self, client, monkeypatch):
        monkeypatch.delenv("OPENRING_AUDIO_KEY", raising=False)
        _register_device()
        resp = client.post("/api/audio/session", json={"device_id": "front-door"})
        assert resp.status_code == 503
        assert "audio is disabled" in resp.json()["error"]

    def test_happy_path_returns_signed_jwt(self, client, with_audio_key):
        _register_device()
        resp = client.post("/api/audio/session", json={"device_id": "front-door"})
        assert resp.status_code == 200
        body = resp.json()
        assert "audio_url" in body
        assert body["expires_in_seconds"] == 300
        # Extract the JWT from the URL and verify with the same key
        # the audio-relay would use.
        assert "?token=" in body["audio_url"]
        jwt = body["audio_url"].split("?token=", 1)[1]
        claims = verify(jwt, _TEST_KEY_BYTES)
        assert claims.device_id == "front-door"
        assert claims.jti  # randomly generated, just check non-empty

    def test_url_uses_request_host(self, client, with_audio_key):
        _register_device()
        # Default test client uses host="testserver"; the URL should
        # carry that hostname (with the relay port appended).
        resp = client.post("/api/audio/session", json={"device_id": "front-door"})
        assert resp.status_code == 200
        url = resp.json()["audio_url"]
        assert "testserver" in url
        assert ":8765/audio/browser?token=" in url

    def test_distinct_calls_get_distinct_jtis(self, client, with_audio_key):
        _register_device()
        first = client.post("/api/audio/session", json={"device_id": "front-door"})
        second = client.post("/api/audio/session", json={"device_id": "front-door"})
        jwt1 = first.json()["audio_url"].split("?token=", 1)[1]
        jwt2 = second.json()["audio_url"].split("?token=", 1)[1]
        c1 = verify(jwt1, _TEST_KEY_BYTES)
        c2 = verify(jwt2, _TEST_KEY_BYTES)
        assert c1.jti != c2.jti

    def test_disabled_device_404(self, client, with_audio_key):
        _register_device()
        db = auth_module.get_db()
        try:
            auth_module.disable_device(db, "front-door")
        finally:
            db.close()
        resp = client.post("/api/audio/session", json={"device_id": "front-door"})
        # The lookup filters disabled=0 — disabled devices should look
        # like they're not registered.
        assert resp.status_code == 404
