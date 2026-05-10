"""Device-token lookup for the audio-relay's Pi-side auth path.

The web service is the authoritative writer of the ``device_tokens``
table; the audio-relay only reads.  We open ``auth.db`` read-only
each lookup — there's at most one Pi reconnect every few seconds,
no need for a persistent connection.

The lookup hashes the raw token with the same SHA-256 the web service
uses (``services/web/src/auth.py:_hash_token``) and SELECTs by
``token_hash`` so the audio-relay never sees a plaintext token leave
its caller.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def get_device_id_for_token(db_path: str, raw_token: str) -> str | None:
    """Return the device_id matching *raw_token*, or None if invalid."""
    if not raw_token:
        return None
    if not os.path.exists(db_path):
        return None
    token_hash = _hash_token(raw_token)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        logger.warning("audio-relay: could not open %s", db_path, exc_info=True)
        return None
    try:
        try:
            row = conn.execute(
                "SELECT device_id FROM device_tokens"
                " WHERE token_hash=? AND disabled=0",
                (token_hash,),
            ).fetchone()
        except sqlite3.OperationalError:
            # device_tokens table doesn't exist (web hasn't migrated yet)
            return None
        return row[0] if row else None
    finally:
        conn.close()
