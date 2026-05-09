"""ScarGuard / OpenRing clipper service.

Subscribes to ``openring:detections`` and ``openring:doorbell``,
verifies HMAC, and for every qualifying event:

  1. Schedules a clip write at ``event_ts + post_roll_seconds``.
  2. When the deadline fires, picks the segments covering
     ``[event_ts - pre_roll, event_ts + post_roll]`` from the
     per-camera ring, concats them into ``/data/clips/<token>.mp4``,
     and writes the metadata row to ``clips`` in openring.db.

Per-camera ffmpeg subprocesses populate the ring continuously
(see segmenter.py) so by the time the deadline fires the post-roll
window has been written.

A bounded ``ThreadPoolExecutor`` keeps clip writes off the listen
loop — a parade of detections never starves the subscriber.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import db as clipper_db
import redis as redis_lib
import yaml
from atomic_ref import AtomicRef
from config_watcher import ConfigWatcher
from healthcheck import start_heartbeat
from segmenter import CameraSegmenter
from settings import ClipperSettings, from_yaml, load_cameras
from writer import write_clip

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/openring.yml")
DETECTION_CHANNEL = "openring:detections"
DOORBELL_CHANNEL = "openring:doorbell"

_REDIS_RECONNECT_DELAY = 5
_REDIS_MAX_RECONNECT_DELAY = 60


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    return cfg if isinstance(cfg, dict) else {}


def setup_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stdout,
    )


def _build_segmenters(
    cameras: list[dict],
    settings: ClipperSettings,
) -> dict[str, CameraSegmenter]:
    """Spawn one CameraSegmenter per enabled camera with an RTSP URL."""
    out: dict[str, CameraSegmenter] = {}
    for c in cameras:
        seg = CameraSegmenter(
            camera_name=c["name"],
            rtsp_url=c["rtsp_url"],
            ring_dir=settings.ring_dir,
            segment_seconds=settings.segment_seconds,
            segments_per_camera=settings.segments_per_camera,
        )
        seg.start()
        out[c["name"]] = seg
    return out


def _stop_segmenters(segmenters: dict[str, CameraSegmenter]) -> None:
    for seg in segmenters.values():
        try:
            seg.stop()
        except Exception:
            logger.exception("Error stopping segmenter for %s", seg.camera_name)


def _parse_event_ts(event: dict) -> float:
    """Best-effort: turn the event's ISO timestamp into a Unix epoch float.

    Falls back to wall-clock now() if the event's timestamp is missing
    or unparseable.  Press events always carry a host-stamped UTC ISO
    timestamp; detection events do too.
    """
    raw = event.get("timestamp", "")
    if not isinstance(raw, str) or not raw:
        return time.time()
    try:
        return datetime.fromisoformat(raw).timestamp()
    except (ValueError, TypeError):
        return time.time()


def _make_clip(
    settings: ClipperSettings,
    segmenter: CameraSegmenter,
    event: dict,
) -> None:
    """Worker task: wait for post-roll, gather segments, write clip + DB row.

    Designed to be submitted to a worker pool — never raises.  Failures
    are logged and recorded as a clips-table row with ``error`` set so
    the web UI can show "clip unavailable" instead of an indefinite
    spinner.
    """
    feedback_token = event.get("feedback_token", "")
    camera = event.get("camera_name", segmenter.camera_name)
    if not feedback_token:
        logger.warning(
            "Skipping clip — event has no feedback_token (camera=%s class=%s)",
            camera, event.get("class_name"),
        )
        return

    event_ts = _parse_event_ts(event)
    deadline = event_ts + settings.post_roll_seconds

    # Sleep until post-roll window has completed + 1 segment of grace
    # so the segment containing the trailing edge is fully written.
    wait_seconds = max(0.0, deadline - time.time()) + settings.segment_seconds
    if wait_seconds > 0:
        time.sleep(wait_seconds)

    window_start = event_ts - settings.pre_roll_seconds - settings.segment_seconds
    window_end = event_ts + settings.post_roll_seconds + settings.segment_seconds
    segments = segmenter.segments_in_window(window_start, window_end)
    if not segments:
        logger.warning(
            "No segments in window for %s (token=%s, ts=%.1f)",
            camera, feedback_token, event_ts,
        )
        clipper_db.insert_failure(
            feedback_token, camera,
            f"no segments in window {window_start:.1f}..{window_end:.1f}",
        )
        return

    output = Path(settings.clips_dir) / f"{feedback_token}.mp4"
    size = write_clip(segments, output)
    if size is None:
        clipper_db.insert_failure(
            feedback_token, camera, "ffmpeg concat failed",
        )
        return

    duration = settings.pre_roll_seconds + settings.post_roll_seconds
    clipper_db.insert_clip(
        feedback_token=feedback_token,
        camera_name=camera,
        clip_path=str(output),
        pre_roll_seconds=settings.pre_roll_seconds,
        post_roll_seconds=settings.post_roll_seconds,
        duration_seconds=duration,
        size_bytes=size,
        segments_used=len(segments),
    )
    logger.info(
        "Wrote clip %s (camera=%s, %d segments, %.1f kB)",
        output.name, camera, len(segments), (size or 0) / 1024,
    )


def subscribe_loop(
    redis_cfg: dict,
    settings_ref: AtomicRef[ClipperSettings],
    segmenters_ref: AtomicRef[dict[str, CameraSegmenter]],
    pool: ThreadPoolExecutor,
    shutdown_event: threading.Event,
) -> None:
    """Listen for events, dispatch each into the worker pool."""
    from event_signing import load_key_from_env, verify_event

    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))
    hmac_key = load_key_from_env()
    if hmac_key is None:
        logger.warning("DETECTION_HMAC_KEY not set — accepting unsigned events")

    delay = _REDIS_RECONNECT_DELAY
    invalid_warned = False
    unsigned_warned = False
    signed_channels = (DETECTION_CHANNEL, DOORBELL_CHANNEL)

    while not shutdown_event.is_set():
        client: redis_lib.Redis | None = None
        pubsub: redis_lib.client.PubSub | None = None
        try:
            client = redis_lib.Redis(
                host=host, port=port,
                password=os.environ.get("REDIS_PASSWORD", "") or None,
                decode_responses=True,
            )
            pubsub = client.pubsub()
            pubsub.subscribe(*signed_channels)
            logger.info("Subscribed to %s", ", ".join(signed_channels))
            delay = _REDIS_RECONNECT_DELAY
            pathlib.Path("/tmp/healthy").touch(exist_ok=True)

            for message in pubsub.listen():
                if shutdown_event.is_set():
                    break
                if message["type"] != "message":
                    continue
                pathlib.Path("/tmp/healthy").touch(exist_ok=True)
                try:
                    event = json.loads(message["data"])
                except json.JSONDecodeError:
                    logger.warning("Malformed message dropped")
                    continue

                if hmac_key is not None:
                    if not verify_event(event, hmac_key):
                        if not invalid_warned:
                            logger.error(
                                "Rejecting %s event with invalid HMAC — NOT clipping. "
                                "Further at DEBUG.", message["channel"],
                            )
                            invalid_warned = True
                        else:
                            logger.debug("Invalid-signature event rejected")
                        continue
                elif not unsigned_warned:
                    unsigned_warned = True
                    logger.warning(
                        "Accepting unsigned %s event. Further at DEBUG.",
                        message["channel"],
                    )

                settings = settings_ref.get()
                if not settings.enabled:
                    continue
                if not settings.applies_to(
                    event.get("class_name", ""),
                    float(event.get("confidence", 0.0) or 0.0),
                ):
                    continue

                camera = event.get("camera_name", "")
                segmenters = segmenters_ref.get()
                seg = segmenters.get(camera)
                if seg is None:
                    logger.debug(
                        "No segmenter for camera %s — skipping clip", camera,
                    )
                    continue

                try:
                    pool.submit(_make_clip, settings, seg, event)
                except RuntimeError:
                    break

        except redis_lib.RedisError:
            if shutdown_event.is_set():
                break
            logger.exception("Redis connection lost — retrying in %ds", delay)
            time.sleep(delay)
            delay = min(delay * 2, _REDIS_MAX_RECONNECT_DELAY)
        finally:
            if pubsub is not None:
                try:
                    pubsub.unsubscribe()
                    pubsub.close()
                except Exception:
                    pass
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    logger.info("Subscription loop exited")


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.get("system", {}).get("log_level", "info"))
    logger.info("OpenRing clipper starting")

    clipper_db.init_db()
    start_heartbeat()

    settings = from_yaml(cfg)
    cameras = load_cameras(cfg) if settings.enabled else []
    segmenters = _build_segmenters(cameras, settings) if settings.enabled else {}

    settings_ref: AtomicRef[ClipperSettings] = AtomicRef(settings)
    segmenters_ref: AtomicRef[dict[str, CameraSegmenter]] = AtomicRef(segmenters)

    pool = ThreadPoolExecutor(
        max_workers=max(1, settings.max_concurrent_clips),
        thread_name_prefix="clipper-worker",
    )

    shutdown_event = threading.Event()

    def _shutdown(sig: int, _frame: object) -> None:
        logger.info("Received signal %s — shutting down", sig)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    def _on_config_change(new_cfg: dict) -> None:
        new_settings = from_yaml(new_cfg)
        settings_ref.set(new_settings)
        # Rebuild segmenters when the camera list changes.  This is the
        # naive "stop everything, start everything" approach; doable
        # cheaply because each segmenter is just an ffmpeg subprocess.
        old = segmenters_ref.get()
        _stop_segmenters(old)
        new_cameras = load_cameras(new_cfg) if new_settings.enabled else []
        new_segs = (
            _build_segmenters(new_cameras, new_settings)
            if new_settings.enabled else {}
        )
        segmenters_ref.set(new_segs)
        logger.info(
            "Config reloaded — clipper %s (%d cameras)",
            "enabled" if new_settings.enabled else "disabled",
            len(new_segs),
        )

    watcher = ConfigWatcher(CONFIG_PATH, _on_config_change)
    watcher.start()

    subscribe_loop(
        cfg.get("redis", {}),
        settings_ref, segmenters_ref,
        pool, shutdown_event,
    )

    watcher.stop()
    _stop_segmenters(segmenters_ref.get())
    pool.shutdown(wait=True, cancel_futures=False)
    logger.info("clipper stopped cleanly")


if __name__ == "__main__":
    main()
