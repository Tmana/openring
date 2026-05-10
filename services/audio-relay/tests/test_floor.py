"""Half-duplex floor arbiter tests.

The floor is the most subtle bit of the audio relay — wrong arbitration
either lets two parties talk over each other (loses the half-duplex
guarantee) or wedges the doorbell on a stale holder.  Cover both
sides of every transition.
"""

from __future__ import annotations

import pytest
from floor import SAFETY_TIMEOUT_SECONDS, Floor, Holder


class FakeClock:
    """Manual clock for the safety-timeout test paths."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fl() -> Floor:
    return Floor(now_fn=FakeClock())


@pytest.fixture
def fl_with_clock() -> tuple[Floor, FakeClock]:
    clock = FakeClock()
    return Floor(now_fn=clock), clock


class TestTake:
    def test_first_take_succeeds(self, fl: Floor):
        result = fl.take(Holder.PI)
        assert result.accepted is True
        assert result.new_holder is Holder.PI
        assert result.notify_other is True

    def test_re_take_by_same_party_is_noop(self, fl: Floor):
        fl.take(Holder.PI)
        result = fl.take(Holder.PI)
        assert result.accepted is True
        assert result.new_holder is Holder.PI
        # Other side already knows; don't re-notify.
        assert result.notify_other is False

    def test_take_by_other_when_held_is_rejected(self, fl: Floor):
        fl.take(Holder.PI)
        result = fl.take(Holder.BROWSER)
        assert result.accepted is False
        assert result.new_holder is Holder.PI
        assert result.notify_other is False

    def test_take_after_timeout_bumps_old_holder(
        self, fl_with_clock: tuple[Floor, FakeClock],
    ):
        fl, clock = fl_with_clock
        fl.take(Holder.PI)
        clock.advance(SAFETY_TIMEOUT_SECONDS + 1)
        result = fl.take(Holder.BROWSER)
        assert result.accepted is True
        assert result.new_holder is Holder.BROWSER
        assert result.notify_other is True

    def test_none_requester_rejected(self, fl: Floor):
        with pytest.raises(ValueError):
            fl.take(Holder.NONE)


class TestRelease:
    def test_release_by_holder_succeeds(self, fl: Floor):
        fl.take(Holder.PI)
        result = fl.release(Holder.PI)
        assert result.accepted is True
        assert result.new_holder is Holder.NONE
        assert result.notify_other is True

    def test_release_by_non_holder_silently_ignored(self, fl: Floor):
        fl.take(Holder.PI)
        result = fl.release(Holder.BROWSER)
        assert result.accepted is False
        assert result.new_holder is Holder.PI
        assert result.notify_other is False

    def test_release_when_idle_is_noop(self, fl: Floor):
        result = fl.release(Holder.PI)
        assert result.accepted is False
        assert result.new_holder is Holder.NONE


class TestForceRelease:
    def test_force_release_when_held_clears(self, fl: Floor):
        fl.take(Holder.PI)
        result = fl.force_release()
        assert result.accepted is True
        assert result.new_holder is Holder.NONE
        assert result.notify_other is True

    def test_force_release_when_idle_is_noop(self, fl: Floor):
        result = fl.force_release()
        assert result.accepted is True
        assert result.notify_other is False


class TestStaleAndHeldFor:
    def test_held_for_returns_none_when_idle(self, fl: Floor):
        assert fl.held_for() is None

    def test_held_for_increments(self, fl_with_clock: tuple[Floor, FakeClock]):
        fl, clock = fl_with_clock
        fl.take(Holder.PI)
        clock.advance(2.5)
        assert fl.held_for() == pytest.approx(2.5)

    def test_stale_after_timeout(self, fl_with_clock: tuple[Floor, FakeClock]):
        fl, clock = fl_with_clock
        fl.take(Holder.BROWSER)
        clock.advance(SAFETY_TIMEOUT_SECONDS - 0.1)
        assert fl.stale() is False
        clock.advance(0.2)
        assert fl.stale() is True

    def test_release_clears_held_for(self, fl: Floor):
        fl.take(Holder.PI)
        fl.release(Holder.PI)
        assert fl.held_for() is None
