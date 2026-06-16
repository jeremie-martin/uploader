"""The queue interface plus the bundle data types every backend produces.

A **bundle** is the contract a project produces: a finished video file + an
``upload.json`` sidecar. ``upload.json`` is written/uploaded **last** so it doubles as
the "ready" sentinel - a half-transferred bundle is never picked up.

``upload.json`` schema (only ``project`` is required)::

    {
      "project": "double-pendulum",
      "values": { "count": 1000000, "boom_time": "12s" },   # used by templates + recorded
      "overrides": { "title": "...", "tags": ["..."], "playlist": "PL...", "privacy": "public" },
      "meta": { "spec": "drop-heavy", "git_sha": "abc123" }, # recorded only; never uploaded
      "video": "video.mp4",          # optional; else the lone video file is used
      "created_at": "2026-06-16T12:00:00Z"   # optional; for FIFO ordering
    }
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SIDECAR_NAME = "upload.json"
MARKER_UPLOADED = "uploaded"
MARKER_FAILED = "failed"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}


@dataclass
class BundleRef:
    """A handle to one bundle inside a backend (does not imply it is downloaded)."""

    backend: Queue
    bundle_id: str  # unique within the backend (dir name / object prefix)
    project: str
    created_at: float  # epoch seconds, for FIFO ordering
    sidecar: dict[str, Any]
    uploaded_marker: dict[str, Any] | None = None  # present => uploaded but not cleaned up

    @property
    def is_resumed(self) -> bool:
        return self.uploaded_marker is not None

    @property
    def values(self) -> dict[str, Any]:
        return dict(self.sidecar.get("values") or {})

    @property
    def overrides(self) -> dict[str, Any]:
        return dict(self.sidecar.get("overrides") or {})

    @property
    def meta(self) -> dict[str, Any]:
        """Free-form reference data - recorded in the ledger, never uploaded or templated."""
        return dict(self.sidecar.get("meta") or {})

    def __repr__(self) -> str:  # avoid recursing into self.backend
        return f"BundleRef({self.backend.name}:{self.bundle_id} project={self.project})"


@dataclass
class LocalBundle:
    """A bundle materialized on local disk, ready to upload."""

    ref: BundleRef
    video_path: Path
    sidecar: dict[str, Any] = field(default_factory=dict)


class Queue(abc.ABC):
    """A pluggable video buffer. Manages bundle storage + crash-safety markers only;
    the ledger / cadence state lives separately in :class:`uploader.state.State` so it
    can stay on the uploader host while the video buffer moves to the cloud."""

    name: str

    @abc.abstractmethod
    def list_ready(self) -> list[BundleRef]:
        """Return refs for fully-staged bundles (sentinel present & settled, not failed).

        Bundles bearing an ``uploaded`` marker are returned with ``uploaded_marker`` set
        so the scheduler can finalize (not re-upload) them.
        """

    @abc.abstractmethod
    def fetch(self, ref: BundleRef, dest_dir: Path) -> LocalBundle:
        """Materialize the bundle's video locally. No-op copy for a local backend;
        a single-video download for an object store."""

    @abc.abstractmethod
    def mark_uploaded(self, ref: BundleRef, record: dict[str, Any]) -> None:
        """Durably record that this bundle was uploaded (storing the youtube id), so a
        crash before :meth:`remove` results in finalize-not-reupload on the next tick."""

    @abc.abstractmethod
    def mark_failed(self, ref: BundleRef, reason: str) -> None:
        """Mark a bundle as terminally failed so it is skipped (kept for inspection)."""

    @abc.abstractmethod
    def remove(self, ref: BundleRef) -> None:
        """Delete the bundle from the buffer."""


def select_video(names: list[str], sidecar: dict[str, Any]) -> str | None:
    """Pick the video file name: the sidecar's ``video`` field, else the lone video file."""
    declared = sidecar.get("video")
    if declared:
        return str(declared)
    vids = [n for n in names if Path(n).suffix.lower() in VIDEO_EXTENSIONS]
    if len(vids) == 1:
        return vids[0]
    if not vids:
        return None
    # Ambiguous: prefer a file literally named like a video, else first sorted.
    return sorted(vids)[0]
