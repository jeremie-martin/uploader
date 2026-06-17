"""Config loader tests: duration parsing, the misplaced-key guard, real project configs."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.framework import recorded_test
from uploader.config import load_global_config, load_project_config, parse_duration, parse_upload_order

REPO_PROJECTS = Path(__file__).resolve().parents[1] / "projects"


@recorded_test("config_durations")
def test_parse_duration(tf):
    tf.expect(parse_duration("2h") == 7200, "2h")
    tf.expect(parse_duration("40m") == 2400, "40m")
    tf.expect(parse_duration("30s") == 30, "30s")
    tf.expect(parse_duration("1d") == 86400, "1d")
    tf.expect(parse_duration(90) == 90, "bare int = seconds")
    tf.expect(parse_duration("") == 0, "empty = 0")


@recorded_test("config_upload_order")
def test_parse_upload_order(tf):
    tf.expect(parse_upload_order("first") == "first", "first")
    tf.expect(parse_upload_order("last") == "last", "last")
    tf.expect(parse_upload_order("random") == "random", "random")
    tf.expect(parse_upload_order("oldest") == "first", "oldest aliases first")
    tf.expect(parse_upload_order("newest") == "last", "newest aliases last")


@recorded_test("config_misplaced_key_guard")
def test_misplaced_tags_raises(tf, tmp_path):
    # `tags` after [description] is the classic TOML trap; must fail loudly with a hint.
    bad = tmp_path / "bad.toml"
    bad.write_text('[description]\ntemplates = ["x"]\ntags = ["a", "b"]\n')
    tf.log("A misplaced top-level key must raise a helpful error, not silently vanish")
    with pytest.raises(ValueError, match="move them above the first"):
        load_project_config(bad)


@recorded_test("config_unknown_key_guard")
def test_typo_key_raises(tf, tmp_path):
    # A typo'd top-level key (e.g. `cadencce`) must fail loudly, not silently disable
    # throttling by falling back to cadence=0.
    bad = tmp_path / "typo.toml"
    bad.write_text('cadencce = "2h"\nprivacy = "public"\n[title]\ntemplates = ["t"]\n')
    with pytest.raises(ValueError, match="unknown key"):
        load_project_config(bad)


@recorded_test("config_hashtag_count_clamped")
def test_reversed_hashtag_count_is_clamped(tf, tmp_path):
    # hi < lo must be normalized at load time so rng.randint can never get an empty range.
    cfg = tmp_path / "rev.toml"
    cfg.write_text('[title]\ntemplates = ["t"]\nhashtags = ["a", "b"]\nhashtag_count = [2, 1]\n')
    pc = load_project_config(cfg)
    lo, hi = pc.title.hashtag_count
    tf.expect(lo <= hi, f"hashtag_count normalized to lo<=hi (got {(lo, hi)})")
    tf.expect(lo >= 0, "lo is non-negative")


@recorded_test("config_expands_local_inbox")
def test_local_backend_inbox_expands_user(tf, tmp_path, monkeypatch):
    for env in ("UPLOADER_HOME", "UPLOADER_PROJECTS_DIR", "UPLOADER_CREDENTIALS_DIR"):
        monkeypatch.delenv(env, raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        """\
home = "~/uploader-home"

[[backend]]
kind = "local"
inbox = "~/.local/share/uploader/inbox"
"""
    )

    loaded = load_global_config(cfg)
    inbox = loaded.backends[0].options["inbox"]
    tf.expect(Path(inbox).is_absolute(), f"local inbox is absolute after expansion (got {inbox!r})")
    tf.expect("~" not in str(inbox), f"local inbox contains no literal '~' (got {inbox!r})")


@recorded_test("config_global_upload_order")
def test_global_upload_order(tf, tmp_path, monkeypatch):
    for env in ("UPLOADER_HOME", "UPLOADER_PROJECTS_DIR", "UPLOADER_CREDENTIALS_DIR"):
        monkeypatch.delenv(env, raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        """\
home = "~/uploader-home"
upload_order = "random"
"""
    )

    loaded = load_global_config(cfg)
    tf.expect(loaded.upload_order == "random", f"upload_order parsed (got {loaded.upload_order!r})")


@recorded_test("config_project_upload_order")
def test_project_upload_order(tf, tmp_path):
    cfg = tmp_path / "project.toml"
    cfg.write_text(
        """\
cadence = "1h"
upload_order = "random"

[title]
templates = ["demo"]
"""
    )

    pc = load_project_config(cfg)
    tf.expect(pc.upload_order == "random", f"project upload_order parsed (got {pc.upload_order!r})")


@recorded_test("config_real_projects_load")
def test_bundled_project_configs(tf):
    for path in sorted(REPO_PROJECTS.glob("*.toml")):
        pc = load_project_config(path)
        tf.expect(len(pc.title.templates) > 0, f"{path.name}: has title templates")
        tf.expect(len(pc.tags) > 0, f"{path.name}: has base tags ({len(pc.tags)})")
        tf.expect(pc.cadence_seconds > 0, f"{path.name}: cadence parsed ({pc.cadence_seconds}s)")
