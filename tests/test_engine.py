"""Engine tests: template filters, rendering, tags_when, hashtag sampling, clamping."""

from __future__ import annotations

import random

import pytest

from tests.framework import recorded_test
from uploader.config import DescriptionSpec, ProjectConfig, TagRule, TitleSpec
from uploader.engine import (
    MAX_TITLE_CHARS,
    ResolvedMetadata,
    TemplateError,
    apply_filter,
    eval_condition,
    pick,
    render_template,
)


@recorded_test("engine_filters")
def test_filters(tf):
    tf.log("Checking the human/comma filters that replace double-pendulum's format_count")
    tf.expect(apply_filter("human", 1_000_000) == "1 Million", "1M -> '1 Million'")
    tf.expect(apply_filter("human", 1_500_000) == "1.5 Million", "1.5M -> '1.5 Million'")
    tf.expect(apply_filter("human", 100_000) == "100K", "100k -> '100K'")
    tf.expect(apply_filter("human", 500) == "500", "small stays plain")
    tf.expect(apply_filter("comma", 1_000_000) == "1,000,000", "comma groups thousands")
    tf.expect(apply_filter("upper", "chaos") == "CHAOS", "upper")


@recorded_test("engine_render")
def test_render_template(tf):
    tf.expect(
        render_template("{count|human} pendulums", {"count": 1_000_000}) == "1 Million pendulums",
        "single filter renders",
    )
    tf.expect(
        render_template("{count|comma} ({count|human})", {"count": 2_000_000}) == "2,000,000 (2 Million)",
        "same key used twice with different filters",
    )
    tf.expect(render_template("no tokens here", {}) == "no tokens here", "plain text passes through")
    tf.log("A missing value must fail loudly rather than publish a literal brace")
    with pytest.raises(TemplateError):
        render_template("{count}", {})


@recorded_test("engine_conditions")
def test_eval_condition(tf):
    tf.expect(eval_condition("count >= 1000000", {"count": 1_000_000}) is True, ">= boundary")
    tf.expect(eval_condition("count >= 1000000", {"count": 999_999}) is False, "below boundary")
    tf.expect(eval_condition('preset == "x4"', {"preset": "x4"}) is True, "string eq")
    tf.expect(
        eval_condition("count > 1000 and preset == \"x4\"", {"count": 5000, "preset": "x4"}) is True,
        "and combination",
    )
    tf.log("A missing name must degrade to False, not raise")
    tf.expect(eval_condition("count >= 1000000", {}) is False, "missing name -> False")


@recorded_test("engine_pick_determinism")
def test_pick_is_deterministic_with_seed(tf):
    cfg = ProjectConfig(
        name="demo",
        playlist="PLxyz",
        privacy="public",
        cadence_seconds=7200,
        title=TitleSpec(
            templates=["{count|human} double pendulums", "Chaos visualized"],
            hashtags=["chaos", "physics", "satisfying"],
            hashtag_count=(1, 2),
        ),
        description=DescriptionSpec(templates=["{count|human} identical starts."]),
        tags=["double pendulum", "{count|comma} sims"],
        tags_when=[TagRule(when="count >= 1000000", add=["million pendulums"])],
    )
    values = {"count": 1_000_000}

    a = pick(cfg, values, rng=random.Random(42))
    b = pick(cfg, values, rng=random.Random(42))
    tf.expect(a == b, f"same seed -> identical result ({a.title!r} vs {b.title!r})")
    tf.expect(isinstance(a, ResolvedMetadata), "returns ResolvedMetadata")
    tf.expect(a.playlist == "PLxyz", "playlist routed from cfg")
    tf.expect(a.privacy == "public", "privacy from cfg")
    tf.log(f"title={a.title!r} tags={a.tags}")
    tf.expect("million pendulums" in a.tags, "tags_when fired for >=1M")
    tf.expect("1,000,000 sims" in a.tags, "tag template rendered")


@recorded_test("engine_overrides_and_clamp")
def test_overrides_and_clamp(tf):
    cfg = ProjectConfig(
        name="demo",
        title=TitleSpec(templates=["original"]),
        description=DescriptionSpec(templates=["orig desc"]),
        tags=["a", "b"],
    )
    out = pick(
        cfg,
        {},
        rng=random.Random(0),
        overrides={"title": "Custom Title", "tags": ["only", "these"], "privacy": "unlisted"},
    )
    tf.expect(out.title == "Custom Title", "title override wins")
    tf.expect(out.tags == ["only", "these"], "tags override wins")
    tf.expect(out.privacy == "unlisted", "privacy override wins")

    tf.log("Title over 100 chars must be truncated")
    long_cfg = ProjectConfig(name="demo", title=TitleSpec(templates=["x" * 250]))
    out2 = pick(long_cfg, {}, rng=random.Random(0))
    tf.expect(len(out2.title) <= MAX_TITLE_CHARS, f"title clamped to {len(out2.title)}")
