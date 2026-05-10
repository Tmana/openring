"""JWT issue + verify + replay protection tests.

Covers the audio-relay's auth path end-to-end: web service issues
the JWT, audio-relay verifies it, second use is rejected.  All in
pure Python — no actual network or filesystem.
"""

from __future__ import annotations

import base64
import json
import time

import auth as audio_auth
import pytest
from auth import (
    EXPECTED_AUDIENCE,
    EXPECTED_ISSUER,
    AudioJwt,
    JtiSet,
    JwtError,
    issue,
    load_key_from_env,
    verify,
)

KEY = b"\x01" * 32  # arbitrary; only the bytes matter for HS256


# ── issue + verify happy path ────────────────────────────────────────


class TestIssueAndVerify:
    def test_round_trip(self):
        token = issue(KEY, sub="alice", device_id="front-door", jti="j1")
        claims = verify(token, KEY)
        assert isinstance(claims, AudioJwt)
        assert claims.sub == "alice"
        assert claims.device_id == "front-door"
        assert claims.jti == "j1"
        # exp is iat + 300 by default
        assert claims.exp > int(time.time())

    def test_custom_lifetime(self):
        token = issue(
            KEY, sub="a", device_id="d", jti="j", lifetime_seconds=10, now=1000,
        )
        claims = verify(token, KEY, now=1005)
        assert claims.exp == 1010

    def test_missing_required_fields(self):
        with pytest.raises(ValueError):
            issue(KEY, sub="", device_id="d", jti="j")
        with pytest.raises(ValueError):
            issue(KEY, sub="a", device_id="", jti="j")
        with pytest.raises(ValueError):
            issue(KEY, sub="a", device_id="d", jti="")


# ── verify rejects every breakage ────────────────────────────────────


class TestVerifyRejects:
    def _good(self, **overrides):
        defaults = dict(sub="alice", device_id="front-door", jti="j")
        defaults.update(overrides)
        return issue(KEY, **defaults)

    def test_garbage_string(self):
        with pytest.raises(JwtError, match="three dot-separated"):
            verify("not-a-jwt", KEY)

    def test_wrong_signing_key(self):
        token = self._good()
        with pytest.raises(JwtError, match="signature mismatch"):
            verify(token, b"\x02" * 32)

    def test_wrong_alg(self):
        # Forge a token with alg=none
        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "none", "typ": "JWT"}).encode(),
        ).rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(
            json.dumps({
                "iss": EXPECTED_ISSUER, "aud": EXPECTED_AUDIENCE,
                "sub": "a", "device_id": "d", "jti": "j",
                "iat": 0, "exp": int(time.time()) + 60,
            }).encode(),
        ).rstrip(b"=").decode()
        token = f"{header}.{payload}."
        with pytest.raises(JwtError, match="unexpected alg"):
            verify(token, KEY)

    def test_expired(self):
        token = issue(KEY, sub="a", device_id="d", jti="j", lifetime_seconds=10, now=1000)
        # 1015 > 1010 + 5s leeway
        with pytest.raises(JwtError, match="expired"):
            verify(token, KEY, now=1016)

    def test_within_leeway(self):
        token = issue(KEY, sub="a", device_id="d", jti="j", lifetime_seconds=10, now=1000)
        # Within 5s leeway after expiry — accepted
        claims = verify(token, KEY, now=1014)
        assert claims.sub == "a"

    def test_wrong_issuer(self):
        # Hand-roll a token with bogus iss
        header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
        body = json.dumps({
            "iss": "evil-corp", "aud": EXPECTED_AUDIENCE,
            "sub": "a", "device_id": "d", "jti": "j",
            "iat": 0, "exp": int(time.time()) + 60,
        }).encode()
        body_b64 = base64.urlsafe_b64encode(body).rstrip(b"=").decode()
        import hashlib
        import hmac as _hmac
        sig = _hmac.new(KEY, f"{header}.{body_b64}".encode(), hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        token = f"{header}.{body_b64}.{sig_b64}"
        with pytest.raises(JwtError, match="iss"):
            verify(token, KEY)

    def test_wrong_audience(self):
        header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
        body = json.dumps({
            "iss": EXPECTED_ISSUER, "aud": "wrong",
            "sub": "a", "device_id": "d", "jti": "j",
            "iat": 0, "exp": int(time.time()) + 60,
        }).encode()
        body_b64 = base64.urlsafe_b64encode(body).rstrip(b"=").decode()
        import hashlib
        import hmac as _hmac
        sig = _hmac.new(KEY, f"{header}.{body_b64}".encode(), hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        with pytest.raises(JwtError, match="aud"):
            verify(f"{header}.{body_b64}.{sig_b64}", KEY)

    def test_missing_claims(self):
        # Build a properly-signed token with an empty body
        header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
        body = json.dumps({
            "iss": EXPECTED_ISSUER, "aud": EXPECTED_AUDIENCE,
            # missing sub, device_id, jti, exp
        }).encode()
        body_b64 = base64.urlsafe_b64encode(body).rstrip(b"=").decode()
        import hashlib
        import hmac as _hmac
        sig = _hmac.new(KEY, f"{header}.{body_b64}".encode(), hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        with pytest.raises(JwtError, match="missing"):
            verify(f"{header}.{body_b64}.{sig_b64}", KEY)


# ── replay protection ─────────────────────────────────────────────────


class TestJtiSet:
    def test_first_claim_succeeds(self):
        s = JtiSet()
        assert s.claim("j1", int(time.time()) + 60) is True

    def test_replay_rejected(self):
        s = JtiSet()
        future = int(time.time()) + 60
        s.claim("j1", future)
        assert s.claim("j1", future) is False

    def test_distinct_jtis_independent(self):
        s = JtiSet()
        future = int(time.time()) + 60
        assert s.claim("j1", future) is True
        assert s.claim("j2", future) is True
        assert s.claim("j1", future) is False

    def test_gc_drops_expired(self):
        s = JtiSet()
        # exp in the past — should be gc'd on next claim
        s.claim("old", int(time.time()) - 100)
        assert s.size() == 1
        # Trigger GC by claiming a new one
        s.claim("fresh", int(time.time()) + 60)
        assert s.size() == 1  # only "fresh" remains


# ── Env-key loader ────────────────────────────────────────────────────


class TestLoadKeyFromEnv:
    def test_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv(audio_auth.ENV_VAR, raising=False)
        assert load_key_from_env() is None

    def test_empty_returns_none(self, monkeypatch):
        monkeypatch.setenv(audio_auth.ENV_VAR, "")
        assert load_key_from_env() is None

    def test_invalid_b64_returns_none(self, monkeypatch):
        monkeypatch.setenv(audio_auth.ENV_VAR, "not-base64!!!")
        assert load_key_from_env() is None

    def test_too_short_returns_none(self, monkeypatch):
        # 8 raw bytes < the 16-byte minimum
        short_key = base64.b64encode(b"x" * 8).decode()
        monkeypatch.setenv(audio_auth.ENV_VAR, short_key)
        assert load_key_from_env() is None

    def test_valid_key_returned(self, monkeypatch):
        good = base64.b64encode(b"x" * 32).decode()
        monkeypatch.setenv(audio_auth.ENV_VAR, good)
        result = load_key_from_env()
        assert result == b"x" * 32
