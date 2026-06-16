"""Local-filesystem queue backend - a directory inbox (the Pi; an rsync target).

Layout::

    inbox/
      <bundle-dir>/
        video.mp4
        upload.json        # written/renamed LAST → the "ready" sentinel
        uploaded           # marker (JSON), written after a successful YouTube upload
        failed             # marker, written on terminal failure

Mirrors the battle-tested rules from ``lpt2d``/``motiontwin``: skip dot-prefixed
in-progress staging dirs, require a settle time so a half-rsynced bundle is not picked
up, and FIFO ordering by the sentinel's mtime.
"""

from __future__ import annotations

import json
import shutil
import time
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

    def list_ready(self) -> list[BundleRef]:
        if not self.inbox.is_dir():
            return []
        now = time.time()
        refs: list[BundleRef] = []
        for d in sorted(self.inbox.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if (d / MARKER_FAILED).exists():
                continue
            sentinel = d / SIDECAR_NAME
            if not sentinel.exists():
                continue  # still being assembled
            marker_path = d / MARKER_UPLOADED
            resumed = marker_path.exists()
            # Fresh (not-yet-uploaded) bundles must settle; resumed ones are already done.
            if not resumed and (now - sentinel.stat().st_mtime) < self.settle_seconds:
                continue
            try:
                sidecar = json.loads(sentinel.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("skipping {}: unreadable {}: {}", d.name, SIDECAR_NAME, e)
                continue
            project = sidecar.get("project")
            if not project:
                logger.warning("skipping {}: sidecar missing 'project'", d.name)
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
                    bundle_id=d.name,
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
        shutil.rmtree(self._bundle_dir(ref.bundle_id), ignore_errors=True)


__all__ = ["LocalQueue"]
