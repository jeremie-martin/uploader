"""Durable scheduler state: per-project cadence clock, upload ledger, dedup, failures.

Kept deliberately separate from the queue backends. Because the uploader process stays
put (e.g. on the Pi) even when the video buffer moves to object storage, this state just
lives locally under ``$UPLOADER_HOME`` throughout — only the videos relocate.

Files (all under ``home``):

* ``state.json``    — ``{"projects": {"<name>": "<last_upload_iso>"}}`` (cadence clock)
* ``uploads.jsonl`` — append-only ledger; source of truth, used for dedup by youtube id
* ``failed.jsonl``  — append-only terminal failures, for post-mortem
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from uploader.atomic import (
    append_jsonl,
    atomic_write_json,
    now_iso,
    read_json_or_none,
    read_jsonl,
)


def _parse_iso(s: str | None) -> float | None:
    """Parse an ISO-8601 timestamp to an epoch (seconds). 'Z' is treated as UTC, and a
    timezone-naive string is assumed UTC — never local — so the cadence clock is correct
    regardless of the host's timezone."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


class State:
    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self.state_path = self.home / "state.json"
        self.uploads_path = self.home / "uploads.jsonl"
        self.failed_path = self.home / "failed.jsonl"

    # --- cadence clock ---------------------------------------------------
    def _state(self) -> dict[str, Any]:
        return read_json_or_none(self.state_path) or {"projects": {}}

    def last_upload_at(self, project: str) -> float | None:
        """Epoch seconds of this project's last upload, or None if never."""
        iso = self._state().get("projects", {}).get(project)
        return _parse_iso(iso)

    def seconds_until_due(self, project: str, cadence_seconds: int, *, now: float | None = None) -> float:
        """How long until ``project`` may upload again (0 if due now)."""
        if cadence_seconds <= 0:
            return 0.0
        last = self.last_upload_at(project)
        if last is None:
            return 0.0
        elapsed = (now if now is not None else time.time()) - last
        return max(0.0, cadence_seconds - elapsed)

    def touch_project(self, project: str, ts: float | None = None) -> None:
        st = self._state()
        st.setdefault("projects", {})[project] = now_iso(datetime.fromtimestamp(ts, UTC) if ts else None)
        atomic_write_json(self.state_path, st)

    # --- ledger / dedup --------------------------------------------------
    def ledger_has_youtube_id(self, youtube_id: str) -> bool:
        return any(e.get("youtube_id") == youtube_id for e in read_jsonl(self.uploads_path))

    def record_upload(self, record: dict[str, Any]) -> None:
        """Append a successful upload to the ledger (idempotent on youtube_id)."""
        yt = record.get("youtube_id")
        if yt and self.ledger_has_youtube_id(yt):
            return
        append_jsonl(self.uploads_path, record)

    def record_failure(self, record: dict[str, Any]) -> None:
        append_jsonl(self.failed_path, record)

    def uploads(self) -> list[dict[str, Any]]:
        return read_jsonl(self.uploads_path)


__all__ = ["State"]
