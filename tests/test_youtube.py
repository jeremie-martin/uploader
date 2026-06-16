"""Tests for the no-network token inspector used by `uploader status`."""

from __future__ import annotations

from tests.framework import recorded_test
from uploader import youtube


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
