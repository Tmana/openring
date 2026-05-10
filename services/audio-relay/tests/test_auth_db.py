"""Device-token lookup tests against an isolated auth.db.

The audio-relay's only DB read.  Verify hash matching, missing-table
graceful degrade, and disabled-device rejection.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from auth_db import _hash_token, get_device_id_for_token


def _make_auth_db(path: Path, with_table: bool = True) -> None:
    conn = sqlite3.connect(str(path))
    if with_table:
        conn.executescript("""
            CREATE TABLE device_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                last_seen_at TEXT,
                last_telemetry TEXT,
                disabled INTEGER NOT NULL DEFAULT 0
            );
        """)
    conn.commit()
    conn.close()


def _insert(path: Path, device_id: str, raw_token: str, *, disabled: int = 0) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        """INSERT INTO device_tokens
            (device_id, label, token_hash, created_at, disabled)
           VALUES (?, ?, ?, '2026-01-01T00:00:00Z', ?)""",
        (device_id, device_id, _hash_token(raw_token), disabled),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "auth.db"
    _make_auth_db(p)
    return p


class TestLookup:
    def test_valid_token_returns_device_id(self, db_path: Path):
        _insert(db_path, "front-door", "raw-token-1")
        assert get_device_id_for_token(str(db_path), "raw-token-1") == "front-door"

    def test_unknown_token_returns_none(self, db_path: Path):
        _insert(db_path, "front-door", "raw-token-1")
        assert get_device_id_for_token(str(db_path), "nope") is None

    def test_disabled_token_returns_none(self, db_path: Path):
        _insert(db_path, "front-door", "raw-token-1", disabled=1)
        assert get_device_id_for_token(str(db_path), "raw-token-1") is None

    def test_empty_token_returns_none(self, db_path: Path):
        assert get_device_id_for_token(str(db_path), "") is None

    def test_missing_db_file_returns_none(self, tmp_path: Path):
        assert get_device_id_for_token(str(tmp_path / "no-such.db"), "x") is None

    def test_missing_table_returns_none(self, tmp_path: Path):
        path = tmp_path / "auth.db"
        _make_auth_db(path, with_table=False)
        assert get_device_id_for_token(str(path), "x") is None


class TestHashMatchesWebService:
    """The audio-relay reuses the web service's SHA-256 hash format
    (services/web/src/auth.py:_hash_token).  Confirm they produce
    the same output for the same input — a divergence here would
    silently break every Pi auth attempt."""

    def test_known_value(self):
        # Computed via hashlib.sha256("hello").hexdigest()
        expected = hashlib.sha256(b"hello").hexdigest()
        assert _hash_token("hello") == expected
