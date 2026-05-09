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

    def _fake_redis(self) -> MagicMock:
        """Mock aioredis.Redis with publish + aclose stubs.

        ``_grab_snapshot`` is patched separately so we don't need to
        emulate the pubsub round-trip here.
        """
        fake = MagicMock()
        fake.publish = AsyncMock(return_value=1)
        fake.aclose = AsyncMock()
        return fake

    @pytest.fixture(autouse=True)
    def _stub_event_insert(self, monkeypatch):
        """Stub the SQLite write — the detection_events table is owned by
        the detector and isn't initialised in the web test environment.
        Each call returns an incrementing event id and a fresh token."""
        counter = {"n": 0}

        def fake_insert(camera_name, snapshot_path, actions_triggered,
                       feedback_token=None, timestamp=None):
            counter["n"] += 1
            import uuid as _u
            return counter["n"], feedback_token or _u.uuid4().hex

        monkeypatch.setattr("routes.doorbell.db.insert_doorbell_event", fake_insert)

    def test_no_bearer_rejected(self, client, fresh_app_state) -> None:
        resp = client.post("/api/doorbell/press", json={})
        assert resp.status_code == 401

    def test_publishes_with_snapshot(self, client, fresh_app_state) -> None:
        token = self._register(client, fresh_app_state)
        fake_redis = self._fake_redis()
        with patch("routes.doorbell.aioredis.Redis", return_value=fake_redis), \
             patch(
                 "routes.doorbell._grab_snapshot",
                 new=AsyncMock(return_value="/data/snapshots/front-door_press.jpg"),
             ):
            resp = client.post(
                "/api/doorbell/press",
                json={"timestamp": "2026-05-09T18:42:11+00:00", "device_id": "front-door"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 202
        assert resp.json()["snapshot_path"] == "/data/snapshots/front-door_press.jpg"
        assert isinstance(resp.json()["event_id"], int)
        # Exactly one publish to openring:doorbell
        assert fake_redis.publish.await_count == 1
        channel, payload_json = fake_redis.publish.await_args.args
        assert channel == "openring:doorbell"
        payload = json.loads(payload_json)
        assert payload["type"] == "doorbell_press"
        assert payload["device_id"] == "front-door"
        assert payload["camera_name"] == "front-door"
        assert payload["class_name"] == "doorbell_press"
        assert payload["confidence"] == 1.0
        assert payload["snapshot_path"] == "/data/snapshots/front-door_press.jpg"
        assert payload["device_timestamp"] == "2026-05-09T18:42:11+00:00"
        assert "timestamp" in payload
        assert "feedback_token" in payload
        assert payload["actions_triggered"] == []  # no rules in MOCK_CONFIG

    def test_snapshot_failure_still_records_press(
        self, client, fresh_app_state,
    ) -> None:
        token = self._register(client, fresh_app_state)
        fake_redis = self._fake_redis()
        with patch("routes.doorbell.aioredis.Redis", return_value=fake_redis), \
             patch(
                 "routes.doorbell._grab_snapshot",
                 new=AsyncMock(return_value=None),
             ):
            resp = client.post(
                "/api/doorbell/press",
                json={"device_id": "front-door"},
                headers={"Authorization": f"Bearer {token}"},
            )
        # Press still completes — a doorbell ring without a still image is
        # better signal than no event at all.
        assert resp.status_code == 202
        assert resp.json()["snapshot_path"] is None
        payload = json.loads(fake_redis.publish.await_args.args[1])
        assert payload["snapshot_path"] is None

    def test_press_does_not_overwrite_telemetry(
        self, client, fresh_app_state,
    ) -> None:
        token = self._register(client, fresh_app_state)
        client.post(
            "/api/doorbell/heartbeat",
            json={"version": "0.0.1", "uptime_seconds": 100},
            headers={"Authorization": f"Bearer {token}"},
        )
        fake_redis = self._fake_redis()
        with patch("routes.doorbell.aioredis.Redis", return_value=fake_redis), \
             patch(
                 "routes.doorbell._grab_snapshot",
                 new=AsyncMock(return_value=None),
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


# ── _resolve_actions (notification routing for doorbell_press) ───────────


class TestResolveActions:
    """Cover the per-camera notification_rules lookup the press handler
    runs against the cached YAML config.  The detector has the same
    semantics for ``openring:detections`` events."""

    def _resolve(self, cfg: dict, device_id: str) -> list[str] | None:
        from routes.doorbell import _resolve_actions
        return _resolve_actions(cfg, device_id)

    def test_unknown_camera_notifies_all(self) -> None:
        # No matching camera → empty list (notifier interprets as "all")
        assert self._resolve({"cameras": []}, "front-door") == []

    def test_camera_with_no_rules_notifies_all(self) -> None:
        cfg = {"cameras": [{"name": "front-door"}]}
        assert self._resolve(cfg, "front-door") == []

    def test_explicit_rule_returns_channels(self) -> None:
        cfg = {"cameras": [{
            "name": "front-door",
            "notification_rules": [
                {"class_name": "doorbell_press", "channels": ["phone-ntfy"]},
            ],
        }]}
        assert self._resolve(cfg, "front-door") == ["phone-ntfy"]

    def test_wildcard_rule_matches(self) -> None:
        cfg = {"cameras": [{
            "name": "front-door",
            "notification_rules": [
                {"class_name": "*", "channels": ["everyone"]},
            ],
        }]}
        assert self._resolve(cfg, "front-door") == ["everyone"]

    def test_no_match_returns_none_to_suppress(self) -> None:
        cfg = {"cameras": [{
            "name": "front-door",
            "notification_rules": [
                {"class_name": "person", "channels": ["x"]},
            ],
        }]}
        assert self._resolve(cfg, "front-door") is None


# ── db.insert_doorbell_event ─────────────────────────────────────────────


class TestInsertDoorbellEvent:
    """Direct test of the SQL insert path against an isolated events DB."""

    @pytest.fixture
    def fresh_events_db(self, tmp_path, monkeypatch):
        path = str(tmp_path / "openring-test.db")
        monkeypatch.setenv("DB_PATH", path)
        # Reload db module so DB_PATH picks up the new env var.
        import importlib

        import db as web_db
        importlib.reload(web_db)
        # Mirror the detector's CREATE TABLE for detection_events.
        import sqlite3
        conn = sqlite3.connect(path)
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
        conn.commit()
        conn.close()
        yield web_db
        # Subsequent tests get the conftest's standard DB_PATH back via
        # monkeypatch teardown.

    def test_inserts_with_all_fields(self, fresh_events_db) -> None:
        event_id, token = fresh_events_db.insert_doorbell_event(
            camera_name="front-door",
            snapshot_path="/data/snapshots/x.jpg",
            actions_triggered=["phone-ntfy"],
        )
        assert event_id > 0
        assert token  # auto-generated uuid
        # Read back
        import sqlite3
        conn = sqlite3.connect(os.environ["DB_PATH"])
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM detection_events WHERE id=?", (event_id,),
        ).fetchone()
        conn.close()
        assert row["class_name"] == "doorbell_press"
        assert row["confidence"] == 1.0
        assert row["camera_name"] == "front-door"
        assert row["snapshot_path"] == "/data/snapshots/x.jpg"
        assert json.loads(row["actions_triggered"]) == ["phone-ntfy"]
        assert row["feedback_token"] == token

    def test_none_actions_stored_as_null(self, fresh_events_db) -> None:
        # actions_triggered=None means "rules exist but no rule matched"
        # — represented as SQL NULL so the notifier suppresses it.
        event_id, _ = fresh_events_db.insert_doorbell_event(
            camera_name="front-door",
            snapshot_path=None,
            actions_triggered=None,
        )
        import sqlite3
        conn = sqlite3.connect(os.environ["DB_PATH"])
        row = conn.execute(
            "SELECT actions_triggered FROM detection_events WHERE id=?",
            (event_id,),
        ).fetchone()
        conn.close()
        assert row[0] is None

    def test_empty_actions_stored_as_empty_json(self, fresh_events_db) -> None:
        # actions_triggered=[] means "no rules configured" → notify all
        event_id, _ = fresh_events_db.insert_doorbell_event(
            camera_name="front-door",
            snapshot_path=None,
            actions_triggered=[],
        )
        import sqlite3
        conn = sqlite3.connect(os.environ["DB_PATH"])
        row = conn.execute(
            "SELECT actions_triggered FROM detection_events WHERE id=?",
            (event_id,),
        ).fetchone()
        conn.close()
        assert row[0] == "[]"
