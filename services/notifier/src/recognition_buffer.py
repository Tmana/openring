"""Short-lived TTL buffer that coalesces detection + recognition events.

The detector publishes ``openring:detections`` immediately when a person
appears.  The recognizer publishes ``openring:recognition`` ~half a
second to a couple seconds later (face detection + embedding +
similarity scan).  Without coalescence the notifier would dispatch the
detection notification *before* the recognizer has anything to say —
the operator would get a generic "person at the front door" ping, and
the face match would arrive too late to matter.

This buffer holds back qualifying detection events for up to
``coalesce_seconds`` (default 2.0) waiting for the matching
recognition to arrive on ``feedback_token``.  Three terminal states:

  * **recognition arrives in the window** → call ``on_dispatch`` with
    the detection event annotated with ``_recognition`` so the
    consumer can apply face rules.
  * **timeout fires** → call ``on_dispatch`` with the bare detection
    event (no recognition annotation).  The consumer falls through to
    the legacy ``notification_rules`` path.
  * **buffer is shutting down** → flush every pending entry through
    the timeout path so nothing is silently dropped.

Doorbell presses do NOT go through this buffer.  They're high-priority
and the design doc explicitly says we never hold a press waiting for a
face match.

Implementation: a single background thread polls a min-heap of
deadlines.  Add/remove operations are O(log n) and the worst case at
sane traffic is a few hundred entries — well below any tuning need.
"""

from __future__ import annotations

import heapq
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class _Pending:
    """One detection event held for coalescence."""

    feedback_token: str
    event: dict
    deadline: float  # monotonic seconds; expire at this time

    def __lt__(self, other: object) -> bool:
        # heapq comparison; fallback to id() so ties are stable.
        if isinstance(other, _Pending):
            return self.deadline < other.deadline
        return NotImplemented


# Sentinel timeout used when the buffer's poll loop shuts down — pump
# everything through the dispatcher rather than dropping it.
_FLUSH = object()


class RecognitionBuffer:
    """TTL coalescence buffer.

    Thread-safe.  All public methods can be called from any thread.
    """

    def __init__(
        self,
        on_dispatch: Callable[[dict], None],
        *,
        coalesce_seconds: float = 2.0,
        poll_interval: float = 0.1,
    ) -> None:
        self._on_dispatch = on_dispatch
        self._coalesce_seconds = coalesce_seconds
        self._poll_interval = poll_interval
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._heap: list[_Pending] = []
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        """Start the background timeout-poll thread.  Idempotent."""
        if self._worker is not None and self._worker.is_alive():
            return
        self._shutdown.clear()
        self._worker = threading.Thread(
            target=self._run, name="recognition-coalesce", daemon=True,
        )
        self._worker.start()

    def stop(self, *, flush: bool = True) -> None:
        """Stop the background thread.

        With ``flush=True`` (default) every still-pending event is
        dispatched through the timeout path before returning — losing
        a notification at shutdown is worse than dispatching one
        slightly late.
        """
        self._shutdown.set()
        self._wake.set()
        if self._worker is not None:
            self._worker.join(timeout=5.0)
        if flush:
            self._flush_all()

    def submit_detection(self, event: dict) -> None:
        """Buffer a detection event waiting for its recognition.

        Caller is responsible for deciding whether the event is a
        candidate for coalescence (face_rules.is_face_class).  Events
        without a feedback_token bypass — they're dispatched immediately
        because we can't correlate them.
        """
        token = str(event.get("feedback_token", ""))
        if not token:
            self._on_dispatch(event)
            return
        deadline = time.monotonic() + self._coalesce_seconds
        entry = _Pending(token, event, deadline)
        with self._lock:
            existing = self._pending.get(token)
            if existing is not None:
                # Duplicate — replace.  This shouldn't happen in
                # practice (feedback_token is unique per event) but if
                # the detector ever republishes, the second wins.
                logger.debug("Duplicate detection token %s — overwriting", token)
            self._pending[token] = entry
            heapq.heappush(self._heap, entry)
        self._wake.set()

    def submit_recognition(self, recognition: dict) -> None:
        """Coalesce: if a detection is waiting, dispatch it now.

        If no detection is waiting, drop the recognition.  Caller is
        responsible for persisting recognition rows separately — this
        buffer is purely a coalescence layer between detection and
        dispatch.
        """
        token = str(recognition.get("feedback_token", ""))
        if not token:
            return
        with self._lock:
            entry = self._pending.pop(token, None)
        if entry is None:
            return
        annotated = dict(entry.event)
        annotated["_recognition"] = recognition
        try:
            self._on_dispatch(annotated)
        except Exception:
            logger.exception("on_dispatch raised for token %s", token)

    def _run(self) -> None:
        while not self._shutdown.is_set():
            now = time.monotonic()
            wait = self._poll_interval
            with self._lock:
                while self._heap and self._heap[0].deadline <= now:
                    expired = heapq.heappop(self._heap)
                    # Skip if it's been overwritten or already
                    # dispatched via submit_recognition.
                    current = self._pending.get(expired.feedback_token)
                    if current is None or current is not expired:
                        continue
                    del self._pending[expired.feedback_token]
                    try:
                        self._on_dispatch(expired.event)
                    except Exception:
                        logger.exception(
                            "on_dispatch raised at coalesce timeout for token %s",
                            expired.feedback_token,
                        )
                if self._heap:
                    wait = max(0.0, min(wait, self._heap[0].deadline - now))
            self._wake.wait(timeout=wait)
            self._wake.clear()

    def _flush_all(self) -> None:
        """Dispatch every still-pending entry — used on shutdown."""
        with self._lock:
            entries = list(self._pending.values())
            self._pending.clear()
            self._heap.clear()
        for entry in entries:
            try:
                self._on_dispatch(entry.event)
            except Exception:
                logger.exception(
                    "on_dispatch raised during flush for token %s",
                    entry.feedback_token,
                )

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)
