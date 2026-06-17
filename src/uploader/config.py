"""Configuration: global settings + per-project pool configs.

Two layers:

* **Global** (``config.toml`` on the uploader host): where credentials live, which
  queue backends to drain, the shared default privacy/category, and where to find the
  per-project configs. Resolved with env-var overrides so a systemd unit can tweak it.
* **Per-project** (``projects/<name>.toml``): the redesigned "pool" - title/description
  templates, hashtags, tags, value-conditioned ``tags_when`` rules, plus routing
  (playlist) and cadence for that project. This is the *content* each project owns; the
  *mechanism* lives in :mod:`uploader.engine`.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from uploader.engine import CATEGORY_FILM_ANIMATION

DEFAULT_HOME = Path(os.environ.get("UPLOADER_HOME", str(Path.home() / ".local/share/uploader")))


# --------------------------------------------------------------------------- #
# Per-project pool config
# --------------------------------------------------------------------------- #


@dataclass
class TitleSpec:
    templates: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    hashtag_count: tuple[int, int] = (0, 0)


@dataclass
class DescriptionSpec:
    templates: list[str] = field(default_factory=list)


@dataclass
class TagRule:
    when: str
    add: list[str]


@dataclass
class ProjectConfig:
    name: str
    playlist: str | None = None
    privacy: str = "private"
    category_id: str = CATEGORY_FILM_ANIMATION
    cadence_seconds: int = 0
    upload_order: str | None = None
    title: TitleSpec = field(default_factory=TitleSpec)
    description: DescriptionSpec = field(default_factory=DescriptionSpec)
    tags: list[str] = field(default_factory=list)
    tags_when: list[TagRule] = field(default_factory=list)


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str | int | float) -> int:
    """Parse a cadence like '2h', '90m', '30s', '1d', or a bare number (seconds)."""
    if isinstance(value, (int, float)):
        return int(value)
    s = value.strip().lower()
    if not s:
        return 0
    if s[-1] in _DURATION_UNITS:
        return int(float(s[:-1]) * _DURATION_UNITS[s[-1]])
    return int(float(s))


# The full schema. Unknown keys are rejected so a typo (e.g. `cadencce`, `privacyy`) fails
# loudly instead of silently falling back to a default (a typo'd `cadence` would otherwise
# disable that project's throttling). Top-level keys that appear under a sub-table are
# almost always the classic TOML trap (a top-level key written after a [table] header).
_TOP_LEVEL_KEYS = {"playlist", "privacy", "category_id", "cadence", "upload_order", "tags", "title", "description", "tags_when"}
_TITLE_KEYS = {"templates", "hashtags", "hashtag_count"}
_DESCRIPTION_KEYS = {"templates"}
_TAGS_WHEN_KEYS = {"when", "add"}


def _reject_unknown(found: object, allowed: set[str], where: str, path: Path) -> None:
    if not isinstance(found, dict):
        return
    unknown = set(found) - allowed
    if not unknown:
        return
    hint = ""
    misplaced = unknown & _TOP_LEVEL_KEYS
    if where != "top level" and misplaced:
        hint = (
            f" - {sorted(misplaced)} look like top-level keys written after the [{where}] header "
            f"(TOML absorbs trailing top-level keys into the preceding table); move them above the "
            f"first [table]"
        )
    raise ValueError(f"{path}: unknown key(s) {sorted(unknown)} in {where}{hint}. Allowed: {sorted(allowed)}.")


def _validate_schema(data: dict, path: Path) -> None:
    _reject_unknown(data, _TOP_LEVEL_KEYS, "top level", path)
    _reject_unknown(data.get("title"), _TITLE_KEYS, "title", path)
    _reject_unknown(data.get("description"), _DESCRIPTION_KEYS, "description", path)
    for rule in data.get("tags_when", []):
        _reject_unknown(rule, _TAGS_WHEN_KEYS, "tags_when", path)


def load_project_config(path: Path) -> ProjectConfig:
    """Load a single ``projects/<name>.toml`` pool config."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    name = path.stem
    _validate_schema(data, path)

    title_raw = data.get("title", {})
    hc = title_raw.get("hashtag_count", [0, 0])
    # Normalize to a sane [lo, hi] with 0 <= lo <= hi so a reversed/short range can never
    # blow up rng.randint(lo, hi) at upload time.
    lo = max(0, int(hc[0])) if len(hc) > 0 else 0
    hi = max(lo, int(hc[1])) if len(hc) > 1 else lo
    title = TitleSpec(
        templates=list(title_raw.get("templates", [])),
        hashtags=list(title_raw.get("hashtags", [])),
        hashtag_count=(lo, hi),
    )
    desc = DescriptionSpec(templates=list(data.get("description", {}).get("templates", [])))
    tags_when = [TagRule(when=str(r["when"]), add=list(r.get("add", []))) for r in data.get("tags_when", [])]

    return ProjectConfig(
        name=name,
        playlist=data.get("playlist"),
        privacy=data.get("privacy", "private"),
        category_id=str(data.get("category_id", CATEGORY_FILM_ANIMATION)),
        cadence_seconds=parse_duration(data.get("cadence", 0)),
        upload_order=parse_upload_order(data["upload_order"]) if "upload_order" in data else None,
        title=title,
        description=desc,
        tags=list(data.get("tags", [])),
        tags_when=tags_when,
    )


# --------------------------------------------------------------------------- #
# Global config + backend specs
# --------------------------------------------------------------------------- #


@dataclass
class BackendSpec:
    """One queue backend entry from the global config's ``[[backend]]`` list."""

    kind: str  # "local" | "objectstore"
    options: dict[str, object] = field(default_factory=dict)


@dataclass
class GlobalConfig:
    home: Path
    credentials_dir: Path
    projects_dir: Path
    backends: list[BackendSpec]
    default_privacy: str = "private"
    settle_seconds: float = 5.0
    upload_order: str = "first"

    @property
    def state_dir(self) -> Path:
        return self.home

    def project_config_path(self, project: str) -> Path:
        return self.projects_dir / f"{project}.toml"

    def load_project(self, project: str) -> ProjectConfig:
        path = self.project_config_path(project)
        if not path.exists():
            raise FileNotFoundError(f"no project config for {project!r} at {path}")
        return load_project_config(path)

    def known_projects(self) -> list[str]:
        if not self.projects_dir.is_dir():
            return []
        return sorted(p.stem for p in self.projects_dir.glob("*.toml"))


def _default_projects_dir() -> Path:
    # Prefer a sibling ``projects/`` next to the installed package's repo root if present,
    # else under HOME. The systemd unit normally sets UPLOADER_PROJECTS_DIR explicitly.
    env = os.environ.get("UPLOADER_PROJECTS_DIR")
    if env:
        return Path(env)
    repo_projects = Path(__file__).resolve().parents[2] / "projects"
    if repo_projects.is_dir():
        return repo_projects
    return DEFAULT_HOME / "projects"


_UPLOAD_ORDER_ALIASES = {
    "fifo": "first",
    "oldest": "first",
    "lifo": "last",
    "newest": "last",
}
_UPLOAD_ORDER_VALUES = {"first", "last", "random"}


def parse_upload_order(value: object) -> str:
    """Parse the global bundle selection mode."""
    order = str(value).strip().lower()
    order = _UPLOAD_ORDER_ALIASES.get(order, order)
    if order not in _UPLOAD_ORDER_VALUES:
        raise ValueError(f"upload_order must be one of {sorted(_UPLOAD_ORDER_VALUES)}, got {value!r}")
    return order


def load_global_config(path: Path | None = None) -> GlobalConfig:
    """Load the global config from ``path`` (or ``$UPLOADER_HOME/config.toml``).

    Missing file is fine - sensible defaults (a single local inbox under HOME) apply,
    overridable by env vars (``UPLOADER_HOME``, ``UPLOADER_PROJECTS_DIR``,
    ``UPLOADER_CREDENTIALS_DIR``).
    """
    home = DEFAULT_HOME
    data: dict = {}
    cfg_path = path or (home / "config.toml")
    if cfg_path.exists():
        data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        if "home" in data:
            home = Path(os.path.expanduser(str(data["home"])))

    credentials_dir = Path(
        os.environ.get("UPLOADER_CREDENTIALS_DIR")
        or os.path.expanduser(str(data.get("credentials_dir", home / "credentials")))
    )
    projects_dir = _default_projects_dir()
    if "projects_dir" in data and not os.environ.get("UPLOADER_PROJECTS_DIR"):
        projects_dir = Path(os.path.expanduser(str(data["projects_dir"])))

    backends_raw = data.get("backend", [])
    if not backends_raw:
        # Default: a single local inbox under HOME.
        backends_raw = [{"kind": "local", "inbox": str(home / "inbox")}]
    backends: list[BackendSpec] = []
    for b in backends_raw:
        options = {k: v for k, v in b.items() if k != "kind"}
        if b["kind"] == "local" and "inbox" in options:
            options["inbox"] = Path(os.path.expanduser(str(options["inbox"])))
        backends.append(BackendSpec(kind=b["kind"], options=options))

    return GlobalConfig(
        home=home,
        credentials_dir=credentials_dir,
        projects_dir=projects_dir,
        backends=backends,
        default_privacy=str(data.get("privacy", "private")),
        settle_seconds=float(data.get("settle_seconds", 5.0)),
        upload_order=parse_upload_order(data.get("upload_order", "first")),
    )


__all__ = [
    "BackendSpec",
    "DescriptionSpec",
    "GlobalConfig",
    "ProjectConfig",
    "TagRule",
    "TitleSpec",
    "load_global_config",
    "load_project_config",
    "parse_duration",
]
