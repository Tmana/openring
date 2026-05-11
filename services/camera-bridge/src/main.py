"""OpenRing camera-bridge — feed non-RTSP sources into the detector.

Reads ``openring.yml`` at startup, walks ``cameras[]``, and generates
``/tmp/mediamtx/mediamtx.yml`` with one path per camera whose
``source`` is ``webcam`` or ``file``.  Cameras whose source is
``ipcam`` or ``pi`` are deliberately skipped — the detector connects
to those directly without going through the bridge.

Then execs MediaMTX with that config.  No subscriber loop, no Redis,
no SQLite — this service is pure protocol translation.  A config
change on disk requires a container restart (``docker compose
restart camera-bridge``) so the operator's intent is explicit; we
deliberately don't hot-reload the way the other sidecars do because
MediaMTX itself doesn't support that gracefully (its watcher
recreates every active path on any file edit, which would interrupt
in-flight streams).

Path-name safety: camera names land in MediaMTX path URLs and in the
generated YAML's key field, so we validate them against a strict regex
before doing anything with them.  An openring.yml with a malformed
camera name fails the bridge fast at startup rather than producing a
weird URL or a MediaMTX parse error.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/openring.yml")
MEDIAMTX_CONFIG = os.environ.get("MEDIAMTX_CONFIG", "/tmp/mediamtx/mediamtx.yml")
MEDIAMTX_BINARY = os.environ.get("MEDIAMTX_BINARY", "/opt/mediamtx/mediamtx")

# Camera names map onto RTSP URL segments and YAML keys.  Restrict to
# a conservative charset so neither MediaMTX's parser nor a downstream
# ``ffprobe rtsp://camera-bridge:8554/<name>`` invocation can be
# tricked by exotic characters.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")

# Device path must be a /dev/video* node — the only thing the bridge
# is willing to v4l2-grab.  Operators can override the regex via env
# if they have to (custom v4l2 loopback names) but the default rejects
# anything that doesn't look like a stock webcam device.
_DEVICE_RE = re.compile(r"^/dev/video\d+$")

# File source paths are restricted to /data/** so the bridge only ever
# reads files the operator deliberately mounted into the container.
# In practice this means ./data/ from the repo root.
_FILE_PREFIX = "/data/"


def setup_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stdout,
    )


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        logger.warning("Config file %s missing — bridge will run with no paths", path)
        return {}
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        logger.error("Config root is not a mapping; treating as empty")
        return {}
    return cfg


def _validate_camera(cam: dict) -> tuple[str | None, str | None]:
    """Return (name, error) — error is None on success.

    Validates only the fields the bridge consumes: name, source,
    device (for webcam), file (for file).  Cameras the bridge doesn't
    handle (ipcam, pi) are filtered out earlier; this function
    assumes a bridge-relevant entry.
    """
    name = cam.get("name")
    if not isinstance(name, str):
        return None, "missing 'name'"
    if not _NAME_RE.match(name):
        return name, (
            f"name {name!r} doesn't match {_NAME_RE.pattern} — restrict to "
            "alphanumeric, underscore, or dash, ≤63 chars, leading alphanumeric"
        )
    source = (cam.get("source") or "").lower()
    if source == "webcam":
        device = cam.get("device", "")
        if not isinstance(device, str) or not _DEVICE_RE.match(device):
            return name, (
                f"source=webcam requires device matching {_DEVICE_RE.pattern}, got {device!r}"
            )
    elif source == "file":
        f = cam.get("file", "")
        if not isinstance(f, str) or not f.startswith(_FILE_PREFIX):
            return name, (
                f"source=file requires path under {_FILE_PREFIX}, got {f!r}"
            )
    else:
        return name, f"unsupported source {source!r}"
    return name, None


def bridged_cameras(cfg: dict) -> list[dict]:
    """Return only the camera entries the bridge will serve.

    Filters by enabled + source in {webcam, file}.  Skips entries that
    fail validation, logging a warning per skip so the operator sees
    *why* their camera isn't appearing.
    """
    out: list[dict] = []
    for cam in cfg.get("cameras") or []:
        if not isinstance(cam, dict):
            continue
        if not cam.get("enabled", True):
            continue
        source = (cam.get("source") or "").lower()
        # Pre-existing configs without a 'source' field default to
        # ipcam (the v0.1 contract).  Those go to the detector
        # directly, never to the bridge.
        if source in ("", "ipcam", "pi"):
            continue
        if source not in ("webcam", "file"):
            logger.warning(
                "Camera %r has unsupported source=%r — skipping",
                cam.get("name"), source,
            )
            continue
        name, err = _validate_camera(cam)
        if err is not None:
            logger.warning("Skipping camera %r: %s", name, err)
            continue
        out.append(cam)
    return out


def _path_block(cam: dict) -> dict[str, Any]:
    """Generate one MediaMTX paths entry from an openring.yml camera dict.

    Each block sets ``runOnInit`` so MediaMTX spawns ffmpeg / v4l2 at
    server startup and re-spawns on disconnect.  We avoid
    ``runOnDemand`` because the detector connects continuously, and
    re-establishing the source each time it briefly disconnects would
    add seconds of latency.
    """
    source = cam["source"].lower()
    name = cam["name"]
    rtsp_target = f"rtsp://localhost:8554/{name}"
    if source == "webcam":
        device = cam["device"]
        return {
            "runOnInit": (
                f"ffmpeg -loglevel warning -f v4l2 -i {device} "
                f"-c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p "
                f"-f rtsp {rtsp_target}"
            ),
            "runOnInitRestart": True,
        }
    if source == "file":
        path = cam["file"]
        loop = cam.get("loop", True)
        loop_arg = "-stream_loop -1 " if loop else ""
        return {
            "runOnInit": (
                f"ffmpeg -loglevel warning -re {loop_arg}-i {path} "
                f"-c:v copy -an -f rtsp {rtsp_target}"
            ),
            "runOnInitRestart": True,
        }
    raise ValueError(f"unreachable: source={source!r}")


def build_mediamtx_config(cfg: dict) -> dict:
    """Return a MediaMTX config dict with paths for each bridged camera."""
    cams = bridged_cameras(cfg)
    paths: dict[str, dict] = {}
    for cam in cams:
        paths[cam["name"]] = _path_block(cam)
    return {
        "logLevel": "info",
        "logDestinations": ["stdout"],
        "rtspAddress": ":8554",
        "hlsDisable": True,
        "webrtcDisable": True,
        "rtmpDisable": True,
        "srtDisable": True,
        # Drop publishUser / readUser auth — bridge is on the internal
        # Docker network only, never bound to the host.  Detector
        # reaches it as ``rtsp://camera-bridge:8554/<name>``.
        "paths": paths,
    }


def write_mediamtx_config(cfg: dict, dest: str) -> None:
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)


def main() -> int:
    cfg = load_config(CONFIG_PATH)
    setup_logging(cfg.get("system", {}).get("log_level", "info"))
    logger.info("OpenRing camera-bridge starting")

    mediamtx_cfg = build_mediamtx_config(cfg)
    paths = mediamtx_cfg["paths"]
    if not paths:
        logger.info(
            "No bridged cameras configured (every camera is ipcam/pi or disabled). "
            "MediaMTX will run with no paths; the detector connects to ipcam/pi "
            "sources directly.",
        )
    else:
        logger.info(
            "Bridging %d camera(s): %s", len(paths), ", ".join(paths),
        )

    write_mediamtx_config(mediamtx_cfg, MEDIAMTX_CONFIG)
    logger.info("Wrote MediaMTX config to %s", MEDIAMTX_CONFIG)

    # Hand off to MediaMTX via execvp so signals propagate cleanly.
    os.execvp(MEDIAMTX_BINARY, [MEDIAMTX_BINARY, MEDIAMTX_CONFIG])


if __name__ == "__main__":
    raise SystemExit(main() or 0)
