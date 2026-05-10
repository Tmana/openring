"""Tests for the clipper service's main worker (path-traversal etc.)."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Path traversal regression for C2 from the v0.2 self-review ───────────


class TestFeedbackTokenGuard:
    """A compromised detector publishing a HMAC-signed event with a
    slash-laden feedback_token would otherwise let the writer escape
    clips_dir.  ``_FEEDBACK_TOKEN_RE`` blocks that.

    The HMAC verification stops *external* injection; this test guards
    against insider risk and accidental future bugs that loosen the
    publisher's contract."""

    @pytest.fixture
    def mocks(self, tmp_path: Path):
        from main import _make_clip
        from settings import ClipperSettings
        settings = ClipperSettings(
            enabled=True,
            clips_dir=str(tmp_path),
            ring_dir=str(tmp_path / "ring"),
        )
        segmenter = MagicMock()
        segmenter.camera_name = "front-door"
        # No segments returned — irrelevant; we should reject before reaching here.
        segmenter.segments_in_window.return_value = []
        return _make_clip, settings, segmenter

    @pytest.mark.parametrize("bad_token", [
        "../../etc/passwd",
        "/absolute/path",
        "tok with spaces",
        "tok\\with\\backslashes",
        "../escape",
        "tok/sub",
        "x",                  # too short
        "x" * 200,            # too long
        "tok\x00null",
        "$(rm -rf)",
    ])
    def test_bad_token_refused_before_segment_lookup(self, mocks, bad_token):
        _make_clip, settings, segmenter = mocks
        event = {
            "feedback_token": bad_token,
            "camera_name": "front-door",
            "timestamp": "2026-05-09T18:00:00+00:00",
        }
        # _make_clip is supposed to return early without writing anything
        # OR calling segments_in_window — patch insert_pending so it can't
        # blow up the test runner.
        with patch("main.clipper_db.insert_failure"), \
             patch("main.write_clip") as write_clip, \
             patch("main.time.sleep"):
            _make_clip(settings, segmenter, event)
        # The writer should NEVER have been called.
        write_clip.assert_not_called()
        # And segments_in_window must not have run either — we bail before
        # we'd ever look at the ring.
        segmenter.segments_in_window.assert_not_called()

    @pytest.mark.parametrize("good_token", [
        "abc12345",                            # min length 8
        "feedback-token-with-dashes",
        "underscores_are_ok",
        "0" * 32,
        "x" * 128,                             # max length 128
        # Real shape: uuid4().hex
        "0123456789abcdef0123456789abcdef",
    ])
    def test_good_token_accepted(self, mocks, good_token, caplog):
        _make_clip, settings, segmenter = mocks
        event = {
            "feedback_token": good_token,
            "camera_name": "front-door",
            "timestamp": "2026-05-09T18:00:00+00:00",
        }
        with patch("main.clipper_db.insert_failure") as update_failure, \
             patch("main.write_clip"), \
             patch("main.time.sleep"), \
             caplog.at_level(logging.INFO):
            _make_clip(settings, segmenter, event)
        # The token-shape rejection only fires for bad tokens; a good
        # token reaches the no-segments-in-window path instead.
        assert not any(
            "not a valid token shape" in rec.getMessage()
            for rec in caplog.records
        )
        # And the failure record (if written) is for the
        # no-segments-in-window reason, not the token-shape one.
        for call in update_failure.call_args_list:
            assert "not a valid token shape" not in (call.kwargs.get("error", "") or "")
