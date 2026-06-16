"""Integration tests for the scheduler tick: cadence, crash-recovery, dedup, dry-run.

The YouTube call is stubbed, so these exercise selection + crash-safe commit logic
end-to-end through a real LocalQueue + real State, without touching the network.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.framework import recorded_test
from uploader import tick, youtube
from uploader.state import State

CONFIG_TMPL = """\
home = "{home}"
credentials_dir = "{creds}"
projects_dir = "{projects}"
settle_seconds = 0

[[backend]]
kind = "local"
inbox = "{inbox}"
"""

PROJECT_TMPL = """\
cadence = "{cadence}"
privacy = "public"
playlist = "PL_{name}"
tags = ["{name}"]

[title]
templates = ["{name} video"]
"""


def _setup(tmp_path: Path, projects: dict[str, str], monkeypatch) -> Path:
    for env in ("UPLOADER_HOME", "UPLOADER_PROJECTS_DIR", "UPLOADER_CREDENTIALS_DIR"):
        monkeypatch.delenv(env, raising=False)
    home = tmp_path / "home"
    creds = tmp_path / "creds"
    projects_dir = tmp_path / "projects"
    inbox = tmp_path / "inbox"
    for p in (home, creds, projects_dir, inbox):
        p.mkdir(parents=True, exist_ok=True)
    for name, cadence in projects.items():
        (projects_dir / f"{name}.toml").write_text(PROJECT_TMPL.format(name=name, cadence=cadence))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        CONFIG_TMPL.format(home=home, creds=creds, projects=projects_dir, inbox=inbox)
    )
    return config_path


def _make_bundle(inbox: Path, bundle_id: str, project: str, *, created_at: str, marker: dict | None = None) -> Path:
    d = inbox / bundle_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "video.mp4").write_bytes(b"fake-video-bytes")
    (d / "upload.json").write_text(json.dumps({"project": project, "created_at": created_at}))
    if marker is not None:
        (d / "uploaded").write_text(json.dumps(marker))
    return d


class _Recorder:
    def __init__(self):
        self.calls: list[dict] = []
        self.next_id = iter(f"VID{i}" for i in range(100))

    def upload(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.next_id)


def _patch_youtube(monkeypatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(youtube, "load_or_refresh", lambda _dir: object())
    monkeypatch.setattr(youtube, "upload", rec.upload)
    return rec


@recorded_test("tick_uploads_one_and_records")
def test_single_upload(tf, tmp_path, monkeypatch):
    cfg = _setup(tmp_path, {"alpha": "1h"}, monkeypatch)
    rec = _patch_youtube(monkeypatch)
    inbox = tmp_path / "inbox"
    _make_bundle(inbox, "alpha-001", "alpha", created_at="2026-06-16T10:00:00Z")

    code = tick.run_tick(cfg)
    tf.expect(code == tick.EXIT_OK, f"tick returns OK (got {code})")
    tf.expect(len(rec.calls) == 1, f"uploaded exactly one (got {len(rec.calls)})")
    tf.expect(rec.calls[0]["title"] == "alpha video", "title resolved from project pool")
    tf.expect(rec.calls[0]["playlist_id"] == "PL_alpha", "playlist routed")

    tf.log("Bundle should be removed and recorded in the ledger")
    tf.expect(not (inbox / "alpha-001").exists(), "bundle dir removed after upload")
    uploads = State(tmp_path / "home").uploads()
    tf.expect(len(uploads) == 1 and uploads[0]["youtube_id"] == "VID0", "ledger has the upload")


@recorded_test("tick_per_project_cadence")
def test_cadence_round_robin(tf, tmp_path, monkeypatch):
    cfg = _setup(tmp_path, {"alpha": "1h", "beta": "1h"}, monkeypatch)
    rec = _patch_youtube(monkeypatch)
    inbox = tmp_path / "inbox"
    # Both of alpha's bundles are OLDER than beta's, so without throttling _select_due
    # would pick alpha twice. The test only passes if alpha is throttled after its first
    # upload, forcing beta on the second tick.
    _make_bundle(inbox, "alpha-001", "alpha", created_at="2026-06-16T10:00:00Z")
    _make_bundle(inbox, "alpha-002", "alpha", created_at="2026-06-16T11:00:00Z")
    _make_bundle(inbox, "beta-001", "beta", created_at="2026-06-16T12:00:00Z")

    tick.run_tick(cfg)
    tick.run_tick(cfg)

    projects_uploaded = [c["title"].split()[0] for c in rec.calls]
    tf.log(f"upload order: {projects_uploaded}")
    tf.expect(len(rec.calls) == 2, f"two ticks -> two uploads (got {len(rec.calls)})")
    tf.expect(
        projects_uploaded == ["alpha", "beta"],
        "alpha (oldest) first; alpha then throttled so beta goes despite alpha-002 being older",
    )


@recorded_test("cadence_clock_is_timezone_correct")
def test_cadence_clock_timezone_robust(tf, tmp_path, monkeypatch):
    # Directly guard the cadence math against the timezone trap: immediately after an
    # upload, a project on a 1h cadence must be throttled for ~1h regardless of host TZ.
    import time as _time

    from uploader.state import State

    monkeypatch.delenv("UPLOADER_HOME", raising=False)
    state = State(tmp_path / "home")
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)

    before = _time.time()
    state.touch_project("alpha")
    remaining = state.seconds_until_due("alpha", 3600, now=before)
    tf.log(f"seconds_until_due right after upload: {remaining}")
    tf.expect(3500 <= remaining <= 3600, f"a just-uploaded project is throttled ~1h, not {remaining}s")


@recorded_test("tick_crash_recovery_no_reupload")
def test_resumed_bundle_is_finalized_not_reuploaded(tf, tmp_path, monkeypatch):
    cfg = _setup(tmp_path, {"alpha": "0s"}, monkeypatch)
    rec = _patch_youtube(monkeypatch)
    inbox = tmp_path / "inbox"
    # Simulate a crash after upload but before cleanup: marker present, never ledgered.
    marker = {"youtube_id": "VIDX", "youtube_url": "https://youtu.be/VIDX", "project": "alpha"}
    _make_bundle(inbox, "alpha-crashed", "alpha", created_at="2026-06-16T10:00:00Z", marker=marker)

    code = tick.run_tick(cfg)
    tf.expect(code == tick.EXIT_OK, "tick OK")
    tf.expect(len(rec.calls) == 0, "resumed bundle must NOT be re-uploaded")
    tf.expect(not (inbox / "alpha-crashed").exists(), "resumed bundle cleaned up")
    uploads = State(tmp_path / "home").uploads()
    tf.expect(len(uploads) == 1 and uploads[0]["youtube_id"] == "VIDX", "marker recovered into ledger")


@recorded_test("tick_dedup_on_resume")
def test_resume_dedup(tf, tmp_path, monkeypatch):
    cfg = _setup(tmp_path, {"alpha": "0s"}, monkeypatch)
    _patch_youtube(monkeypatch)
    inbox = tmp_path / "inbox"
    state = State(tmp_path / "home")
    # Ledger already has the id (crash happened AFTER ledger, before remove).
    state.record_upload({"youtube_id": "VIDDUP", "project": "alpha"})
    marker = {"youtube_id": "VIDDUP", "project": "alpha"}
    _make_bundle(inbox, "alpha-dup", "alpha", created_at="2026-06-16T10:00:00Z", marker=marker)

    tick.run_tick(cfg)
    uploads = State(tmp_path / "home").uploads()
    tf.expect(len(uploads) == 1, f"no duplicate ledger entry (got {len(uploads)})")


@recorded_test("tick_dry_run_keeps_bundle")
def test_dry_run(tf, tmp_path, monkeypatch):
    cfg = _setup(tmp_path, {"alpha": "1h"}, monkeypatch)
    rec = _patch_youtube(monkeypatch)
    inbox = tmp_path / "inbox"
    _make_bundle(inbox, "alpha-001", "alpha", created_at="2026-06-16T10:00:00Z")

    code = tick.run_tick(cfg, dry_run=True)
    tf.expect(code == tick.EXIT_OK, "dry-run OK")
    tf.expect(len(rec.calls) == 0, "dry-run does not upload")
    tf.expect((inbox / "alpha-001").exists(), "dry-run keeps the bundle")
