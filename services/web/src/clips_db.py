"""Read-only access to the clipper sidecar's ``clips`` table.

The clipper service is the sole writer; web reads to attach clip
metadata to events on the events page.  Lives in the same
``/data/openring.db`` file the detector + web already share, so we
just open another connection — no ATTACH dance.

If the clipper has never run on this deployment, the table won't
exist; ``get_clips_for_tokens`` swallows the resulting OperationalError
and returns an empty dict.  That makes the events page degrade
gracefully (no clip badges, no overlay video) rather than 500ing.
"""

from __future__ import annotations

import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/openring.db")


def _connect() -> sqlite3.Connection | None:
    """Open a read-only connection.  None on failure."""
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        logger.warning("clips_db: failed to open %s", DB_PATH, exc_info=True)
        return None


def get_clips_for_tokens(tokens: list[str]) -> dict[str, dict]:
    """Return ``{feedback_token: clip_row_dict}`` for the given tokens.

    Tokens with no clip (or when the clips table is missing) are
    simply absent from the result.  Caller falls back to "no clip
    yet".  Empty input → empty dict, no DB access.
    """
    if not tokens:
        return {}
    conn = _connect()
    if conn is None:
        return {}
    placeholders = ",".join("?" * len(tokens))
    try:
        rows = conn.execute(
            f"""
            SELECT feedback_token, camera_name, clip_path,
                   pre_roll_seconds, post_roll_seconds, duration_seconds,
                   size_bytes, segments_used, error, created_at
            FROM clips
            WHERE feedback_token IN ({placeholders})
            """,
            tokens,
        ).fetchall()
    except sqlite3.OperationalError:
        # Table doesn't exist (clipper never ran) — return empty.
        conn.close()
        return {}
    except sqlite3.Error:
        logger.warning("clips_db: query failed", exc_info=True)
        conn.close()
        return {}
    conn.close()
    return {r["feedback_token"]: dict(r) for r in rows}
