"""Tests for the v0.2 heartbeat watchdog transition logic.

We don't run the watchdog thread itself — that's a `while True` loop
on top of `_evaluate_once`.  Each test calls `_evaluate_once` with a
fake redis client and asserts the right publishes (or absence of
publishes) for a given device-state matrix.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import auth as auth_module
import heartbeat_watchdog as wd
import pytest


@pytest.fixture(autouse=True)
def fresh_auth_db():
    """Init + wipe the conftest's standard AUTH_DB_PATH between tests."""
    path = os.environ["AUTH_DB_PATH"]
    auth_module.init_db(path)
    db = auth_module.get_db()
    try:
        for tbl in ("device_tokens", "sessions", "api_tokens", "audit_events", "users", "login_attempts"):
            try:
                db.execute(f"DELETE FROM {tbl}")
            except Exception:
                pass
        db.commit()
    finally:
        db.close()
    yield path


def _seed_device(device_id: str, last_seen: datetime | None) -> None:
    """Insert a device_tokens row with a controllable last_seen_at."""
    db = auth_module.get_db()
    try:
        auth_module.create_device_token(db, device_id, device_id)
        if last_seen is not None:
            db.execute(
                "UPDATE device_tokens SET last_seen_at=? WHERE device_id=?",
                (last_seen.isoformat(), device_id),
            )
            db.commit()
    finally:
        db.close()


def _fake_client() -> MagicMock:
    fake = MagicMock()
    fake.publish = MagicMock()
    return fake


def _evaluate(state: dict, cfg: dict | None = None) -> MagicMock:
    """Run one watchdog pass; return the mock redis client for assertions."""
    client = _fake_client()
    wd._evaluate_once(cfg or {}, state, client, hmac_key=None)
    return client


# ── Per-device timeout resolution ────────────────────────────────────────


class TestPerDeviceTimeout:
    def test_default_when_unconfigured(self):
        assert wd._per_device_timeout({}, "front-door") == 90.0

    def test_system_override(self):
        cfg = {"system": {"heartbeat_timeout_seconds": 120}}
        assert wd._per_device_timeout(cfg, "front-door") == 120.0

    def test_per_device_override_wins(self):
        cfg = {
            "system": {"heartbeat_timeout_seconds": 120},
            "doorbells": [{"device_id": "front-door", "heartbeat_timeout_seconds": 30}],
        }
        assert wd._per_device_timeout(cfg, "front-door") == 30.0
        # Other devices fall back to system default
        assert wd._per_device_timeout(cfg, "side-gate") == 120.0

    def test_garbage_per_device_override_falls_back(self):
        cfg = {
            "doorbells": [{"device_id": "front-door", "heartbeat_timeout_seconds": "not-a-number"}],
        }
        assert wd._per_device_timeout(cfg, "front-door") == 90.0


# ── Transition matrix ────────────────────────────────────────────────────


class TestTransitions:
    def test_bootstrap_does_not_publish(self):
        """First time we see a device, we record state silently — no
        notification fanout on web restart."""
        _seed_device("front-door", datetime.now(timezone.utc))
        state: dict = {}
        client = _evaluate(state)
        client.publish.assert_not_called()
        assert state == {"front-door": "online"}

    def test_offline_to_offline_silent(self):
        _seed_device("front-door", datetime.now(timezone.utc) - timedelta(seconds=600))
        state: dict = {"front-door": "offline"}
        client = _evaluate(state)
        client.publish.assert_not_called()

    def test_online_to_offline_publishes(self):
        _seed_device("front-door", datetime.now(timezone.utc) - timedelta(seconds=600))
        state: dict = {"front-door": "online"}
        client = _evaluate(state)
        assert client.publish.call_count == 1
        channel, payload_json = client.publish.call_args.args
        assert channel == "openring:device"
        payload = json.loads(payload_json)
        assert payload["type"] == "device_offline"
        assert payload["device_id"] == "front-door"
        assert payload["offline_seconds"] is not None
        assert state["front-door"] == "offline"

    def test_offline_to_online_publishes_recovery(self):
        _seed_device("front-door", datetime.now(timezone.utc))
        state: dict = {"front-door": "offline"}
        client = _evaluate(state)
        assert client.publish.call_count == 1
        channel, payload_json = client.publish.call_args.args
        assert channel == "openring:device"
        payload = json.loads(payload_json)
        assert payload["type"] == "device_recovered"
        assert payload["device_id"] == "front-door"
        assert state["front-door"] == "online"

    def test_no_last_seen_treated_as_offline(self):
        # Brand new device that has been registered but never sent a
        # heartbeat is offline.  But we don't fire on the first pass
        # (bootstrap rule).
        _seed_device("front-door", None)
        state: dict = {}
        client = _evaluate(state)
        client.publish.assert_not_called()
        assert state == {"front-door": "offline"}

        # On the next pass with state already recorded, still offline,
        # no transition → silent.
        client = _evaluate(state)
        client.publish.assert_not_called()

    def test_per_device_timeout_respected(self):
        """Tight per-device timeout flips the device offline even when
        the system default would still consider it online."""
        cfg = {
            "system": {"heartbeat_timeout_seconds": 600},
            "doorbells": [{"device_id": "twitchy", "heartbeat_timeout_seconds": 5}],
        }
        _seed_device("twitchy", datetime.now(timezone.utc) - timedelta(seconds=30))
        state: dict = {"twitchy": "online"}
        client = _evaluate(state, cfg)
        # With 5s timeout, 30s ago is offline
        assert client.publish.call_count == 1
        payload = json.loads(client.publish.call_args.args[1])
        assert payload["type"] == "device_offline"

    def test_unparseable_last_seen_treated_as_offline(self):
        # Manually plant garbage in the column.  Don't blow up.
        db = auth_module.get_db()
        try:
            auth_module.create_device_token(db, "front-door", "x")
            db.execute(
                "UPDATE device_tokens SET last_seen_at='not-a-timestamp' WHERE device_id='front-door'",
            )
            db.commit()
        finally:
            db.close()
        state: dict = {"front-door": "online"}
        client = _evaluate(state)
        # Treated as offline → transition fires
        assert client.publish.call_count == 1
        payload = json.loads(client.publish.call_args.args[1])
        assert payload["type"] == "device_offline"


# ── HMAC signing path ────────────────────────────────────────────────────


class TestHmacSigning:
    def test_payload_signed_when_key_present(self):
        from event_signing import verify_event
        _seed_device("front-door", datetime.now(timezone.utc) - timedelta(seconds=600))
        state: dict = {"front-door": "online"}
        client = _fake_client()
        # 32-byte key
        key = b"\x01" * 32
        wd._evaluate_once({}, state, client, hmac_key=key)
        payload = json.loads(client.publish.call_args.args[1])
        assert "_sig" in payload
        assert verify_event(payload, key) is True

    def test_payload_unsigned_when_key_absent(self):
        _seed_device("front-door", datetime.now(timezone.utc) - timedelta(seconds=600))
        state: dict = {"front-door": "online"}
        client = _fake_client()
        wd._evaluate_once({}, state, client, hmac_key=None)
        payload = json.loads(client.publish.call_args.args[1])
        assert "_sig" not in payload


# ── Redis failure resilience ─────────────────────────────────────────────


class TestRedisFailureSwallowed:
    def test_publish_failure_does_not_raise(self):
        import redis as redis_lib
        _seed_device("front-door", datetime.now(timezone.utc) - timedelta(seconds=600))
        state: dict = {"front-door": "online"}
        client = MagicMock()
        client.publish.side_effect = redis_lib.RedisError("kaboom")
        # Should not propagate
        wd._evaluate_once({}, state, client, hmac_key=None)
        # Transition was still recorded
        assert state["front-door"] == "offline"
