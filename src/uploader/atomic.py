"""Crash-safe filesystem helpers: atomic writes and durable directory syncs.

Lifted from the pattern shared by ``motiontwin`` and ``lpt2d`` (atomic_write_bytes
+ fsync). The marker-first commit strategy in :mod:`uploader.tick` relies on these:
a partially written marker/ledger must never be observable.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (write tmp + fsync + os.replace)."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` (UTF-8) to ``path`` atomically."""
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, obj: Any) -> None:
    """Serialize ``obj`` to JSON and write it to ``path`` atomically."""
    atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False))


def fsync_dir(path: Path) -> None:
    """fsync a directory so a freshly created child entry is durable.

    No-op-safe on platforms where opening a directory for fsync is not allowed.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def append_jsonl(path: Path, obj: Any) -> None:
    """Append one JSON object as a line to a JSONL ledger, creating it if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL ledger into a list of dicts (empty if missing). Skips blank/corrupt lines."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def read_json_or_none(path: Path) -> Any | None:
    """Read+parse a JSON file, returning None if missing or corrupt."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def now_iso(ts: datetime | None = None) -> str:
    """Return an ISO-8601 UTC timestamp string (e.g. '2026-06-16T12:00:00Z')."""
    dt = ts or datetime.now(UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
