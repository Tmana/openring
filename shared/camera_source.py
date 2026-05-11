"""Resolve a ``cameras[]`` entry to the RTSP URL the detector connects to.

The detector ingests RTSP exclusively.  Different source types map to
different URLs:

  * ``ipcam`` (or no ``source`` field — pre-v0.6 default) — use the
    camera's own ``rtsp_url`` directly.  No bridge involved.
  * ``pi`` — same as ``ipcam`` operationally; the field is purely
    informational so admin UI can show a different icon and the
    heartbeat watchdog knows which cameras are paired Pi devices.
  * ``webcam`` / ``file`` — the ``camera-bridge`` sidecar ingests
    these and re-serves them at ``rtsp://camera-bridge:8554/<name>``.

This helper is the single source of truth for that mapping.  Both
the detector and the future Cameras-admin page should call into it
so an operator changing the schema in one place doesn't accidentally
break the other.
"""

from __future__ import annotations

import os

# Where the bridge is reachable from inside the Docker network.  Override
# via env when wiring this into something other than the bundled compose
# stack (e.g. a Kubernetes Service hostname).
BRIDGE_HOST = os.environ.get("CAMERA_BRIDGE_HOST", "camera-bridge")
BRIDGE_PORT = int(os.environ.get("CAMERA_BRIDGE_PORT", "8554"))

# Sources the bridge serves.  The detector talks to bridge for these,
# to the camera directly for everything else.
_BRIDGED_SOURCES = frozenset({"webcam", "file"})


def resolve_rtsp_url(cam: dict) -> str | None:
    """Return the RTSP URL the detector should connect to, or None.

    None means the camera entry is malformed (missing rtsp_url for an
    ipcam, missing name for a bridged source).  Callers should log and
    skip; never default to a placeholder URL.

    The function is intentionally tolerant of the v0.1 schema where
    ``source`` doesn't exist — those entries are treated as ``ipcam``
    so existing deployments keep working without a config edit.
    """
    if not isinstance(cam, dict):
        return None
    source = (cam.get("source") or "").lower()
    name = cam.get("name")

    if source in _BRIDGED_SOURCES:
        if not isinstance(name, str) or not name:
            return None
        return f"rtsp://{BRIDGE_HOST}:{BRIDGE_PORT}/{name}"

    # ipcam / pi / unset → direct connect via the explicit rtsp_url.
    url = cam.get("rtsp_url")
    if not isinstance(url, str) or not url:
        return None
    return url


def is_bridged(cam: dict) -> bool:
    """True iff ``cam`` is served by ``camera-bridge`` rather than direct.

    Used by the admin UI and the heartbeat watchdog to skip
    bridge-served cameras when their "is this hardware online?"
    question doesn't apply (a file source is always "online" by
    construction; a USB webcam's heartbeat is the bridge's process
    health, not a separate device).
    """
    if not isinstance(cam, dict):
        return False
    return (cam.get("source") or "").lower() in _BRIDGED_SOURCES
