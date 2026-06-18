#!/usr/bin/env python3
"""Backfill bridge: stage existing double-pendulum renders as uploader bundles.

PROJECT ADAPTER (one-time use). This is deliberately *not* part of the uploader core - the
core stays project-agnostic. This script encodes double-pendulum's ``metadata.json`` schema
so its already-rendered videos become generic bundles in the uploader inbox, after which the
normal scheduler drains them at the project's cadence.

For each ``video_*`` directory under the source it derives the sidecar ``values`` that
``projects/double-pendulum.toml`` expects:

* ``count``     = ``simulation.pendulum_count``
* ``boom_time`` = ``round(results.boom_frame / output.video_fps)`` seconds, **video-time**
  (matches ``templates.py:generate_description``); ``"N/A"`` when unavailable. Two
  description templates reference ``{boom_time}``, so it is always supplied.

It skips directories already uploaded (basename present in double-pendulum's
``uploads.jsonl`` archive), and those lacking the final video or a pendulum count. The
video is hardlinked (same filesystem -> no extra disk); the source render is untouched.

Usage::

    uv run python scripts/stage_double_pendulum.py ~/double-pendulum/watch2 --dry-run
    uv run python scripts/stage_double_pendulum.py ~/double-pendulum/watch2 --limit 3
    uv run python scripts/stage_double_pendulum.py ~/double-pendulum/watch2
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

# Reuse the uploader's own staging primitives so bundles are built exactly like `uploader stage`.
from uploader.atomic import atomic_write_json, now_iso
from uploader.cli import _default_inbox, _reserve_bundle_dir
from uploader.config import load_global_config
from uploader.queue.base import SIDECAR_NAME

PROJECT = "double-pendulum"
VIDEO_NAME = "video_processed_final.mp4"
DEFAULT_ARCHIVE = Path.home() / ".local/share/pendulum-tools/uploads.jsonl"


def load_uploaded_source_dirs(archive: Path) -> set[str]:
    """Basenames of source dirs that double-pendulum already uploaded (dedup safety net)."""
    seen: set[str] = set()
    if not archive.exists():
        return seen
    for line in archive.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        sd = rec.get("source_dir")
        if sd:
            seen.add(Path(sd).name)
    return seen


def derive_values(meta: dict) -> dict | None:
    """Map double-pendulum metadata.json -> sidecar values, or None if count is missing."""
    sim = meta.get("simulation") or {}
    out = meta.get("output") or {}
    results = meta.get("results") or {}
    count = sim.get("pendulum_count")
    if count is None:
        return None
    boom_time = "N/A"
    boom_frame = results.get("boom_frame")
    fps = out.get("video_fps")
    if boom_frame and fps:
        boom_time = f"{boom_frame / fps:.0f}s"  # video-time, matches templates.py
    elif results.get("boom_seconds") is not None:
        boom_time = f"{results['boom_seconds']:.0f}s"  # fallback: simulation time
    return {"count": int(count), "boom_time": boom_time}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path, help="directory containing video_* render dirs (e.g. ~/double-pendulum/watch2)")
    ap.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE, help="double-pendulum uploads.jsonl for dedup")
    ap.add_argument("--config", type=Path, default=None, help="uploader config.toml (default: $UPLOADER_HOME/config.toml)")
    ap.add_argument("--dry-run", action="store_true", help="report what would be staged; stage nothing")
    ap.add_argument("--limit", type=int, default=0, help="stage at most N bundles (0 = all)")
    args = ap.parse_args()

    cfg = load_global_config(args.config)
    cfg.load_project(PROJECT)  # fail fast if the pool config is missing or broken
    inbox = _default_inbox(cfg)
    uploaded = load_uploaded_source_dirs(args.archive)

    src = args.source.expanduser()
    if not src.is_dir():
        ap.error(f"source is not a directory: {src}")
    dirs = sorted(d for d in src.iterdir() if d.is_dir() and d.name.startswith("video_"))

    staged = skip_uploaded = skip_novideo = skip_nometa = 0
    for d in dirs:
        if args.limit and staged >= args.limit:
            break
        if d.name in uploaded:
            skip_uploaded += 1
            continue
        video = d / VIDEO_NAME
        if not video.exists():
            print(f"skip (no {VIDEO_NAME}): {d.name}")
            skip_novideo += 1
            continue
        try:
            meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"skip (bad metadata.json: {e}): {d.name}")
            skip_nometa += 1
            continue
        values = derive_values(meta)
        if values is None:
            print(f"skip (no pendulum_count): {d.name}")
            skip_nometa += 1
            continue

        if args.dry_run:
            print(f"would stage {PROJECT}/{d.name}  count={values['count']} boom_time={values['boom_time']}")
            staged += 1
            continue

        bundle_id, bdir = _reserve_bundle_dir(inbox, f"{PROJECT}/{d.name}")
        dest = bdir / "video.mp4"
        try:
            os.link(video, dest)  # hardlink: instant, no extra disk; original untouched
        except OSError:
            shutil.copy2(video, dest)  # cross-device or unsupported -> copy
        created_at = meta.get("created_at")
        sidecar = {
            "project": PROJECT,
            "video": "video.mp4",
            "created_at": created_at if isinstance(created_at, str) and created_at.strip() else now_iso(),
            "values": values,
            "meta": {"source_dir": d.name},
        }
        atomic_write_json(bdir / SIDECAR_NAME, sidecar)  # written last = ready sentinel
        print(f"staged {bundle_id}  count={values['count']} boom_time={values['boom_time']}")
        staged += 1

    verb = "would stage" if args.dry_run else "staged"
    print(
        f"\n{verb}: {staged} | skipped already-uploaded: {skip_uploaded} | "
        f"no-video: {skip_novideo} | bad/no-meta: {skip_nometa} | inbox: {inbox}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
