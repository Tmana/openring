"""Half-duplex floor arbiter.

The half-duplex state machine documented in ``docs/AUDIO.md``
§"Half-duplex state machine".  Whoever asks first wins; the loser
gets ``ErrorCode.FLOOR_BUSY``.  A 30-second safety releases a
held floor regardless of the holder.

This module is pure data — no I/O, no network, no asyncio.  The
calling layer (``main.py``) is responsible for sending the side-
effect frames in response to ``ArbitrationResult``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum

SAFETY_TIMEOUT_SECONDS = 30.0


class Holder(Enum):
    NONE = "none"
    PI = "pi"
    BROWSER = "browser"


@dataclass(frozen=True)
class ArbitrationResult:
    """Outcome of a request to take or release the floor.

    Attributes:
        accepted     whether the requester now holds the floor (or
                     has released it cleanly)
        new_holder   the floor's state AFTER the request
        notify_other if True, the OTHER side should be told about
                     this change (e.g. push the floor-handoff state
                     frame to whoever is paired)
    """

    accepted: bool
    new_holder: Holder
    notify_other: bool


class Floor:
    """Per-device floor state.

    One ``Floor`` instance per paired device.  Thread-safe — the
    audio-relay's WS handlers run in asyncio tasks but the `take`
    / `release` calls take a lock so a Pi-side message and a
    browser-side message can't race in the few microseconds between
    them.
    """

    __slots__ = ("_holder", "_taken_at", "_lock", "_now")

    def __init__(self, now_fn=None) -> None:
        self._holder: Holder = Holder.NONE
        self._taken_at: float | None = None
        self._lock = threading.Lock()
        # Injectable clock so tests can drive the safety timer without
        # actually sleeping.  Defaults to time.monotonic.
        self._now = now_fn or time.monotonic

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    @property
    def holder(self) -> Holder:
        with self._lock:
            return self._holder

    def take(self, requester: Holder) -> ArbitrationResult:
        """Try to grab the floor for *requester*.

        Returns whether the request was accepted.  Re-grabbing while
        you already hold the floor is a no-op (accepted, same holder,
        no notify).  Grabbing while the OTHER side holds is rejected
        unless the safety timeout has elapsed — in that case the
        existing holder is forcibly bumped.
        """
        if requester is Holder.NONE:
            raise ValueError("requester cannot be NONE")
        with self._lock:
            if self._holder is requester:
                return ArbitrationResult(True, self._holder, notify_other=False)
            if self._holder is Holder.NONE:
                self._holder = requester
                self._taken_at = self._now()
                return ArbitrationResult(True, self._holder, notify_other=True)
            # Other side holds.  Bump if the safety timeout has elapsed.
            if self._is_stale_locked():
                self._holder = requester
                self._taken_at = self._now()
                return ArbitrationResult(True, self._holder, notify_other=True)
            return ArbitrationResult(False, self._holder, notify_other=False)

    def release(self, releaser: Holder) -> ArbitrationResult:
        """Release the floor on behalf of *releaser*.

        Releasing when you don't hold the floor is silently a no-op
        — that's the right behaviour for crash recovery (Pi
        reconnects after a hiccup and sends a release because its
        client thinks it was talking).
        """
        with self._lock:
            if self._holder is not releaser:
                return ArbitrationResult(False, self._holder, notify_other=False)
            self._holder = Holder.NONE
            self._taken_at = None
            return ArbitrationResult(True, self._holder, notify_other=True)

    def force_release(self) -> ArbitrationResult:
        """Release the floor unconditionally.

        Used by the audio-relay when one side disconnects mid-talk.
        """
        with self._lock:
            if self._holder is Holder.NONE:
                return ArbitrationResult(True, self._holder, notify_other=False)
            self._holder = Holder.NONE
            self._taken_at = None
            return ArbitrationResult(True, self._holder, notify_other=True)

    def stale(self) -> bool:
        """True iff the floor is currently held past the safety timeout."""
        with self._lock:
            return self._is_stale_locked()

    def held_for(self) -> float | None:
        """Seconds the current holder has been on the floor, or None
        if no one's on it."""
        with self._lock:
            if self._taken_at is None:
                return None
            return self._now() - self._taken_at

    # ------------------------------------------------------------------
    # Internal — must be called with self._lock held
    # ------------------------------------------------------------------

    def _is_stale_locked(self) -> bool:
        if self._holder is Holder.NONE or self._taken_at is None:
            return False
        return (self._now() - self._taken_at) > SAFETY_TIMEOUT_SECONDS
