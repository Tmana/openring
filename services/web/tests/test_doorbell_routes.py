"""Tests for routes/doorbell.py and the supporting auth/db helpers.

Auth is disabled in conftest's MOCK_CONFIG, so admin endpoints succeed
without a real session.  Device-side endpoints (register / press /
heartbeat) enforce their own auth model regardless of the user-auth
middleware setting.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import auth as auth_module
import db as web_db
import pytest


@pytest.fixture(autouse=True)
def fresh_auth_db():
    """Init (and reset between tests) the conftest's standard AUTH_DB_PATH.

    We can't redirect ``auth_module.get_db()`` to a per-test path because
    its default argument is captured at module-load time — patching
    ``auth_module.AUTH_DB_PATH`` after import is a no-op for
    ``get_db(db_path=AUTH_DB_PATH)``.  Instead we use the conftest path
    everywhere and wipe rows between tests so isolation is preserved.
    """
    path = os.environ["AUTH_DB_PATH"]
    auth_module.init_db(path)
    db = auth_module.get_db()
    try:
        # Wipe state from previous tests — devices first (no FK), then sessions/tokens.
        for tbl in ("device_tokens", "sessions", "api_tokens", "audit_events", "users", "login_attempts"):
            try:
                db.execute(f"DELETE FROM {tbl}")
            except Exception:
                pass
        db.commit()
    finally:
        db.close()
    yield path


@pytest.fixture
def fresh_app_state(monkeypatch):
    """Replace get_app_state/set_app_state with an in-memory dict so the
    pairing window state doesn't leak across tests via /tmp/sg-test.db."""
    state: dict[str, str] = {}
    monkeypatch.setattr(web_db, "get_app_state", lambda k: state.get(k))
    monkeypatch.setattr(web_db, "set_app_state", lambda k, v: state.update({k: v}))
    return state


def _open_window(state: dict[str, str], minutes: int = 5) -> str:
    expires = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    state["doorbell_pairing_until"] = expires
    return expires


# ── /api/doorbell/register ───────────────────────────────────────────────


class TestRegister:
    def test_closed_window_rejects(self, client, fresh_app_state) -> None:
        resp = client.post(
            "/api/doorbell/register",
            json={"device_id": "front-door", "label": "Front door"},
        )
        assert resp.status_code == 403
        assert "pairing window" in resp.json()["error"]

    def test_open_window_returns_token(self, client, fresh_app_state) -> None:
        _open_window(fresh_app_state)
        resp = client.post(
            "/api/doorbell/register",
            json={"device_id": "front-door", "label": "Front door"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["device_id"] == "front-door"
        assert body["label"] == "Front door"
        assert isinstance(body["device_token"], str) and len(body["device_token"]) > 30

    def test_re_register_rotates_token(self, client, fresh_app_state) -> None:
        _open_window(fresh_app_state)
        first = client.post(
            "/api/doorbell/register",
            json={"device_id": "front-door", "label": "Front door"},
        ).json()["device_token"]
        _open_window(fresh_app_state)
        second = client.post(
            "/api/doorbell/register",
            json={"device_id": "front-door", "label": "Front door (re-flashed)"},
        ).json()["device_token"]
        assert first != second
        # Old token no longer validates
        db = auth_module.get_db()
        try:
            assert auth_module.validate_device_token(db, first) is None
            assert auth_module.validate_device_token(db, second) is not None
        finally:
            db.close()

    @pytest.mark.parametrize("device_id", [
        "",                        # empty
        "Front Door",              # spaces + uppercase
        "front_door",              # underscore
        "../etc/passwd",           # path traversal
        "a" * 64,                  # too long
        "-leading-dash",           # leading dash
    ])
    def test_invalid_device_id_rejected(
        self, client, fresh_app_state, device_id,
    ) -> None:
        _open_window(fresh_app_state)
        resp = client.post(
            "/api/doorbell/register",
            json={"device_id": device_id, "label": "x"},
        )
        assert resp.status_code == 400

    def test_invalid_json_rejected(self, client, fresh_app_state) -> None:
        _open_window(fresh_app_state)
        resp = client.post(
            "/api/doorbell/register",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_label_defaults_to_device_id(self, client, fresh_app_state) -> None:
        _open_window(fresh_app_state)
        resp = client.post(
            "/api/doorbell/register",
            json={"device_id": "side-gate"},
        )
        assert resp.status_code == 200
        assert resp.json()["label"] == "side-gate"


# ── /api/doorbell/heartbeat ──────────────────────────────────────────────


class TestHeartbeat:
    def _register(self, client, state) -> str:
        _open_window(state)
        return client.post(
            "/api/doorbell/register",
            json={"device_id": "front-door", "label": "Front door"},
        ).json()["device_token"]

    def test_no_bearer_rejected(self, client, fresh_app_state) -> None:
        resp = client.post("/api/doorbell/heartbeat", json={"version": "x"})
        assert resp.status_code == 401

    def test_bad_bearer_rejected(self, client, fresh_app_state) -> None:
        resp = client.post(
            "/api/doorbell/heartbeat",
            json={"version": "x"},
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert resp.status_code == 401

    def test_valid_bearer_accepted(self, client, fresh_app_state) -> None:
        token = self._register(client, fresh_app_state)
        resp = client.post(
            "/api/doorbell/heartbeat",
            json={"version": "0.0.1", "uptime_seconds": 42, "cpu_temp_c": 50.1},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        # last_seen + telemetry persisted
        db = auth_module.get_db()
        try:
            devices = auth_module.list_devices(db)
        finally:
            db.close()
        assert len(devices) == 1
        assert devices[0]["last_seen_at"] is not None
        assert devices[0]["last_telemetry"] is not None
        assert json.loads(devices[0]["last_telemetry"])["version"] == "0.0.1"


# ── /api/doorbell/press ──────────────────────────────────────────────────


class TestPress:
    def _register(self, client, state) -> str:
        _open_window(state)
        return client.post(
            "/api/doorbell/register",
            json={"device_id": "front-door", "label": "Front door"},
        ).json()["device_token"]

    def test_no_bearer_rejected(self, client, fresh_app_state) -> None:
        resp = client.post("/api/doorbell/press", json={})
        assert resp.status_code == 401

    def test_publishes_to_redis(self, client, fresh_app_state) -> None:
        token = self._register(client, fresh_app_state)
        # Mock the aioredis client so the test doesn't need a real Redis.
        fake_redis = MagicMock()
        fake_redis.publish = AsyncMock(return_value=1)
        fake_redis.aclose = AsyncMock()
        with patch(
            "routes.doorbell.aioredis.Redis",
            return_value=fake_redis,
        ):
            resp = client.post(
                "/api/doorbell/press",
                json={"timestamp": "2026-05-09T18:42:11+00:00", "device_id": "front-door"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 202
        # One publish to openring:doorbell with a JSON payload
        assert fake_redis.publish.await_count == 1
        channel, payload_json = fake_redis.publish.await_args.args
        assert channel == "openring:doorbell"
        payload = json.loads(payload_json)
        assert payload["type"] == "doorbell_press"
        assert payload["device_id"] == "front-door"
        assert payload["device_timestamp"] == "2026-05-09T18:42:11+00:00"
        # received_at is the host's clock, distinct from device_timestamp
        assert "timestamp" in payload

    def test_press_does_not_overwrite_telemetry(
        self, client, fresh_app_state,
    ) -> None:
        token = self._register(client, fresh_app_state)
        # Heartbeat first → telemetry stored
        client.post(
            "/api/doorbell/heartbeat",
            json={"version": "0.0.1", "uptime_seconds": 100},
            headers={"Authorization": f"Bearer {token}"},
        )
        fake_redis = MagicMock()
        fake_redis.publish = AsyncMock(return_value=1)
        fake_redis.aclose = AsyncMock()
        with patch(
            "routes.doorbell.aioredis.Redis", return_value=fake_redis,
        ):
            client.post(
                "/api/doorbell/press",
                json={"timestamp": "x", "device_id": "front-door"},
                headers={"Authorization": f"Bearer {token}"},
            )
        # Telemetry from the heartbeat must survive
        db = auth_module.get_db()
        try:
            devices = auth_module.list_devices(db)
        finally:
            db.close()
        assert json.loads(devices[0]["last_telemetry"])["version"] == "0.0.1"


# ── /api/doorbell/pair-window admin ──────────────────────────────────────


class TestPairWindowAdmin:
    def test_open_sets_window(self, client, fresh_app_state) -> None:
        resp = client.post("/api/doorbell/pair-window/open")
        assert resp.status_code == 200
        body = resp.json()
        assert "expires_at" in body
        assert body["duration_seconds"] == 300

    def test_close_clears_window(self, client, fresh_app_state) -> None:
        client.post("/api/doorbell/pair-window/open")
        resp = client.post("/api/doorbell/pair-window/close")
        assert resp.status_code == 200
        # Subsequent register call is rejected
        reg = client.post(
            "/api/doorbell/register",
            json={"device_id": "front-door", "label": "x"},
        )
        assert reg.status_code == 403

    def test_status_reports_open_state(self, client, fresh_app_state) -> None:
        # Initially closed
        resp = client.get("/api/doorbell/pair-window")
        assert resp.status_code == 200
        assert resp.json()["open"] is False
        # Open and re-check
        client.post("/api/doorbell/pair-window/open")
        resp = client.get("/api/doorbell/pair-window")
        assert resp.json()["open"] is True


# ── /api/doorbell/devices admin ──────────────────────────────────────────


class TestDevicesAdmin:
    def test_list_empty(self, client, fresh_app_state) -> None:
        resp = client.get("/api/doorbell/devices")
        assert resp.status_code == 200
        assert resp.json() == {"devices": []}

    def test_list_after_register(self, client, fresh_app_state) -> None:
        _open_window(fresh_app_state)
        client.post(
            "/api/doorbell/register",
            json={"device_id": "front-door", "label": "Front door"},
        )
        resp = client.get("/api/doorbell/devices")
        devices = resp.json()["devices"]
        assert len(devices) == 1
        assert devices[0]["device_id"] == "front-door"
        # Last telemetry comes back as a parsed dict (not a JSON string)
        assert devices[0]["last_telemetry"] in (None, {})

    def test_delete_removes_device(self, client, fresh_app_state) -> None:
        _open_window(fresh_app_state)
        client.post(
            "/api/doorbell/register",
            json={"device_id": "front-door", "label": "x"},
        )
        resp = client.delete("/api/doorbell/devices/front-door")
        assert resp.status_code == 200
        # Now the registry is empty
        listing = client.get("/api/doorbell/devices").json()
        assert listing["devices"] == []

    def test_delete_unknown_404(self, client, fresh_app_state) -> None:
        resp = client.delete("/api/doorbell/devices/never-existed")
        assert resp.status_code == 404


# ── Pairing-window helpers (db.py) ───────────────────────────────────────


class TestPairingWindowHelpers:
    def test_open_then_is_open(self, fresh_app_state) -> None:
        web_db.open_pairing_window(seconds=60)
        assert web_db.is_pairing_window_open() is True

    def test_close_then_not_open(self, fresh_app_state) -> None:
        web_db.open_pairing_window(seconds=60)
        web_db.close_pairing_window()
        assert web_db.is_pairing_window_open() is False

    def test_expired_window_not_open(self, fresh_app_state) -> None:
        # Manually set an expiry in the past
        fresh_app_state["doorbell_pairing_until"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        assert web_db.is_pairing_window_open() is False

    def test_open_with_zero_seconds_raises(self, fresh_app_state) -> None:
        with pytest.raises(ValueError):
            web_db.open_pairing_window(seconds=0)


# ── Auth helpers (auth.py) ───────────────────────────────────────────────


class TestDeviceAuthHelpers:
    def test_create_then_validate(self) -> None:
        db = auth_module.get_db()
        try:
            raw = auth_module.create_device_token(db, "front-door", "Front door")
            res = auth_module.validate_device_token(db, raw)
        finally:
            db.close()
        assert res is not None
        assert res["device_id"] == "front-door"

    def test_validate_unknown_returns_none(self) -> None:
        db = auth_module.get_db()
        try:
            assert auth_module.validate_device_token(db, "nope") is None
            assert auth_module.validate_device_token(db, "") is None
        finally:
            db.close()

    def test_disable_then_validate_returns_none(self) -> None:
        db = auth_module.get_db()
        try:
            raw = auth_module.create_device_token(db, "front-door", "x")
            auth_module.disable_device(db, "front-door")
            assert auth_module.validate_device_token(db, raw) is None
        finally:
            db.close()

    def test_create_with_empty_id_raises(self) -> None:
        db = auth_module.get_db()
        try:
            with pytest.raises(ValueError):
                auth_module.create_device_token(db, "", "x")
            with pytest.raises(ValueError):
                auth_module.create_device_token(db, "front-door", "")
        finally:
            db.close()
