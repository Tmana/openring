"""Audio-relay-side glue around ``shared/audio_jwt.py``.

The JWT issue + verify code lives in shared/ so the web service (the
issuer) and this service (the verifier) can't drift out of sync.
This module adds the audio-relay-only ``JtiSet`` for replay
protection — a process-local one-shot enforcer.

Public API (re-exported for backwards-compatibility with main.py
imports of this module):

  * AudioJwt
  * JwtError
  * issue, verify, load_key_from_env (delegates to shared)
  * ENV_VAR, EXPECTED_ISSUER, EXPECTED_AUDIENCE
  * JtiSet (audio-relay-only)
"""

from __future__ import annotations

import threading
import time

from audio_jwt import (  # noqa: F401  re-export
    DEFAULT_LIFETIME_SECONDS,
    ENV_VAR,
    EXPECTED_AUDIENCE,
    EXPECTED_ISSUER,
    AudioJwt,
    JwtError,
    issue,
    load_key_from_env,
    verify,
)


class JtiSet:
    """In-memory set of used JWT IDs.

    Process-local — a relay restart resets the set, which means a
    captured token survives at most one restart-window of replay
    (5 minutes by default).  Documented trade-off in ``docs/AUDIO.md``.

    Periodic GC drops jtis whose corresponding exp has passed so the
    set doesn't grow unbounded across long-running processes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jtis: dict[str, int] = {}  # jti → exp epoch seconds

    def claim(self, jti: str, exp: int) -> bool:
        """True on first use of *jti*, False on replay."""
        with self._lock:
            self._gc_locked()
            if jti in self._jtis:
                return False
            self._jtis[jti] = exp
            return True

    def size(self) -> int:
        with self._lock:
            return len(self._jtis)

    def _gc_locked(self) -> None:
        now = int(time.time())
        stale = [j for j, exp in self._jtis.items() if exp < now]
        for j in stale:
            self._jtis.pop(j, None)
