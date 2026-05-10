"""recognition_buffer.RecognitionBuffer — coalescence + timeout tests.

Times in these tests are deliberately small (50ms coalesce window, 5ms
poll) so the suite runs fast.  We don't pretend to test exact wallclock
timing; just the state transitions.
"""

from __future__ import annotations

import threading
import time

import pytest
from recognition_buffer import RecognitionBuffer


@pytest.fixture
def buffer():
    """RecognitionBuffer with a tight coalesce window for fast tests."""
    dispatched: list[dict] = []
    lock = threading.Lock()

    def on_dispatch(event: dict) -> None:
        with lock:
            dispatched.append(event)

    buf = RecognitionBuffer(
        on_dispatch,
        coalesce_seconds=0.05,
        poll_interval=0.005,
    )
    buf.start()
    yield buf, dispatched
    buf.stop(flush=False)


def _det(token: str = "abc12345", **extra) -> dict:
    base = {
        "feedback_token": token,
        "camera_name": "front-door",
        "class_name": "person",
        "confidence": 0.85,
    }
    base.update(extra)
    return base


def _rec(token: str = "abc12345", **extra) -> dict:
    base = {
        "feedback_token": token,
        "camera_name": "front-door",
        "status": "matched",
        "label": "Sarah",
        "face_id": 1,
    }
    base.update(extra)
    return base


def _wait_for(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class TestCoalescence:
    def test_recognition_arrives_in_window(self, buffer):
        buf, dispatched = buffer
        buf.submit_detection(_det())
        buf.submit_recognition(_rec())
        # Coalesced: dispatch fires once with the recognition annotated.
        assert _wait_for(lambda: len(dispatched) == 1)
        assert dispatched[0]["_recognition"]["label"] == "Sarah"

    def test_timeout_dispatches_bare_event(self, buffer):
        buf, dispatched = buffer
        buf.submit_detection(_det())
        # No recognition arrives — wait past the coalesce window.
        assert _wait_for(lambda: len(dispatched) == 1, timeout=2.0)
        assert "_recognition" not in dispatched[0]

    def test_recognition_after_timeout_is_dropped(self, buffer):
        buf, dispatched = buffer
        buf.submit_detection(_det())
        assert _wait_for(lambda: len(dispatched) == 1, timeout=2.0)
        # Recognition arrives late — buffer's already dispatched the
        # detection on timeout.  The late recognition is dropped.
        buf.submit_recognition(_rec())
        time.sleep(0.05)
        assert len(dispatched) == 1


class TestEdgeCases:
    def test_detection_without_token_dispatches_immediately(self, buffer):
        buf, dispatched = buffer
        buf.submit_detection({"camera_name": "x", "class_name": "person"})
        assert _wait_for(lambda: len(dispatched) == 1, timeout=0.5)
        assert "feedback_token" not in dispatched[0]

    def test_recognition_for_unknown_token_no_op(self, buffer):
        buf, dispatched = buffer
        buf.submit_recognition(_rec(token="not-buffered"))
        time.sleep(0.05)
        assert dispatched == []

    def test_duplicate_detection_overwrites(self, buffer):
        buf, dispatched = buffer
        buf.submit_detection(_det(token="dupe", camera_name="A"))
        buf.submit_detection(_det(token="dupe", camera_name="B"))
        # Coalesce by recognition arriving — should see only the second
        # detection's camera_name.
        buf.submit_recognition(_rec(token="dupe"))
        assert _wait_for(lambda: len(dispatched) == 1)
        assert dispatched[0]["camera_name"] == "B"

    def test_pending_count_reflects_buffer_state(self, buffer):
        buf, _ = buffer
        assert buf.pending_count == 0
        buf.submit_detection(_det(token="t1"))
        buf.submit_detection(_det(token="t2"))
        assert buf.pending_count == 2
        buf.submit_recognition(_rec(token="t1"))
        assert _wait_for(lambda: buf.pending_count == 1, timeout=0.5)


class TestShutdown:
    def test_flush_dispatches_pending(self):
        dispatched: list[dict] = []
        buf = RecognitionBuffer(
            lambda e: dispatched.append(e),
            coalesce_seconds=10.0,  # very long — would never fire on its own
            poll_interval=0.005,
        )
        buf.start()
        buf.submit_detection(_det(token="t1"))
        buf.submit_detection(_det(token="t2"))
        buf.stop(flush=True)
        assert len(dispatched) == 2

    def test_no_flush_drops_pending(self):
        dispatched: list[dict] = []
        buf = RecognitionBuffer(
            lambda e: dispatched.append(e),
            coalesce_seconds=10.0,
            poll_interval=0.005,
        )
        buf.start()
        buf.submit_detection(_det(token="t1"))
        buf.stop(flush=False)
        assert dispatched == []


class TestCallbackErrors:
    def test_callback_exception_does_not_break_buffer(self):
        # First call raises; second call must still succeed.
        seen: list[str] = []
        flip = {"first": True}

        def on_dispatch(event: dict) -> None:
            if flip["first"]:
                flip["first"] = False
                raise RuntimeError("boom")
            seen.append(event["feedback_token"])

        buf = RecognitionBuffer(on_dispatch, coalesce_seconds=0.05, poll_interval=0.005)
        buf.start()
        buf.submit_detection(_det(token="t1"))
        # Wait for the first dispatch attempt (which raises).
        time.sleep(0.2)
        buf.submit_detection(_det(token="t2"))
        assert _wait_for(lambda: "t2" in seen, timeout=1.0)
        buf.stop(flush=False)
