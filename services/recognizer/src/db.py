"""SQLite persistence for v0.4 face recognition.

The recognizer service is the *sole writer* to ``recognizer.db``.  The
web service reads it read-only when rendering the events page, and
PR-B's enrollment UI writes to ``known_faces``/``face_embeddings`` via
the recognizer's HTTP-shaped admin API (not yet built — PR-B).

Why a separate DB and not an extra table in ``openring.db``?  Because
the recognizer's writes are orthogonal to the events render — keeping
them out of ``openring.db`` preserves the single-writer-per-DB-file
invariant when sqlite WAL is contended under load.  The recognitions
table joins to ``detection_events`` only by ``feedback_token``, so a
cross-DB query at the application layer is fine.

Embedding storage: ``face_recognition`` returns a 128-D float32 numpy
array, which is exactly 512 bytes raw.  We persist as BLOB, no
serialisation overhead, and reconstruct with ``np.frombuffer``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DB_PATH: str = os.environ.get("RECOGNIZER_DB_PATH", "/data/recognizer.db")

_lock = threading.Lock()
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


def init_db() -> None:
    """Create the schema if missing.  Idempotent."""
    with _lock:
        conn = _get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS known_faces (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                label       TEXT NOT NULL,
                notes       TEXT NOT NULL DEFAULT '',
                enabled     INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_known_faces_label_unique
                ON known_faces(LOWER(label));

            CREATE TABLE IF NOT EXISTS face_embeddings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                face_id       INTEGER NOT NULL REFERENCES known_faces(id) ON DELETE CASCADE,
                embedding     BLOB NOT NULL,
                source_image  TEXT NOT NULL,
                created_at    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_face_embeddings_face
                ON face_embeddings(face_id);

            CREATE TABLE IF NOT EXISTS recognitions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                feedback_token  TEXT NOT NULL UNIQUE,
                camera_name     TEXT NOT NULL,
                status          TEXT NOT NULL,
                face_id         INTEGER,
                label           TEXT,
                score           REAL,
                bbox            TEXT,
                error           TEXT,
                created_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_recognitions_token
                ON recognitions(feedback_token);
            CREATE INDEX IF NOT EXISTS idx_recognitions_face
                ON recognitions(face_id);
        """)
        conn.commit()


def list_enabled_faces() -> list[dict]:
    """Return enabled known_faces rows joined to their embeddings.

    Each row gets an ``embeddings`` key — a list of raw bytes objects.
    Callers that need numpy arrays do the conversion themselves so we
    don't drag numpy into the DB module.
    """
    with _lock:
        conn = _get_conn()
        faces = conn.execute(
            "SELECT id, label, notes FROM known_faces WHERE enabled = 1 ORDER BY id",
        ).fetchall()
        out: list[dict] = []
        for f in faces:
            embs = conn.execute(
                "SELECT embedding FROM face_embeddings WHERE face_id = ?",
                (f["id"],),
            ).fetchall()
            out.append({
                "id": f["id"],
                "label": f["label"],
                "notes": f["notes"],
                "embeddings": [bytes(e["embedding"]) for e in embs],
            })
        return out


def insert_recognition(
    *,
    feedback_token: str,
    camera_name: str,
    status: str,
    face_id: int | None = None,
    label: str | None = None,
    score: float | None = None,
    bbox: str | None = None,
    error: str | None = None,
) -> None:
    """Record a recognition outcome.  Idempotent on feedback_token.

    Allowed status values: ``matched``, ``unknown``, ``no_face``, ``error``.
    A re-fired event for the same token (rare — only on retries) updates
    the existing row rather than producing a duplicate.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                """
                INSERT INTO recognitions
                    (feedback_token, camera_name, status, face_id, label,
                     score, bbox, error, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(feedback_token) DO UPDATE SET
                    status=excluded.status,
                    face_id=excluded.face_id,
                    label=excluded.label,
                    score=excluded.score,
                    bbox=excluded.bbox,
                    error=excluded.error,
                    created_at=excluded.created_at
                """,
                (
                    feedback_token, camera_name, status, face_id, label,
                    score, bbox, (error or None), now,
                ),
            )
            conn.commit()
        except sqlite3.Error:
            logger.exception("Failed to insert recognition row for %s", feedback_token)


def insert_known_face(label: str, notes: str = "") -> int:
    """Insert a new known_faces row and return its id."""
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            """
            INSERT INTO known_faces (label, notes, enabled, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?)
            """,
            (label, notes, now, now),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def insert_embedding(face_id: int, embedding: bytes, source_image: str) -> None:
    """Insert a single embedding row for an enrolled face."""
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO face_embeddings (face_id, embedding, source_image, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (face_id, embedding, source_image, now),
        )
        conn.commit()
