"""Best-effort technical metadata extraction via ffprobe.

Captured at upload time and stored in the ledger so uploads can later be correlated with
YouTube analytics (resolution, duration, fps, codecs, size). Entirely optional: if
ffprobe is absent or the probe fails, this returns ``{}`` and never raises - probing
must never block an upload.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from loguru import logger


def _fps(rate: str | None) -> float | None:
    """Turn an ffprobe frame rate like '30/1' into 30.0."""
    if not rate:
        return None
    try:
        num, den = rate.split("/")
        return round(int(num) / int(den), 3)
    except (ValueError, ZeroDivisionError):
        return None


def probe_media(path: Path) -> dict[str, Any]:
    """Return a small dict of technical metadata for ``path`` (or ``{}`` on any failure)."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {}
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            logger.warning("ffprobe exited {} for {}", proc.returncode, path)
            return {}
        data = json.loads(proc.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
        logger.warning("ffprobe failed for {}: {}", path, e)
        return {}

    fmt = data.get("format", {})
    media: dict[str, Any] = {}
    if fmt.get("duration"):
        media["duration_s"] = round(float(fmt["duration"]), 3)
    if fmt.get("size"):
        media["size_bytes"] = int(fmt["size"])
    for s in data.get("streams", []):
        kind = s.get("codec_type")
        if kind == "video" and "width" not in media:
            media["width"] = s.get("width")
            media["height"] = s.get("height")
            media["vcodec"] = s.get("codec_name")
            media["fps"] = _fps(s.get("r_frame_rate"))
        elif kind == "audio" and "acodec" not in media:
            media["acodec"] = s.get("codec_name")
    return media


__all__ = ["probe_media"]
