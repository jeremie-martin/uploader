"""The metadata engine - the one universal mechanism the uploader owns.

This is the clean replacement for the title/description/tag "pools" that were
re-implemented (messily) in every project: ``double-pendulum``'s
``templates.py`` (``format_count`` / ``generate_title`` / ``get_count_tags``),
``lpt2d``'s ``titles.py``, ``motiontwin``'s ``lib/titles.mjs``.

The split is: the *content* is project-specific (lives in ``projects/<name>.toml``)
but the *mechanism* is universal and lives here:

* ``render_template`` - ``{key}``/``{key|filter}``/``{key|f1|f2}`` substitution with an
  extensible filter registry (``human`` turns ``1000000`` -> ``"1 Million"``).
* ``pick`` - choose a title/description template, render with per-video values, append
  a random sample of hashtags, assemble tags (base + value-conditioned ``tags_when``),
  apply sidecar overrides last, then validate/clamp to YouTube's limits.

Everything is driven by a ``random.Random`` passed in, so callers can seed by bundle id
for reproducibility (as ``double-pendulum`` did).
"""

from __future__ import annotations

import ast
import operator
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from uploader.config import ProjectConfig

# YouTube hard limits (https://developers.google.com/youtube/v3/docs/videos).
MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 5000
MAX_TAGS_CHARS = 500  # sum of len(tag), roughly; YouTube also counts quoting for spaces

CATEGORY_FILM_ANIMATION = "1"


class TemplateError(ValueError):
    """A template referenced a value that was not provided, or a filter is unknown."""


# --------------------------------------------------------------------------- #
# Filter registry - extend by registering more callables.
# --------------------------------------------------------------------------- #

Filter = Callable[[Any], str]
_FILTERS: dict[str, Filter] = {}


def register_filter(name: str) -> Callable[[Filter], Filter]:
    """Decorator to register a template filter under ``name``."""

    def deco(fn: Filter) -> Filter:
        _FILTERS[name] = fn
        return fn

    return deco


@register_filter("human")
def _f_human(v: Any) -> str:
    """Format a number compactly for titles: 1000000 -> '1 Million', 100000 -> '100K'."""
    n = float(v)
    if abs(n) >= 1_000_000:
        m = n / 1_000_000
        return f"{int(m)} Million" if m == int(m) else f"{m:.1f} Million"
    if abs(n) >= 1_000:
        k = n / 1_000
        return f"{int(k)}K" if k == int(k) else f"{k:.1f}K"
    return str(int(n)) if n == int(n) else str(n)


@register_filter("comma")
def _f_comma(v: Any) -> str:
    """Thousands-separated: 1000000 -> '1,000,000'."""
    return f"{int(v):,}"


@register_filter("upper")
def _f_upper(v: Any) -> str:
    return str(v).upper()


@register_filter("lower")
def _f_lower(v: Any) -> str:
    return str(v).lower()


@register_filter("title")
def _f_title(v: Any) -> str:
    return str(v).title()


@register_filter("int")
def _f_int(v: Any) -> str:
    return str(int(float(v)))


def apply_filter(name: str, value: Any) -> str:
    fn = _FILTERS.get(name)
    if fn is None:
        raise TemplateError(f"unknown template filter {name!r} (known: {sorted(_FILTERS)})")
    return fn(value)


# --------------------------------------------------------------------------- #
# Template rendering: {key}, {key|filter}, {key|f1|f2}
# --------------------------------------------------------------------------- #

_TOKEN = re.compile(r"\{([a-zA-Z0-9_]+)((?:\|[a-zA-Z0-9_]+)*)\}")


def render_template(text: str, values: dict[str, Any]) -> str:
    """Render ``{key}`` / ``{key|filter}`` / ``{key|f1|f2}`` tokens against ``values``.

    Raises :class:`TemplateError` if a referenced key is missing - a malformed
    bundle/config should fail loudly rather than publish a literal ``{count}``.
    """

    def sub(m) -> str:
        key = m.group(1)
        if key not in values:
            raise TemplateError(f"template references missing value {key!r}")
        out: Any = values[key]
        filters = [f for f in m.group(2).split("|") if f]
        for fname in filters:
            out = apply_filter(fname, out)
        return str(out)

    return _TOKEN.sub(sub, text)


# --------------------------------------------------------------------------- #
# Safe condition evaluation for tags_when (no eval()).
# --------------------------------------------------------------------------- #

_CMP_OPS = {
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


def eval_condition(expr: str, values: dict[str, Any]) -> bool:
    """Safely evaluate a condition like ``count >= 1000000 and preset == "x4"``.

    Supports comparisons (``> >= < <= == != in not in``), boolean ``and``/``or``/``not``,
    bare names (resolved from ``values``), numbers, strings, and list/tuple literals.
    A name that is absent, or a comparison that raises (e.g. ``None >= 1``), evaluates
    to ``False`` rather than blowing up the whole upload.
    """
    try:
        node = ast.parse(expr, mode="eval").body
    except SyntaxError as e:
        raise TemplateError(f"invalid condition {expr!r}: {e}") from e

    def ev(n: ast.AST) -> Any:
        if isinstance(n, ast.BoolOp):
            vals = [ev(v) for v in n.values]
            if isinstance(n.op, ast.And):
                return all(vals)
            return any(vals)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            return not ev(n.operand)
        if isinstance(n, ast.Compare):
            left = ev(n.left)
            for op, comp in zip(n.ops, n.comparators, strict=False):
                fn = _CMP_OPS.get(type(op))
                if fn is None:
                    raise TemplateError(f"unsupported operator in {expr!r}")
                try:
                    if not fn(left, ev(comp)):
                        return False
                except TypeError:
                    return False
                left = ev(comp)
            return True
        if isinstance(n, ast.Name):
            return values.get(n.id)
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, (ast.List, ast.Tuple)):
            return [ev(e) for e in n.elts]
        raise TemplateError(f"disallowed expression in {expr!r}: {ast.dump(n)}")

    return bool(ev(node))


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


@dataclass
class ResolvedMetadata:
    """The final, upload-ready metadata for one video."""

    title: str
    description: str
    tags: list[str] = field(default_factory=list)
    playlist: str | None = None
    privacy: str = "private"
    category_id: str = CATEGORY_FILM_ANIMATION


def _dedup(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in seq:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _clamp_tags(tags: list[str], limit: int = MAX_TAGS_CHARS) -> list[str]:
    """Drop trailing tags until the total character budget fits YouTube's limit."""
    out: list[str] = []
    total = 0
    for t in tags:
        # +1 approximates the separator/quote overhead YouTube applies.
        cost = len(t) + 1
        if total + cost > limit:
            logger.warning("tags exceed {} chars; dropping {!r} and the rest", limit, t)
            break
        out.append(t)
        total += cost
    return out


def pick(
    cfg: ProjectConfig,
    values: dict[str, Any],
    *,
    rng: random.Random | None = None,
    overrides: dict[str, Any] | None = None,
) -> ResolvedMetadata:
    """Resolve final metadata for one video from a project's pool + per-video values.

    Parameters
    ----------
    cfg
        The project's pool config (templates, hashtags, tags, routing).
    values
        Per-video data from the bundle sidecar (e.g. ``{"count": 1_000_000}``).
    rng
        Source of randomness; pass a seeded ``random.Random`` for reproducibility.
    overrides
        Sidecar ``overrides`` block - any of ``title``/``description``/``tags``/
        ``playlist``/``privacy`` here fully replaces the engine's choice.
    """
    rng = rng or random.Random()
    overrides = overrides or {}

    # --- title -----------------------------------------------------------
    if cfg.title.templates:
        title = render_template(rng.choice(cfg.title.templates), values)
    else:
        title = ""
    hashtags = cfg.title.hashtags
    if hashtags:
        lo, hi = cfg.title.hashtag_count
        n = min(rng.randint(lo, hi), len(hashtags))
        if n > 0:
            chosen = rng.sample(hashtags, n)
            title = f"{title} {' '.join('#' + h for h in chosen)}".strip()

    # --- description -----------------------------------------------------
    description = render_template(rng.choice(cfg.description.templates), values) if cfg.description.templates else ""

    # --- tags ------------------------------------------------------------
    tags = [render_template(t, values) for t in cfg.tags]
    for rule in cfg.tags_when:
        if eval_condition(rule.when, values):
            tags.extend(render_template(t, values) for t in rule.add)
    tags = _dedup(tags)

    # --- overrides (win over everything) ---------------------------------
    if "title" in overrides:
        title = str(overrides["title"])
    if "description" in overrides:
        description = str(overrides["description"])
    if "tags" in overrides and overrides["tags"] is not None:
        tags = _dedup([str(t) for t in overrides["tags"]])
    playlist = overrides.get("playlist", cfg.playlist)
    privacy = overrides.get("privacy", cfg.privacy)
    category_id = overrides.get("category_id", cfg.category_id)

    # --- validate / clamp ------------------------------------------------
    title = title.replace("<", "").replace(">", "").strip()  # YouTube rejects < > in titles
    if len(title) > MAX_TITLE_CHARS:
        logger.warning("title {} chars > {}; truncating", len(title), MAX_TITLE_CHARS)
        title = title[:MAX_TITLE_CHARS].rstrip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        logger.warning("description {} chars > {}; truncating", len(description), MAX_DESCRIPTION_CHARS)
        description = description[:MAX_DESCRIPTION_CHARS]
    tags = _clamp_tags(tags)

    return ResolvedMetadata(
        title=title,
        description=description,
        tags=tags,
        playlist=playlist,
        privacy=privacy,
        category_id=category_id,
    )


__all__ = [
    "CATEGORY_FILM_ANIMATION",
    "MAX_DESCRIPTION_CHARS",
    "MAX_TAGS_CHARS",
    "MAX_TITLE_CHARS",
    "ResolvedMetadata",
    "TemplateError",
    "apply_filter",
    "eval_condition",
    "pick",
    "register_filter",
    "render_template",
]
