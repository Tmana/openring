"""Web-service read/write helpers for v0.4 recognizer.db.

The single-writer-per-DB-file invariant doesn't hold here — both the
recognizer service and the web service write to ``recognizer.db`` —
but the *single-writer-per-TABLE* invariant does:

  * Web writes ``known_faces`` and ``face_embeddings`` (PR-B
    enrollment flow).
  * Recognizer writes ``recognitions`` (PR-A inference flow).

Both connect with WAL mode so the parallel writes are safe.  The web
service still avoids touching ``recognitions`` to keep the boundary
clean and to make the design obvious to anyone reading the code.

For embedding compute on enrollment: the web container does NOT have
``face_recognition`` installed — adding dlib would bloat the web image
~250 MB.  Instead, web saves the photo + ``known_faces`` row, then
publishes ``openring:enrollment`` on Redis to ask the recognizer
service to compute the embedding.  The recognizer also runs a startup
sweep that catches any photos missing embeddings, so a missed Redis
message during a recognizer restart isn't terminal.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Same default path as services/recognizer/src/db.py — the web service
# mounts /data the same as the recognizer.  An override via env var is
# honoured for testability.
DB_PATH: str = os.environ.get("RECOGNIZER_DB_PATH", "/data/recognizer.db")

_lock = threading.Lock()
_local = threading.local()

# Where the operator-uploaded reference photos live.  One subdir per
# face_id.  Same convention as snapshots / clips.
REFERENCES_DIR: str = os.environ.get("FACE_REFERENCES_DIR", "/data/face-references")


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
    """Create the schema if missing.  Safe to call before the recognizer
    container has booted — the schema definition is identical, so whichever
    side runs first wins and the other no-ops."""
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


# ── Read API ─────────────────────────────────────────────────────────


def list_faces() -> list[dict[str, Any]]:
    """List every enrolled face, with photo + embedding counts.

    Returns rows sorted by created_at desc — most recent enrollments
    first, matching the UI's "what did I just add?" expectation.  The
    ``photos`` count is what the user uploaded; the ``embeddings``
    count is what the recognizer has finished processing.  Difference
    > 0 means embedding compute is still in flight (or failed).
    """
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            """
            SELECT
              f.id, f.label, f.notes, f.enabled,
              f.created_at, f.updated_at,
              (SELECT COUNT(*) FROM face_embeddings e WHERE e.face_id = f.id)
                AS embedding_count
            FROM known_faces f
            ORDER BY f.created_at DESC
            """,
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        face_dir = os.path.join(REFERENCES_DIR, str(r["id"]))
        try:
            photos = sorted(
                fn for fn in os.listdir(face_dir)
                if fn.lower().endswith((".jpg", ".jpeg", ".png"))
            ) if os.path.isdir(face_dir) else []
        except OSError:
            photos = []
        out.append({
            "id": r["id"],
            "label": r["label"],
            "notes": r["notes"],
            "enabled": bool(r["enabled"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "embedding_count": r["embedding_count"],
            "photos": photos,
        })
    return out


def get_face(face_id: int) -> dict[str, Any] | None:
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM known_faces WHERE id = ?", (face_id,),
        ).fetchone()
    return dict(row) if row else None


# ── Write API (web is the only writer to known_faces + face_embeddings) ─


def insert_known_face(label: str, notes: str = "") -> int:
    """Insert a new known_faces row and return its id.

    UNIQUE(LOWER(label)) is the conflict guard — duplicate labels raise
    ``sqlite3.IntegrityError`` which the route layer catches and surfaces
    as a 400 with a friendly message.
    """
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


def update_face(face_id: int, *, label: str | None = None,
                notes: str | None = None, enabled: bool | None = None) -> None:
    """Patch a face's label / notes / enabled.  No-op when all fields None."""
    fields: list[str] = []
    params: list[Any] = []
    if label is not None:
        fields.append("label = ?")
        params.append(label)
    if notes is not None:
        fields.append("notes = ?")
        params.append(notes)
    if enabled is not None:
        fields.append("enabled = ?")
        params.append(1 if enabled else 0)
    if not fields:
        return
    fields.append("updated_at = ?")
    params.append(datetime.now(timezone.utc).isoformat())
    params.append(face_id)
    with _lock:
        conn = _get_conn()
        conn.execute(
            f"UPDATE known_faces SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        conn.commit()


def delete_face(face_id: int) -> None:
    """Hard-delete a face.  ON DELETE CASCADE removes its embeddings.

    The route layer is responsible for removing the on-disk reference
    photos under ``REFERENCES_DIR/<face_id>/`` — we don't entangle
    filesystem cleanup with DB transactions because partial failure on
    the FS side shouldn't roll back the DB delete.
    """
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM known_faces WHERE id = ?", (face_id,))
        conn.commit()
