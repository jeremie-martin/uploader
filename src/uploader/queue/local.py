"""Local-filesystem queue backend: a directory inbox (the Pi; an rsync target).

The inbox is scanned RECURSIVELY: any directory holding an ``upload.json`` sentinel is a
bundle, wherever it sits in the tree, so you can organize the inbox into whatever
sub-folders you like (by project, by date, by experiment). It is still strictly one
video + one sidecar per bundle; the scan never descends into a bundle, and skips
dot-prefixed in-progress staging dirs at any level.

Layout (the bundle dir may be nested at any depth under the inbox)::

    inbox/
      anything/you/want/<bundle>/
        video.mp4
        upload.json        # written/renamed LAST, the "ready" sentinel
        uploaded           # marker (JSON) written after a successful upload
        failed             # marker written on terminal failure

Mirrors the battle-tested rules from lpt2d/motiontwin: a settle time so a half-written
bundle is not picked up, dot-prefix skipping, and FIFO ordering by the sentinel mtime.
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from loguru import logger

from uploader.atomic import atomic_write_json, fsync_dir, now_iso
from uploader.queue.base import (
    MARKER_FAILED,
    MARKER_UPLOADED,
    SIDECAR_NAME,
    BundleRef,
    LocalBundle,
    Queue,
    select_video,
)
from uploader.state import _parse_iso


class LocalQueue(Queue):
    def __init__(self, inbox: str | Path, settle_seconds: float = 5.0) -> None:
        self.inbox = Path(inbox)
        self.settle_seconds = settle_seconds
        self.name = f"local:{self.inbox}"

    def _bundle_dir(self, bundle_id: str) -> Path:
        return self.inbox / bundle_id

    def _iter_bundle_dirs(self) -> Iterator[Path]:
        """Yield every directory under the inbox that holds a sidecar, without descending
        into a bundle or into dot-prefixed (in-progress staging) directories."""
        stack = [self.inbox]
        while stack:
            d = stack.pop()
            if d != self.inbox and (d / SIDECAR_NAME).is_file():
                yield d  # a bundle: do not look for nested bundles inside it
                continue
            try:
                children = list(d.iterdir())
            except OSError:
                continue
            for child in children:
                if child.is_dir() and not child.name.startswith("."):
                    stack.append(child)

    def list_ready(self) -> list[BundleRef]:
        if not self.inbox.is_dir():
            return []
        now = time.time()
        refs: list[BundleRef] = []
        for d in self._iter_bundle_dirs():
            if (d / MARKER_FAILED).exists():
                continue
            sentinel = d / SIDECAR_NAME
            marker_path = d / MARKER_UPLOADED
            resumed = marker_path.exists()
            # Fresh bundles must settle; resumed ones are already uploaded.
            if not resumed and (now - sentinel.stat().st_mtime) < self.settle_seconds:
                continue
            try:
                sidecar = json.loads(sentinel.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("skipping {}: unreadable {}: {}", d, SIDECAR_NAME, e)
                continue
            project = sidecar.get("project")
            if not project:
                logger.warning("skipping {}: sidecar missing 'project'", d)
                continue
            marker = None
            if resumed:
                try:
                    marker = json.loads(marker_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    marker = {}
            created = _parse_iso(sidecar.get("created_at")) or sentinel.stat().st_mtime
            refs.append(
                BundleRef(
                    backend=self,
                    bundle_id=d.relative_to(self.inbox).as_posix(),  # path relative to inbox; unique
                    project=project,
                    created_at=created,
                    sidecar=sidecar,
                    uploaded_marker=marker,
                )
            )
        return refs

    def fetch(self, ref: BundleRef, dest_dir: Path) -> LocalBundle:
        # Local backend: the video is already on disk; no copy needed.
        d = self._bundle_dir(ref.bundle_id)
        names = [p.name for p in d.iterdir() if p.is_file()]
        video_name = select_video(names, ref.sidecar)
        if video_name is None or not (d / video_name).exists():
            raise FileNotFoundError(f"no video file in bundle {d}")
        return LocalBundle(ref=ref, video_path=d / video_name, sidecar=ref.sidecar)

    def mark_uploaded(self, ref: BundleRef, record: dict[str, Any]) -> None:
        d = self._bundle_dir(ref.bundle_id)
        atomic_write_json(d / MARKER_UPLOADED, record)
        fsync_dir(d)

    def mark_failed(self, ref: BundleRef, reason: str) -> None:
        d = self._bundle_dir(ref.bundle_id)
        atomic_write_json(d / MARKER_FAILED, {"reason": reason, "failed_at": now_iso()})
        fsync_dir(d)

    def remove(self, ref: BundleRef) -> None:
        d = self._bundle_dir(ref.bundle_id)
        shutil.rmtree(d, ignore_errors=True)
        self._prune_empty_parents(d.parent)

    def _prune_empty_parents(self, d: Path) -> None:
        """Remove now-empty organizational sub-dirs up to (but never including) the inbox,
        so recursive layouts don't leave a litter of empty folders behind."""
        while d != self.inbox and self.inbox in d.parents:
            try:
                d.rmdir()  # only succeeds when empty
            except OSError:
                break
            d = d.parent


__all__ = ["LocalQueue"]
