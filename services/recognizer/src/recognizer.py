"""Face detection + embedding compute, decoupled from the Redis loop.

Wraps the ``face_recognition`` library (dlib-backed) so the rest of the
service can be unit-tested without dragging dlib into pytest.  The two
public entry points are :func:`embed_image` for enrollment and
:func:`recognize_crop` for the per-event match path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class RecognitionResult:
    """Outcome of a single ``recognize_crop`` call.

    ``status`` is one of ``matched``, ``unknown``, ``no_face`` — the
    recognizer never reports ``error`` from this layer; that's the
    main loop's job when it catches an exception around the call.
    """

    status: str
    face_id: int | None = None
    label: str | None = None
    score: float | None = None
    bbox: tuple[int, int, int, int] | None = None  # (x1,y1,x2,y2)


def _embeddings_to_array(blobs: list[bytes]) -> np.ndarray:
    """Stack persisted 128-D float32 embeddings into a (N, 128) array."""
    if not blobs:
        return np.empty((0, 128), dtype=np.float32)
    arrays = [np.frombuffer(b, dtype=np.float32) for b in blobs]
    return np.vstack(arrays)


def _crop_with_padding(
    image: "np.ndarray",
    bbox: tuple[int, int, int, int],
    padding_pct: float,
) -> "np.ndarray":
    """Widen the YOLO bbox by padding_pct on each side, clamp to frame.

    Detector bboxes are tight on the body; face_recognition wants some
    surrounding context (hairline, ears, neck) for reliable detection.
    """
    h, w = image.shape[:2]
    x1, y1, x2, y2 = bbox
    bw = max(1, x2 - x1)
    bh = max(1, y2 - y1)
    px = int(bw * padding_pct)
    py = int(bh * padding_pct)
    nx1 = max(0, x1 - px)
    ny1 = max(0, y1 - py)
    nx2 = min(w, x2 + px)
    ny2 = min(h, y2 + py)
    return image[ny1:ny2, nx1:nx2]


def embed_image(image_path: str) -> tuple[bytes, str | None]:
    """Compute the embedding for a single reference photo.

    Returns ``(embedding_bytes, error)``.  On success ``error`` is None
    and the bytes are the 128-D float32 vector.  On failure (no face,
    multiple faces, IO error) ``embedding_bytes`` is ``b""`` and ``error``
    describes the problem.

    Used by the enrollment flow (PR-B); kept here so the matching path
    and the enrollment path share a single embedding model.
    """
    import face_recognition

    try:
        image = face_recognition.load_image_file(image_path)
    except Exception as e:
        return b"", f"could not read image: {e}"

    locations = face_recognition.face_locations(image, model="hog")
    if len(locations) == 0:
        return b"", "no face detected in reference photo"
    if len(locations) > 1:
        return b"", f"multiple faces detected ({len(locations)}); reference photos must contain exactly one face"

    encs = face_recognition.face_encodings(image, known_face_locations=locations)
    if not encs:
        return b"", "face was detected but embedding failed"
    return encs[0].astype(np.float32).tobytes(), None


def recognize_crop(
    snapshot_path: str,
    bbox: tuple[int, int, int, int],
    padding_pct: float,
    known_faces: list[dict],
    tolerance: float,
) -> RecognitionResult:
    """Match the largest face in the bbox crop against enrolled faces.

    ``known_faces`` is the list returned by ``db.list_enabled_faces``;
    each entry needs ``id``, ``label``, and ``embeddings`` (list of raw
    bytes).  Faces with zero embeddings are skipped.

    Strategy: face_recognition computes a Euclidean distance per
    enrolled embedding; we take the *minimum* distance per identity
    (best photo wins), then pick the identity with the lowest min.
    Library convention: tighter (lower) distance = better match.
    """
    import face_recognition

    try:
        image = face_recognition.load_image_file(snapshot_path)
    except Exception as e:
        logger.warning("Failed to load snapshot %s: %s", snapshot_path, e)
        return RecognitionResult(status="no_face")

    crop = _crop_with_padding(image, bbox, padding_pct)
    if crop.size == 0:
        return RecognitionResult(status="no_face")

    locations = face_recognition.face_locations(crop, model="hog")
    if not locations:
        return RecognitionResult(status="no_face")

    # Pick the largest detected face — porch use case is "person walks
    # toward door"; the dominant face is the visitor.  In v0.5 we may
    # want multi-face semantics; v0.4 is highest-confidence-wins.
    largest = max(locations, key=lambda r: (r[2] - r[0]) * (r[1] - r[3]))
    encs = face_recognition.face_encodings(crop, known_face_locations=[largest])
    if not encs:
        return RecognitionResult(status="no_face")
    query = encs[0]

    # face_locations returns (top, right, bottom, left); convert to the
    # (x1,y1,x2,y2) shape we persist, mapped back into snapshot coords.
    top, right, bottom, left = largest
    # crop's origin in original image — we need it for the persisted bbox
    h, w = image.shape[:2]
    x1, y1, x2, y2 = bbox
    bw = max(1, x2 - x1)
    bh = max(1, y2 - y1)
    px = int(bw * padding_pct)
    py = int(bh * padding_pct)
    cx_off = max(0, x1 - px)
    cy_off = max(0, y1 - py)
    abs_bbox = (cx_off + left, cy_off + top, cx_off + right, cy_off + bottom)
    # Defensive clamp in case of off-by-one on the crop edges.
    abs_bbox = (
        max(0, min(w, abs_bbox[0])),
        max(0, min(h, abs_bbox[1])),
        max(0, min(w, abs_bbox[2])),
        max(0, min(h, abs_bbox[3])),
    )

    best_face: dict | None = None
    best_score = float("inf")
    for face in known_faces:
        embs = face.get("embeddings") or []
        if not embs:
            continue
        arr = _embeddings_to_array(embs)
        if arr.size == 0:
            continue
        # face_recognition.face_distance returns euclidean per row.
        dists = face_recognition.face_distance(arr, query)
        min_d = float(np.min(dists))
        if min_d < best_score:
            best_score = min_d
            best_face = face

    if best_face is None:
        return RecognitionResult(status="unknown", score=None, bbox=abs_bbox)
    if best_score > tolerance:
        return RecognitionResult(status="unknown", score=best_score, bbox=abs_bbox)
    return RecognitionResult(
        status="matched",
        face_id=int(best_face["id"]),
        label=str(best_face["label"]),
        score=best_score,
        bbox=abs_bbox,
    )
