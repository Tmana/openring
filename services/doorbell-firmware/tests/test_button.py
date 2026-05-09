"""Tests for the button service.  Hardware mocked via gpiozero MockFactory."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, patch

import button
import pytest


@pytest.fixture
def mock_gpio() -> None:
    """Replace gpiozero's pin factory with the mock backend."""
    from gpiozero import Device
    from gpiozero.pins.mock import MockFactory
    Device.pin_factory = MockFactory()


@pytest.fixture
def queue_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "queue.json"
    monkeypatch.setattr(button, "QUEUE_PATH", path)
    return path


class TestQueuePersistence:
    def test_load_when_missing_returns_empty(self, queue_path: Path) -> None:
        q = button._load_queue()
        assert isinstance(q, deque)
        assert len(q) == 0

    def test_save_then_load_round_trips(self, queue_path: Path) -> None:
        q = deque(maxlen=button.QUEUE_MAX)
        q.append({"timestamp": "2026-05-09T18:00:00Z", "device_id": "front-door"})
        q.append({"timestamp": "2026-05-09T18:00:01Z", "device_id": "front-door"})
        button._save_queue(q)
        loaded = button._load_queue()
        assert len(loaded) == 2
        assert loaded[0]["timestamp"] == "2026-05-09T18:00:00Z"

    def test_load_corrupt_falls_back_to_empty(self, queue_path: Path) -> None:
        queue_path.write_text("not valid json")
        assert len(button._load_queue()) == 0


class TestFlush:
    def _settings(self) -> dict[str, str]:
        return {
            "HOST_BASE_URL": "https://h.example.com",
            "DEVICE_TOKEN": "tok",
            "VERSION": "test",
        }

    def test_drops_on_success(self, queue_path: Path) -> None:
        import threading
        q: deque[dict] = deque(
            [{"timestamp": "t0", "device_id": "d"}],
            maxlen=button.QUEUE_MAX,
        )
        with patch("button.post_to_host") as post:
            post.return_value = MagicMock(ok=True, status_code=200)
            button._flush(self._settings(), q, threading.Lock())
        assert len(q) == 0

    def test_drops_on_4xx(self, queue_path: Path) -> None:
        import threading
        q: deque[dict] = deque(
            [{"timestamp": "t0", "device_id": "d"}],
            maxlen=button.QUEUE_MAX,
        )
        with patch("button.post_to_host") as post:
            post.return_value = MagicMock(ok=False, status_code=401)
            button._flush(self._settings(), q, threading.Lock())
        # 4xx is unrecoverable — drop rather than spin forever
        assert len(q) == 0

    def test_keeps_on_network_failure(self, queue_path: Path) -> None:
        import threading
        q: deque[dict] = deque(
            [{"timestamp": "t0", "device_id": "d"}],
            maxlen=button.QUEUE_MAX,
        )
        with patch("button.post_to_host") as post:
            post.return_value = None  # all retries exhausted
            button._flush(self._settings(), q, threading.Lock())
        # Stays queued — periodic retry will pick it up
        assert len(q) == 1
