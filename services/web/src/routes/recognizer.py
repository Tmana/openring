"""Admin enrollment UI for v0.4 face recognition.

Routes under ``/admin/recognizer``:

  GET    /admin/recognizer                — list enrolled faces + add form
  POST   /admin/recognizer                — create a new face (label + notes
                                            + uploaded photos)
  POST   /admin/recognizer/{id}/photos    — append more photos to a face
  POST   /admin/recognizer/{id}/edit      — patch label/notes/enabled
  POST   /admin/recognizer/{id}/delete    — hard-delete face + photos
  GET    /admin/recognizer/{id}/photo/{n} — serve a single reference photo

Admin-only.  Every mutation also publishes an ``openring:enrollment``
HMAC-signed message asking the recognizer to (re-)compute embeddings;
the recognizer also runs a startup catch-up sweep so missed messages
self-heal.

The web container does NOT carry dlib — that's the point of the
sidecar split.  Embeddings are computed on the recognizer side; this
file only writes the photos to disk and the metadata row to
recognizer.db.

Path-traversal hygiene: face_id is parsed as int (FastAPI does this)
and the photo index ``n`` is parsed as int too.  Filenames on disk are
``<n>.<ext>`` — always integer-prefixed.  Operator-supplied filenames
are never used.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

import audit
import recognizer_db
import redis as redis_lib
from event_signing import load_key_from_env, sign_event
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from route_auth import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/recognizer")

_src = os.path.dirname(os.path.dirname(__file__))
templates = Jinja2Templates(directory=os.path.join(_src, "templates"))

# Hard upper-bound on a single uploaded reference photo.  10 MB lets a
# 12 MP cellphone JPEG through with headroom; bigger is almost always
# a misconfigured camera shot saved as PNG.
_MAX_PHOTO_BYTES = 10 * 1024 * 1024
# Chunk size for the streaming bound check.  64 KB is small enough that
# overrun is detected within a hundred ms of malicious upload start, big
# enough that loop overhead is negligible for the legitimate <10 MB case.
_UPLOAD_CHUNK_BYTES = 64 * 1024

# Accepted MIME types — we sniff the first few bytes for magic numbers
# rather than trusting Content-Type.  JPEG and PNG only; HEIC/WEBP/etc
# would need extra Pillow-side decoder work in the recognizer and the
# 99% case is phone-camera output.
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Label charset — same shape as a Linux username plus spaces, so the
# label is safe to render in a notification subject line and safe in
# YAML config without quoting.  No commas (rules are CSV-ish), no
# leading/trailing whitespace.
_LABEL_RE = re.compile(r"^[a-zA-Z0-9 _.\-]{1,64}$")

ENROLLMENT_CHANNEL = "openring:enrollment"


def _redirect_with(msg: str | None = None, error: str | None = None) -> RedirectResponse:
    qs = []
    if msg:
        qs.append(f"msg={quote(msg)}")
    if error:
        qs.append(f"error={quote(error)}")
    suffix = ("?" + "&".join(qs)) if qs else ""
    return RedirectResponse(f"/admin/recognizer{suffix}", status_code=302)


def _redirect_face(face_id: int, error: str) -> RedirectResponse:
    return RedirectResponse(
        f"/admin/recognizer?error={quote(error)}#face-{face_id}",
        status_code=302,
    )


def _next_photo_index(face_dir: Path) -> int:
    """Return the next integer filename to use under face_dir.

    We pick max(existing) + 1 rather than `len(...) + 1` so deletes
    don't collide.  Returns 1 for an empty directory.
    """
    if not face_dir.exists():
        return 1
    highest = 0
    for entry in face_dir.iterdir():
        stem = entry.stem
        if stem.isdigit():
            highest = max(highest, int(stem))
    return highest + 1


def _publish_enrollment(face_id: int, photo_path: str) -> None:
    """Tell the recognizer to embed a freshly-uploaded photo.

    HMAC-signed if the key is configured (charter requirement).  Best-
    effort — a Redis flap doesn't fail enrollment because the recognizer
    runs a startup sweep that picks up any stranded photos.
    """
    import os as _os

    import config_store

    cfg = config_store.load_cached()
    redis_cfg = cfg.get("redis", {}) or {}
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))
    password = _os.environ.get("REDIS_PASSWORD", "") or None
    hmac_key = load_key_from_env()
    payload: dict[str, Any] = {
        "face_id": face_id,
        "photo_path": photo_path,
        "action": "embed",
    }
    if hmac_key is not None:
        payload = sign_event(payload, hmac_key)
    try:
        client = redis_lib.Redis(host=host, port=port, password=password,
                                 decode_responses=True, socket_timeout=2)
        try:
            client.publish(ENROLLMENT_CHANNEL, __import__("json").dumps(payload))
        finally:
            client.close()
    except redis_lib.RedisError:
        logger.warning(
            "Could not publish enrollment for face_id=%s — recognizer will "
            "pick it up on its next startup sweep", face_id,
        )


async def _save_photo(face_id: int, upload: UploadFile) -> tuple[str | None, str | None]:
    """Validate + save a single uploaded photo.

    Returns ``(saved_path, error)``.  On success ``saved_path`` is the
    relative path under REFERENCES_DIR and ``error`` is None.  On
    rejection ``saved_path`` is None and ``error`` is a human-readable
    reason.

    Reads in chunks against a running counter so a multi-GB body can't
    fully buffer through Starlette's spooled-temp-file before the size
    cap fires.  We bail the moment the cap is exceeded and the partially-
    read upload is discarded — Starlette handles client-disconnect
    cleanly even when the body is much larger than what we accepted.
    """
    if not upload.filename:
        return None, "missing filename"

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_PHOTO_BYTES:
            return None, f"photo too large (max {_MAX_PHOTO_BYTES // (1024*1024)} MB)"
        chunks.append(chunk)

    if total == 0:
        return None, "empty upload"
    raw = b"".join(chunks)

    if raw[:3] == _JPEG_MAGIC:
        ext = ".jpg"
    elif raw[:8] == _PNG_MAGIC:
        ext = ".png"
    else:
        return None, "only JPEG and PNG are accepted"

    face_dir = Path(recognizer_db.REFERENCES_DIR) / str(face_id)
    face_dir.mkdir(parents=True, exist_ok=True)
    idx = _next_photo_index(face_dir)
    target = face_dir / f"{idx}{ext}"
    # Write to a temp file and rename so partial writes never appear
    # in the listing — the recognizer's startup sweep would otherwise
    # try to embed half a JPEG.
    tmp = face_dir / f".tmp.{secrets.token_hex(4)}{ext}"
    try:
        tmp.write_bytes(raw)
        tmp.replace(target)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None, f"could not save photo: {exc}"
    return str(target), None


# ── List + add ─────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
async def faces_list(request: Request) -> Response:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate
    recognizer_db.init_db()
    faces = recognizer_db.list_faces()
    return templates.TemplateResponse(
        request,
        "recognizer.html",
        {
            "faces": faces,
            "msg": request.query_params.get("msg"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("")
async def faces_create(
    request: Request,
    label: str = Form(...),
    notes: str = Form(""),
    photos: list[UploadFile] = File(default_factory=list),
) -> Response:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate

    label = (label or "").strip()
    notes = (notes or "").strip()
    if not _LABEL_RE.match(label):
        return _redirect_with(error="Label must be 1-64 chars (letters, numbers, space, _ . -).")

    recognizer_db.init_db()
    try:
        face_id = recognizer_db.insert_known_face(label=label, notes=notes)
    except sqlite3.IntegrityError:
        return _redirect_with(error=f"A face with label {label!r} already exists.")

    saved = 0
    rejection: str | None = None
    for upload in photos:
        path, err = await _save_photo(face_id, upload)
        if err is not None:
            rejection = err
            continue
        if path is not None:
            _publish_enrollment(face_id, path)
            saved += 1

    audit.record_request(
        request,
        action="recognizer.face_create",
        resource=f"face:{face_id}",
        details={"label": label, "photos_uploaded": saved},
    )

    if saved == 0 and rejection is not None:
        return _redirect_with(
            error=f"Created {label!r} but rejected every photo: {rejection}",
        )
    if rejection is not None:
        return _redirect_with(
            msg=f"Created {label!r} with {saved} photo(s); some were rejected: {rejection}",
        )
    return _redirect_with(msg=f"Created {label!r} with {saved} photo(s).")


# ── Per-face routes ────────────────────────────────────────────────────


@router.post("/{face_id}/photos")
async def faces_add_photos(
    request: Request,
    face_id: int,
    photos: list[UploadFile] = File(default_factory=list),
) -> Response:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate
    if recognizer_db.get_face(face_id) is None:
        return _redirect_with(error=f"No face with id {face_id}.")

    saved = 0
    rejection: str | None = None
    for upload in photos:
        path, err = await _save_photo(face_id, upload)
        if err is not None:
            rejection = err
            continue
        if path is not None:
            _publish_enrollment(face_id, path)
            saved += 1

    audit.record_request(
        request,
        action="recognizer.photos_add",
        resource=f"face:{face_id}",
        details={"photos_uploaded": saved},
    )

    if saved == 0 and rejection is not None:
        return _redirect_face(face_id, error=f"All photos rejected: {rejection}")
    if rejection is not None:
        return _redirect_with(
            msg=f"Added {saved} photo(s); some were rejected: {rejection}",
        )
    return _redirect_with(msg=f"Added {saved} photo(s).")


@router.post("/{face_id}/edit")
async def faces_edit(
    request: Request,
    face_id: int,
    label: str = Form(...),
    notes: str = Form(""),
    enabled: str = Form(""),
) -> Response:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate
    if recognizer_db.get_face(face_id) is None:
        return _redirect_with(error=f"No face with id {face_id}.")

    label = (label or "").strip()
    notes = (notes or "").strip()
    if not _LABEL_RE.match(label):
        return _redirect_face(face_id, "Label must be 1-64 chars (letters, numbers, space, _ . -).")

    is_enabled = enabled.strip().lower() in ("1", "on", "true", "yes")

    try:
        recognizer_db.update_face(
            face_id, label=label, notes=notes, enabled=is_enabled,
        )
    except sqlite3.IntegrityError:
        return _redirect_face(face_id, f"Label {label!r} is already taken.")

    audit.record_request(
        request,
        action="recognizer.face_edit",
        resource=f"face:{face_id}",
        details={"label": label, "enabled": is_enabled},
    )
    return _redirect_with(msg=f"Updated {label!r}.")


@router.post("/{face_id}/delete")
async def faces_delete(request: Request, face_id: int) -> Response:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate
    face = recognizer_db.get_face(face_id)
    if face is None:
        return _redirect_with(error=f"No face with id {face_id}.")

    recognizer_db.delete_face(face_id)
    # Best-effort filesystem cleanup.  An I/O failure here doesn't roll
    # back the DB delete — orphan photos are harmless and the next
    # `list_faces()` won't show them.
    face_dir = Path(recognizer_db.REFERENCES_DIR) / str(face_id)
    if face_dir.exists():
        try:
            shutil.rmtree(face_dir)
        except OSError as exc:
            logger.warning("Could not remove %s: %s", face_dir, exc)

    audit.record_request(
        request,
        action="recognizer.face_delete",
        resource=f"face:{face_id}",
        details={"label": face.get("label")},
    )
    return _redirect_with(msg=f"Deleted {face.get('label')!r}.")


# ── Photo serving ─────────────────────────────────────────────────────


@router.get("/{face_id}/photo/{filename}")
async def faces_photo(request: Request, face_id: int, filename: str) -> Response:
    """Serve a single reference photo.

    Admin-gated — the photos are PII and we don't want them on the
    public web tree.  Filename is matched against the trusted on-disk
    listing rather than concatenated, so no path-traversal surface.
    """
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate
    face_dir = Path(recognizer_db.REFERENCES_DIR) / str(face_id)
    if not face_dir.is_dir():
        return Response(status_code=404)
    # Validate by listing the directory and matching exactly.  This
    # rejects ".." and any other traversal attempts because they
    # simply won't appear in the listing.
    safe_filenames = {
        fn for fn in os.listdir(face_dir)
        if fn.lower().endswith((".jpg", ".jpeg", ".png"))
    }
    if filename not in safe_filenames:
        return Response(status_code=404)
    target = face_dir / filename
    if not target.is_file():
        return Response(status_code=404)
    media = "image/jpeg" if filename.lower().endswith((".jpg", ".jpeg")) else "image/png"
    return FileResponse(str(target), media_type=media)
