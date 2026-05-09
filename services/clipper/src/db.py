"""SQLite persistence for v0.2 video clips.

The clipper service is the sole writer to the ``clips`` table.  Web
service joins on ``feedback_token`` when rendering the events page so
each row's snapshot overlay can offer a clip player.

Lives in the existing ``/data/openring.db`` (not a sidecar DB) because
the clip-to-event relationship is tight enough that a JOIN on every
events query is cleaner than maintaining a separate DB file.  Single-
writer per table is preserved: detector owns ``detection_events``,
clipper owns ``clips``, web owns ``feedback`` UPDATE-only.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DB_PATH: str = os.environ.get("DB_PATH", "/data/openring.db")

_lock = threading.Lock()
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


def init_db() -> None:
    """Create the ``clips`` table + indexes if missing."""
    with _lock:
        conn = _get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS clips (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                feedback_token  TEXT    NOT NULL UNIQUE,
                camera_name     TEXT    NOT NULL,
                clip_path       TEXT    NOT NULL,
                pre_roll_seconds  REAL,
                post_roll_seconds REAL,
                duration_seconds  REAL,
                size_bytes      INTEGER,
                segments_used   INTEGER,
                created_at      TEXT NOT NULL,
                error           TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_clips_token   ON clips(feedback_token);
            CREATE INDEX IF NOT EXISTS idx_clips_created ON clips(created_at);
        """)
        conn.commit()


def insert_clip(
    feedback_token: str,
    camera_name: str,
    clip_path: str,
    pre_roll_seconds: float,
    post_roll_seconds: float,
    duration_seconds: float | None,
    size_bytes: int | None,
    segments_used: int,
) -> None:
    """Record a successful clip write.

    Idempotent on ``feedback_token``: a re-fired event for the same
    token (rare — would only happen on retry) updates in place rather
    than producing a duplicate row.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                """
                INSERT INTO clips
                    (feedback_token, camera_name, clip_path,
                     pre_roll_seconds, post_roll_seconds,
                     duration_seconds, size_bytes, segments_used,
                     created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(feedback_token) DO UPDATE SET
                    clip_path=excluded.clip_path,
                    pre_roll_seconds=excluded.pre_roll_seconds,
                    post_roll_seconds=excluded.post_roll_seconds,
                    duration_seconds=excluded.duration_seconds,
                    size_bytes=excluded.size_bytes,
                    segments_used=excluded.segments_used,
                    error=NULL
                """,
                (
                    feedback_token, camera_name, clip_path,
                    pre_roll_seconds, post_roll_seconds,
                    duration_seconds, size_bytes, segments_used,
                    now,
                ),
            )
            conn.commit()
        except sqlite3.Error:
            logger.exception("Failed to insert clip row for %s", feedback_token)


def insert_failure(
    feedback_token: str,
    camera_name: str,
    error: str,
) -> None:
    """Record that a clip couldn't be written (e.g. zero segments in window)."""
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                """
                INSERT INTO clips
                    (feedback_token, camera_name, clip_path, segments_used, created_at, error)
                VALUES (?, ?, '', 0, ?, ?)
                ON CONFLICT(feedback_token) DO UPDATE SET
                    error=excluded.error,
                    segments_used=excluded.segments_used
                """,
                (feedback_token, camera_name, now, error[:500]),
            )
            conn.commit()
        except sqlite3.Error:
            logger.exception("Failed to insert clip failure for %s", feedback_token)


def get_clip_for_token(token: str) -> dict | None:
    """Read-only lookup used by the web service's events JOIN.  Returns
    a dict with the clips row or None when there's no clip yet."""
    if not token:
        return None
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM clips WHERE feedback_token = ?", (token,),
        ).fetchone()
    return dict(row) if row else None
