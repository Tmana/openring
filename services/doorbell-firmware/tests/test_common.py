"""Tests for the shared device-side HTTP client."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import common
import pytest
import requests


@pytest.fixture
def secrets_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "secrets.env"
    path.write_text(
        "HOST_BASE_URL=https://openring.example.com\n"
        "DEVICE_TOKEN=tok-abc\n"
        "DEVICE_ID=front-door\n"
        "VERSION=0.0.1\n"
        "# this is a comment\n"
        "\n",
    )
    monkeypatch.setattr(common, "SECRETS_FILE", path)
    return path


class TestLoadSettings:
    def test_parses_keys(self, secrets_file: Path) -> None:
        settings = common.load_settings()
        assert settings["HOST_BASE_URL"] == "https://openring.example.com"
        assert settings["DEVICE_TOKEN"] == "tok-abc"
        assert settings["VERSION"] == "0.0.1"

    def test_skips_comments_and_blanks(self, secrets_file: Path) -> None:
        # Already covered by parses_keys, but make it explicit.
        assert "comment" not in common.load_settings()

    def test_missing_file_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(common, "SECRETS_FILE", tmp_path / "nope.env")
        with pytest.raises(FileNotFoundError):
            common.load_settings()


class TestPostToHost:
    def _settings(self) -> dict[str, str]:
        return {
            "HOST_BASE_URL": "https://openring.example.com",
            "DEVICE_TOKEN": "tok-abc",
            "VERSION": "0.0.1",
        }

    def test_success_returns_response(self) -> None:
        with patch("common.requests.post") as post:
            post.return_value.ok = True
            post.return_value.status_code = 200
            resp = common.post_to_host(self._settings(), "/api/x", {"foo": 1})
        assert resp is not None
        assert resp.ok
        # Bearer header set
        kwargs = post.call_args.kwargs
        assert kwargs["headers"]["Authorization"] == "Bearer tok-abc"

    def test_4xx_does_not_retry(self) -> None:
        with patch("common.requests.post") as post:
            post.return_value.ok = False
            post.return_value.status_code = 401
            post.return_value.text = "unauthorized"
            resp = common.post_to_host(self._settings(), "/api/x", {})
        assert resp is not None
        assert resp.status_code == 401
        # Single attempt
        assert post.call_count == 1

    def test_5xx_retries_then_returns(self) -> None:
        with patch("common.requests.post") as post, patch("common.time.sleep"):
            post.return_value.ok = False
            post.return_value.status_code = 502
            post.return_value.text = "bad gateway"
            common.post_to_host(self._settings(), "/api/x", {}, max_retries=3)
        assert post.call_count == 3

    def test_network_error_retries(self) -> None:
        with patch("common.requests.post") as post, patch("common.time.sleep"):
            post.side_effect = requests.ConnectionError("kaboom")
            resp = common.post_to_host(self._settings(), "/api/x", {}, max_retries=3)
        assert resp is None
        assert post.call_count == 3

    def test_missing_settings_returns_none(self) -> None:
        resp = common.post_to_host({}, "/api/x", {})
        assert resp is None
