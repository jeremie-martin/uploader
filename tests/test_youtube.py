"""Tests for the no-network token inspector used by `uploader status`."""

from __future__ import annotations

import pickle

import pytest

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
