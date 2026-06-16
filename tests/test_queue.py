"""Queue backend tests: LocalQueue settle/sentinel rules + ObjectStoreQueue via moto."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.framework import recorded_test
from uploader.config import BackendSpec
from uploader.queue import BackendConfigError, build_backends
from uploader.queue.base import MARKER_FAILED
from uploader.queue.local import LocalQueue


@recorded_test("backend_option_validation")
def test_build_backends_validation(tf):
    # Missing required option -> clear error, not a raw KeyError that crashes the tick.
    with pytest.raises(BackendConfigError, match="missing required option"):
        build_backends([BackendSpec(kind="local", options={})])
    with pytest.raises(BackendConfigError, match="missing required option"):
        build_backends([BackendSpec(kind="objectstore", options={"prefix": "x"})])
    # Unknown option (typo) -> clear error naming the bad key.
    with pytest.raises(BackendConfigError, match="unknown option"):
        build_backends([BackendSpec(kind="objectstore", options={"bucket": "b", "buckeet": "typo"})])
    with pytest.raises(BackendConfigError, match="unknown queue backend kind"):
        build_backends([BackendSpec(kind="nope", options={})])
    # A valid local spec builds.
    built = build_backends([BackendSpec(kind="local", options={"inbox": "/tmp/x"})])
    tf.expect(len(built) == 1, "valid local backend builds")


def _write_bundle(inbox: Path, name: str, project: str | None = "demo") -> Path:
    d = inbox / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "video.mp4").write_bytes(b"vid")
    sidecar = {"project": project} if project else {}
    (d / "upload.json").write_text(json.dumps(sidecar))
    return d


@recorded_test("local_queue_rules")
def test_local_queue_listing(tf, tmp_path):
    inbox = tmp_path / "inbox"
    q = LocalQueue(inbox, settle_seconds=0)

    _write_bundle(inbox, "ready-001")
    # dot-prefixed = in-progress staging, must be ignored
    _write_bundle(inbox, ".staging-002")
    # no sidecar sentinel yet = still assembling
    half = inbox / "half-003"
    half.mkdir(parents=True)
    (half / "video.mp4").write_bytes(b"vid")
    # failed marker = skipped
    failed = _write_bundle(inbox, "failed-004")
    (failed / MARKER_FAILED).write_text("{}")
    # missing project = skipped
    _write_bundle(inbox, "noproj-005", project=None)

    refs = q.list_ready()
    ids = {r.bundle_id for r in refs}
    tf.log(f"listed: {sorted(ids)}")
    tf.expect(ids == {"ready-001"}, f"only the valid ready bundle is listed (got {sorted(ids)})")


@recorded_test("local_queue_recursive")
def test_local_queue_recursive(tf, tmp_path):
    inbox = tmp_path / "inbox"
    q = LocalQueue(inbox, settle_seconds=0)
    # A bundle nested several levels deep must be found.
    _write_bundle(inbox, "by-project/tiki/2026/run-001")
    # A non-bundle organizational dir (no sidecar) in between is fine.
    (inbox / "by-project" / "empty-so-far").mkdir(parents=True, exist_ok=True)

    refs = q.list_ready()
    ids = {r.bundle_id for r in refs}
    tf.log(f"listed ids: {ids}")
    tf.expect(ids == {"by-project/tiki/2026/run-001"}, f"nested bundle found with relative id (got {ids})")

    ref = refs[0]
    local = q.fetch(ref, tmp_path / "scratch")
    tf.expect(local.video_path.exists(), "fetch resolves the nested video")

    q.remove(ref)
    tf.expect(not (inbox / "by-project" / "tiki").exists(), "remove prunes now-empty parent dirs")
    tf.expect(inbox.exists(), "but never removes the inbox itself")
    tf.expect((inbox / "by-project" / "empty-so-far").exists(), "unrelated empty dir left untouched")


@recorded_test("local_queue_no_descend_into_bundle")
def test_no_nested_bundle_inside_bundle(tf, tmp_path):
    inbox = tmp_path / "inbox"
    q = LocalQueue(inbox, settle_seconds=0)
    outer = _write_bundle(inbox, "outer")
    # A stray subdir with its own sidecar inside a bundle must NOT become a second bundle.
    _write_bundle(inbox / "outer", "extras")  # creates inbox/outer/extras/upload.json
    refs = q.list_ready()
    ids = {r.bundle_id for r in refs}
    tf.expect(ids == {"outer"}, f"scan does not descend into a bundle (got {ids})")
    _ = outer


@recorded_test("local_queue_settle")
def test_local_queue_settle_time(tf, tmp_path):
    inbox = tmp_path / "inbox"
    q = LocalQueue(inbox, settle_seconds=60)
    _write_bundle(inbox, "fresh-001")
    tf.expect(q.list_ready() == [], "a just-written bundle is withheld until it settles")

    # An old-enough sentinel is accepted.
    q2 = LocalQueue(inbox, settle_seconds=0)
    tf.expect(len(q2.list_ready()) == 1, "settle=0 accepts immediately")


@recorded_test("local_queue_fetch_and_markers")
def test_local_fetch_mark_remove(tf, tmp_path):
    inbox = tmp_path / "inbox"
    q = LocalQueue(inbox, settle_seconds=0)
    _write_bundle(inbox, "b-001")
    ref = q.list_ready()[0]

    local = q.fetch(ref, tmp_path / "scratch")
    tf.expect(local.video_path.name == "video.mp4", "fetch resolves the lone video file")
    tf.expect(local.video_path.exists(), "video path exists (local fetch is in-place)")

    q.mark_uploaded(ref, {"youtube_id": "VID9"})
    marker = json.loads((inbox / "b-001" / "uploaded").read_text())
    tf.expect(marker["youtube_id"] == "VID9", "uploaded marker persisted")

    # A bundle with a marker is reported as resumed.
    resumed = q.list_ready()[0]
    tf.expect(resumed.is_resumed, "marker -> is_resumed")

    q.remove(ref)
    tf.expect(not (inbox / "b-001").exists(), "remove deletes the bundle dir")


# --------------------------------------------------------------------------- #
# Object store (moto)
# --------------------------------------------------------------------------- #

moto = pytest.importorskip("moto")


@recorded_test("objectstore_roundtrip")
def test_objectstore_roundtrip(tf, tmp_path, monkeypatch):
    from moto import mock_aws

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    with mock_aws():
        import boto3

        from uploader.queue.objectstore import ObjectStoreQueue

        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="vids")
        # video first, sentinel last (as a generator would)
        s3.put_object(Bucket="vids", Key="inbox/run-1/video.mp4", Body=b"the-video")
        s3.put_object(Bucket="vids", Key="inbox/run-1/upload.json", Body=json.dumps({"project": "demo"}).encode())

        # A nested bundle (deeper key prefix) must also be discovered.
        s3.put_object(Bucket="vids", Key="inbox/by-day/2026/run-2/video.mp4", Body=b"vid2")
        s3.put_object(Bucket="vids", Key="inbox/by-day/2026/run-2/upload.json", Body=json.dumps({"project": "demo"}).encode())

        q = ObjectStoreQueue(bucket="vids", prefix="inbox", settle_seconds=0)
        refs = q.list_ready()
        ids = {r.bundle_id for r in refs}
        tf.expect(ids == {"run-1", "by-day/2026/run-2"}, f"flat + nested bundles found (got {ids})")
        ref = next(r for r in refs if r.bundle_id == "run-1")
        tf.expect(ref.project == "demo", "project read from sidecar object")

        local = q.fetch(ref, tmp_path / "dl")
        tf.expect(local.video_path.read_bytes() == b"the-video", "video downloaded one-at-a-time")

        q.mark_uploaded(ref, {"youtube_id": "VIDOBJ"})
        marker = json.loads(s3.get_object(Bucket="vids", Key="inbox/run-1/uploaded")["Body"].read())
        tf.expect(marker["youtube_id"] == "VIDOBJ", "marker object written")
        run1 = next(r for r in q.list_ready() if r.bundle_id == "run-1")
        tf.expect(run1.is_resumed, "marker -> resumed on next list")

        q.remove(ref)
        remaining = s3.list_objects_v2(Bucket="vids", Prefix="inbox/run-1/").get("Contents", [])
        tf.expect(remaining == [], "remove deletes all bundle objects")
