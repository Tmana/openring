"""OpenRing recognizer service.

Subscribes to ``openring:detections``, verifies HMAC, and for every
qualifying event:

  1. Loads the snapshot from /data/snapshots/<feedback_token>.jpg
  2. Crops the bbox + padding, runs face detection + embedding
  3. Scores against every enabled enrolled face's embedding cluster
  4. Persists a recognitions row and publishes on
     ``openring:recognition`` with HMAC

A bounded ThreadPoolExecutor keeps the inference off the listen loop;
backpressure is bounded queue + drop-with-warning, never block-and-stall.

The recognizer is opt-in via ``face_recognition.enabled`` in
openring.yml — when disabled the service idles and does no work.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import db as recognizer_db
import redis as redis_lib
import yaml
from atomic_ref import AtomicRef
from config_watcher import ConfigWatcher
from event_signing import load_key_from_env, sign_event, verify_event
from healthcheck import start_heartbeat
from recognizer import recognize_crop
from settings import RecognizerSettings, from_yaml

# Same shape regex as services/clipper/src/main.py — feedback_token is
# concatenated into a snapshot filesystem path below; HMAC stops external
# injection, but a compromised detector publishing a signed payload with
# a slash-laden token would otherwise let us escape /data/snapshots.
_FEEDBACK_TOKEN_RE = re.compile(r"^[a-zA-Z0-9_-]{8,128}$")

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/openring.yml")
DETECTION_CHANNEL = "openring:detections"
RECOGNITION_CHANNEL = "openring:recognition"

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


def _bbox_from_event(event: dict) -> tuple[int, int, int, int] | None:
    """Pull (x1,y1,x2,y2) ints out of a detection event, or None if absent."""
    bbox = event.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        return (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))
    except (TypeError, ValueError):
        return None


def _publish_recognition(
    redis_client: "redis_lib.Redis",
    hmac_key: bytes | None,
    event: dict,
    result_dict: dict,
) -> None:
    """HMAC-sign and publish a recognition outcome onto Redis.

    The payload mirrors the upstream detection shape (feedback_token,
    camera_name, timestamp) so downstream subscribers can correlate
    without joining through the events table.  If hmac_key is None we
    publish unsigned and let receivers warn — same forgiving posture
    other services in the stack use.
    """
    payload: dict = {
        "feedback_token": result_dict["feedback_token"],
        "camera_name": result_dict["camera_name"],
        "status": result_dict["status"],
        "face_id": result_dict.get("face_id"),
        "label": result_dict.get("label"),
        "score": result_dict.get("score"),
        "bbox": result_dict.get("bbox"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if hmac_key is not None:
        payload = sign_event(payload, hmac_key)
    try:
        redis_client.publish(RECOGNITION_CHANNEL, json.dumps(payload))
    except redis_lib.RedisError:
        logger.exception("Failed to publish recognition for %s", payload.get("feedback_token"))


def _process_event(
    settings: RecognizerSettings,
    event: dict,
    redis_client: "redis_lib.Redis",
    hmac_key: bytes | None,
) -> None:
    """Worker task: snapshot → detect → embed → match → persist + publish.

    Submitted to a bounded ThreadPoolExecutor.  Never raises out — any
    exception surfaces as a recognitions row with status=error so the
    web events page can still render "matching failed" instead of
    showing a permanent spinner.
    """
    feedback_token = str(event.get("feedback_token", ""))
    camera = str(event.get("camera_name", ""))
    if not feedback_token or not _FEEDBACK_TOKEN_RE.match(feedback_token):
        logger.error(
            "Refusing recognition — feedback_token %r is not a valid token shape",
            feedback_token,
        )
        return

    bbox = _bbox_from_event(event)
    if bbox is None:
        logger.debug("Skipping recognition — event has no bbox (%s)", feedback_token)
        return

    snapshot_path = os.path.join(settings.snapshots_dir, f"{feedback_token}.jpg")
    if not os.path.isfile(snapshot_path):
        logger.warning("Snapshot missing for %s — recording error", feedback_token)
        recognizer_db.insert_recognition(
            feedback_token=feedback_token,
            camera_name=camera,
            status="error",
            error="snapshot file missing",
        )
        return

    try:
        known = recognizer_db.list_enabled_faces()
    except Exception:
        logger.exception("Failed to load enrolled faces from DB")
        recognizer_db.insert_recognition(
            feedback_token=feedback_token,
            camera_name=camera,
            status="error",
            error="db read failed",
        )
        return

    try:
        result = recognize_crop(
            snapshot_path=snapshot_path,
            bbox=bbox,
            padding_pct=settings.bbox_padding_pct,
            known_faces=known,
            tolerance=settings.tolerance,
        )
    except Exception as e:
        logger.exception("Recognition crashed for %s", feedback_token)
        recognizer_db.insert_recognition(
            feedback_token=feedback_token,
            camera_name=camera,
            status="error",
            error=str(e)[:500],
        )
        return

    bbox_json: str | None = None
    if result.bbox is not None:
        bbox_json = json.dumps(list(result.bbox))

    recognizer_db.insert_recognition(
        feedback_token=feedback_token,
        camera_name=camera,
        status=result.status,
        face_id=result.face_id,
        label=result.label,
        score=result.score,
        bbox=bbox_json,
    )

    _publish_recognition(
        redis_client, hmac_key, event,
        {
            "feedback_token": feedback_token,
            "camera_name": camera,
            "status": result.status,
            "face_id": result.face_id,
            "label": result.label,
            "score": result.score,
            "bbox": list(result.bbox) if result.bbox is not None else None,
        },
    )

    if result.status == "matched":
        logger.info(
            "Matched %s as %s (score=%.3f) for token=%s",
            camera, result.label, result.score or 0.0, feedback_token,
        )
    else:
        logger.debug(
            "Recognition status=%s for token=%s (camera=%s)",
            result.status, feedback_token, camera,
        )


def subscribe_loop(
    redis_cfg: dict,
    settings_ref: AtomicRef[RecognizerSettings],
    pool: ThreadPoolExecutor,
    shutdown_event: threading.Event,
) -> None:
    """Listen for detection events, dispatch each into the worker pool.

    Reconnect-with-backoff structure mirrors clipper/notifier — same
    handful of edge cases (transient Redis flap, malformed JSON,
    invalid HMAC) handled the same way for consistency.
    """
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))
    hmac_key = load_key_from_env()
    if hmac_key is None:
        logger.warning("DETECTION_HMAC_KEY not set — accepting unsigned events")

    delay = _REDIS_RECONNECT_DELAY
    invalid_warned = False
    unsigned_warned = False

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
            pubsub.subscribe(DETECTION_CHANNEL)
            logger.info("Subscribed to %s", DETECTION_CHANNEL)
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
                                "Rejecting %s event with invalid HMAC — NOT recognizing. "
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
                    str(event.get("class_name", "")),
                    float(event.get("confidence", 0.0) or 0.0),
                ):
                    continue

                try:
                    pool.submit(_process_event, settings, event, client, hmac_key)
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
    logger.info("OpenRing recognizer starting")

    recognizer_db.init_db()
    start_heartbeat()

    settings = from_yaml(cfg)
    settings_ref: AtomicRef[RecognizerSettings] = AtomicRef(settings)

    pool = ThreadPoolExecutor(
        max_workers=max(1, settings.max_concurrent_workers),
        thread_name_prefix="recognizer-worker",
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
        logger.info(
            "Config reloaded — recognizer %s",
            "enabled" if new_settings.enabled else "disabled",
        )

    watcher = ConfigWatcher(CONFIG_PATH, _on_config_change)
    watcher.start()

    subscribe_loop(
        cfg.get("redis", {}),
        settings_ref, pool, shutdown_event,
    )

    watcher.stop()
    pool.shutdown(wait=True, cancel_futures=False)
    logger.info("recognizer stopped cleanly")


if __name__ == "__main__":
    main()
