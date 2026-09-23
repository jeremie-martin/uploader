"""Tests for the no-network token inspector used by `uploader status`."""

from __future__ import annotations

import json
import pickle

import httplib2
import pytest
from googleapiclient.errors import HttpError

from tests.framework import recorded_test
from uploader import youtube


class _RefreshFailCreds:
    valid = False
    expired = True
    refresh_token = "refresh"

    def refresh(self, _request):
        raise RuntimeError("refresh rejected")


@recorded_test("token_inspect_missing")
def test_inspect_missing_token(tf, tmp_path):
    t = youtube.inspect_token(tmp_path)
    tf.expect(t == {"present": False}, f"missing token reported as absent (got {t})")


@recorded_test("token_inspect_unreadable")
def test_inspect_unreadable_token(tf, tmp_path):
    (tmp_path / "token.pickle").write_bytes(b"not a pickle")
    t = youtube.inspect_token(tmp_path)
    tf.expect(t["present"] is True, "present")
    tf.expect("error" in t, f"unreadable token reported as error (got {t})")


@recorded_test("token_load_unreadable_auth_error")
def test_load_unreadable_token_is_auth_error(tf, tmp_path):
    (tmp_path / "token.pickle").write_bytes(b"not a pickle")
    with pytest.raises(youtube.AuthError):
        youtube.load_or_refresh(tmp_path)
    tf.expect(True, "unreadable token load is reported as AuthError")


@recorded_test("token_refresh_failure_auth_error")
def test_refresh_failure_is_auth_error(tf, tmp_path):
    (tmp_path / "token.pickle").write_bytes(pickle.dumps(_RefreshFailCreds()))
    with pytest.raises(youtube.AuthError):
        youtube.load_or_refresh(tmp_path)
    tf.expect(True, "refresh failure is reported as AuthError")


def _http_error(status: int, reason: str | None, message: str = "boom") -> HttpError:
    """Build an HttpError shaped like a real googleapiclient one."""
    errors = [] if reason is None else [{"message": message, "domain": "youtube.video", "reason": reason}]
    content = json.dumps({"error": {"code": status, "message": message, "errors": errors}}).encode()
    return HttpError(httplib2.Response({"status": status}), content)


@recorded_test("rate_limit_upload_limit_exceeded")
def test_upload_limit_exceeded_is_rate_limit(tf):
    """The per-channel 24h upload cap arrives as HTTP 400 - it must NOT be terminal.

    Regression: keying off the status code classified this as a terminal UploadError,
    so every throttled tick permanently marked a good bundle `failed`.
    """
    e = _http_error(400, "uploadLimitExceeded", "The user has exceeded the number of videos they may upload.")
    tf.expect(youtube._is_rate_limit(e), "400/uploadLimitExceeded must be treated as a rate limit")


@recorded_test("rate_limit_classification")
def test_rate_limit_classification(tf):
    """Throttling reasons are retryable at any status; real errors stay terminal."""
    for status, reason in [(403, "quotaExceeded"), (403, "rateLimitExceeded"), (400, "uploadLimitExceeded")]:
        tf.expect(youtube._is_rate_limit(_http_error(status, reason)), f"{status}/{reason} should be a rate limit")

    tf.expect(youtube._is_rate_limit(_http_error(429, None)), "429 is a rate limit even with no reason")

    for status, reason in [(400, "invalidTitle"), (403, "forbidden"), (404, "videoNotFound")]:
        tf.expect(not youtube._is_rate_limit(_http_error(status, reason)), f"{status}/{reason} must stay terminal")


@recorded_test("rate_limit_malformed_body")
def test_rate_limit_malformed_body(tf):
    """A body that isn't the expected JSON shape must not raise - just not a rate limit."""
    for content in [b"not json", b"null", b"[]", b'{"error": "a string"}', b"\xff\xfe"]:
        e = HttpError(httplib2.Response({"status": 400}), content)
        try:
            got = youtube._is_rate_limit(e)
        except Exception as exc:  # noqa: BLE001
            tf.expect(False, f"_is_rate_limit raised on {content!r}: {exc}")
            continue
        tf.expect(got is False, f"{content!r} should not be a rate limit")
