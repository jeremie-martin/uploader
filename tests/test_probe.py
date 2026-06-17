"""Probe tests: fps parsing and graceful degradation on non-media input."""

from __future__ import annotations

import json
from dataclasses import dataclass

from tests.framework import recorded_test
from uploader.probe import _fps, probe_media


@dataclass
class _Proc:
    returncode: int
    stdout: str


@recorded_test("probe_fps")
def test_fps(tf):
    tf.expect(_fps("30/1") == 30.0, "30/1 -> 30.0")
    tf.expect(_fps("60000/1001") == round(60000 / 1001, 3), "ntsc 59.94")
    tf.expect(_fps(None) is None, "None -> None")
    tf.expect(_fps("bad") is None, "garbage -> None")
    tf.expect(_fps("30/0") is None, "div-by-zero -> None")


@recorded_test("probe_graceful")
def test_probe_non_media(tf, tmp_path):
    # ffprobe on a non-video must degrade to {} (or {} if ffprobe is absent), never raise.
    f = tmp_path / "not_a_video.mp4"
    f.write_bytes(b"definitely not an mp4")
    result = probe_media(f)
    tf.expect(result == {}, f"non-media probes to empty dict (got {result})")


@recorded_test("probe_non_numeric_format_fields")
def test_probe_non_numeric_format_fields(tf, tmp_path, monkeypatch):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x")
    payload = {"format": {"duration": "N/A", "size": "N/A"}, "streams": []}
    monkeypatch.setattr("uploader.probe.shutil.which", lambda _name: "/usr/bin/ffprobe")
    monkeypatch.setattr(
        "uploader.probe.subprocess.run",
        lambda *_args, **_kwargs: _Proc(returncode=0, stdout=json.dumps(payload)),
    )

    result = probe_media(f)
    tf.expect(result == {}, f"non-numeric ffprobe format fields are ignored (got {result})")
