"""Enrollment-side embedding compute for v0.4 face recognition.

The web service writes ``known_faces`` rows + saves photos to
``/data/face-references/<face_id>/<n>.{jpg,png}`` and publishes
``openring:enrollment`` asking the recognizer to embed.  This module
turns that ask into a ``face_embeddings`` row.

There are two callers:

  * :func:`embed_one`  — fired by the Redis subscriber on a single
    inbound message.  Handles the request-response shape directly.
  * :func:`sweep`      — fired once at recognizer startup to find any
    photos that exist on disk but don't have a corresponding
    ``face_embeddings`` row.  Self-heals after a missed Redis message
    or a recognizer crash mid-enrollment.

Both call into ``recognizer.embed_image``, which wraps face_recognition's
``face_locations`` + ``face_encodings``.  The wrapper rejects photos
with zero or multiple faces (consent-and-quality requirement from
docs/FACE_RECOGNITION.md §2.2).
"""

from __future__ import annotations

import logging
import os
import pathlib
import sqlite3
from typing import TYPE_CHECKING

import db as recognizer_db

if TYPE_CHECKING:
    from settings import RecognizerSettings

logger = logging.getLogger(__name__)


def _photo_already_embedded(face_id: int, photo_path: str) -> bool:
    """True if ``face_embeddings`` already has a row for this source image."""
    rel = os.path.basename(photo_path)
    conn = sqlite3.connect(recognizer_db.DB_PATH, timeout=10)
    try:
        row = conn.execute(
            "SELECT 1 FROM face_embeddings WHERE face_id = ? AND source_image = ? LIMIT 1",
            (face_id, rel),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def embed_one(face_id: int, photo_path: str) -> None:
    """Compute and persist the embedding for a single reference photo.

    Idempotent on (face_id, source_image): re-running for the same photo
    is a no-op.  Errors (no face / multiple faces / IO) are logged and
    swallowed — the operator sees the result via
    ``list_faces()``'s ``embedding_count`` column in the UI.
    """
    from recognizer import embed_image

    if not os.path.isfile(photo_path):
        logger.warning("Skipping enrollment — photo %s missing", photo_path)
        return
    if _photo_already_embedded(face_id, photo_path):
        logger.debug("Skipping enrollment — already embedded: %s", photo_path)
        return

    blob, error = embed_image(photo_path)
    if error is not None:
        logger.warning(
            "Could not embed %s for face_id=%s: %s", photo_path, face_id, error,
        )
        return

    rel = os.path.basename(photo_path)
    try:
        recognizer_db.insert_embedding(face_id, blob, rel)
        logger.info("Embedded %s for face_id=%s", rel, face_id)
    except sqlite3.IntegrityError:
        # FK violation — face was deleted between upload and embed.  Drop quietly.
        logger.info(
            "face_id=%s no longer exists — discarding embedding for %s",
            face_id, rel,
        )


def sweep(settings: "RecognizerSettings") -> int:
    """Walk references_dir + embed any photo without a row yet.

    Returns the number of embeddings newly written.  Called at process
    startup so a Redis message that was published while the recognizer
    was down doesn't strand the photo unembedded forever.
    """
    root = pathlib.Path(settings.references_dir)
    if not root.is_dir():
        return 0

    written = 0
    for face_dir in sorted(root.iterdir()):
        if not face_dir.is_dir():
            continue
        try:
            face_id = int(face_dir.name)
        except ValueError:
            continue
        for photo in sorted(face_dir.iterdir()):
            if photo.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            if _photo_already_embedded(face_id, str(photo)):
                continue
            from recognizer import embed_image
            blob, error = embed_image(str(photo))
            if error is not None:
                logger.warning(
                    "Sweep: could not embed %s for face_id=%s: %s",
                    photo, face_id, error,
                )
                continue
            rel = photo.name
            try:
                recognizer_db.insert_embedding(face_id, blob, rel)
                written += 1
            except sqlite3.IntegrityError:
                pass
    if written:
        logger.info("Startup sweep wrote %d new embedding(s)", written)
    return written
