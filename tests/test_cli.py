"""CLI tests: `uploader stage` builds a correct, ready bundle."""

from __future__ import annotations

import json
from datetime import datetime as real_datetime
from pathlib import Path

from click.testing import CliRunner

from tests.framework import recorded_test
from uploader.cli import cli

CONFIG_TMPL = """\
home = "{home}"
projects_dir = "{projects}"
settle_seconds = 0

[[backend]]
kind = "local"
inbox = "{inbox}"
"""

PROJECT = """\
cadence = "2h"
tags = ["demo"]

[title]
templates = ["demo {seed}"]
"""


class _FixedDatetime:
    @classmethod
    def now(cls):
        return real_datetime(2026, 6, 17, 12, 0, 0)


def _config(tmp_path: Path) -> Path:
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "demo.toml").write_text(PROJECT)
    (tmp_path / "inbox").mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG_TMPL.format(home=tmp_path / "home", projects=tmp_path / "projects", inbox=tmp_path / "inbox"))
    return cfg


@recorded_test("stage_builds_bundle")
def test_stage(tf, tmp_path, monkeypatch):
    for env in ("UPLOADER_HOME", "UPLOADER_PROJECTS_DIR", "UPLOADER_CREDENTIALS_DIR"):
        monkeypatch.delenv(env, raising=False)
    cfg = _config(tmp_path)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")

    result = CliRunner().invoke(
        cli,
        ["--config", str(cfg), "stage", str(video), "--project", "demo", "-V", "seed=36", "-M", "spec=line", "--privacy", "private"],
    )
    tf.expect(result.exit_code == 0, f"stage exits 0 (output: {result.output})")

    inbox = tmp_path / "inbox"
    bundles = [d for d in inbox.iterdir() if d.is_dir()]
    tf.expect(len(bundles) == 1, f"one bundle created (got {len(bundles)})")
    d = bundles[0]
    tf.expect((d / "clip.mp4").exists(), "video present in bundle")
    sidecar = json.loads((d / "upload.json").read_text())
    tf.log(f"sidecar: {sidecar}")
    tf.expect(sidecar["project"] == "demo", "project set")
    tf.expect(sidecar["values"] == {"seed": 36}, "value coerced to int and stored")
    tf.expect(sidecar["meta"] == {"spec": "line"}, "meta stored separately")
    tf.expect(sidecar["overrides"]["privacy"] == "private", "privacy override stored")
    tf.expect("created_at" in sidecar, "created_at sentinel present")


@recorded_test("stage_bundle_ids_unique_on_collision")
def test_stage_bundle_ids_unique_on_collision(tf, tmp_path, monkeypatch):
    for env in ("UPLOADER_HOME", "UPLOADER_PROJECTS_DIR", "UPLOADER_CREDENTIALS_DIR"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr("uploader.cli.datetime", _FixedDatetime)
    cfg = _config(tmp_path)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")

    runner = CliRunner()
    args = ["--config", str(cfg), "stage", str(video), "--project", "demo"]
    first = runner.invoke(cli, args)
    second = runner.invoke(cli, args)
    tf.expect(first.exit_code == 0, f"first stage exits 0 (output: {first.output})")
    tf.expect(second.exit_code == 0, f"second stage exits 0 (output: {second.output})")

    bundles = sorted(d.name for d in (tmp_path / "inbox").iterdir() if d.is_dir())
    tf.log(f"bundle dirs: {bundles}")
    tf.expect(len(bundles) == 2, f"two invocations create two bundle dirs (got {bundles})")
    tf.expect(len(set(bundles)) == 2, "bundle ids are unique")


@recorded_test("stage_rejects_unknown_project")
def test_stage_unknown_project(tf, tmp_path, monkeypatch):
    for env in ("UPLOADER_HOME", "UPLOADER_PROJECTS_DIR", "UPLOADER_CREDENTIALS_DIR"):
        monkeypatch.delenv(env, raising=False)
    cfg = _config(tmp_path)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    result = CliRunner().invoke(cli, ["--config", str(cfg), "stage", str(video), "--project", "nope"])
    tf.expect(result.exit_code != 0, "staging an unknown project fails before any file is written")
    tf.expect(not any((tmp_path / "inbox").iterdir()), "nothing staged on failure")
