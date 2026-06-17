"""The scheduler tick - the oneshot unit a cron/systemd-timer fires.

Modeled on ``motiontwin``'s ``publish-once.mjs`` skeleton, but with all project-specific
work (video composition, highscore logic) removed: this only resolves text and uploads.

One tick:

1. Acquire a single-instance lock.
2. Gather ready bundles from every configured queue backend.
3. **Finalize resumed bundles** (uploaded marker present → ledger-if-needed + remove);
   never re-upload them.
4. **Per-project cadence selection**: among fresh bundles whose project is *due*
   (``now - last_upload >= cadence``), pick the oldest. If none are due, exit cleanly.
5. Fetch one video, resolve metadata via the engine, upload, then commit crash-safely:
   marker (fsync) → ledger (dedup) → cadence clock → remove bundle.

Exit codes (so the timer/operator can tell apart states):
    0  uploaded one, or nothing to do
    2  rate-limited - bundle kept for retry
    3  auth broken - needs ``uploader auth``; bundle kept
    1  a bundle failed terminally (recorded to failed.jsonl, removed)
"""

from __future__ import annotations

import os
import random
import tempfile
import time
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from loguru import logger

from uploader import engine, youtube
from uploader.atomic import now_iso
from uploader.config import GlobalConfig, load_global_config
from uploader.probe import probe_media
from uploader.queue import build_backends
from uploader.queue.base import BundleRef
from uploader.state import State, _parse_iso

EXIT_OK = 0
EXIT_TERMINAL = 1
EXIT_RATE_LIMIT = 2
EXIT_AUTH = 3

LOCK_PATH = Path(tempfile.gettempdir()) / "uploader-tick.lock"


@contextmanager
def single_instance_lock(path: Path = LOCK_PATH) -> Iterator[None]:
    """A PID-bearing O_EXCL lock; steals the lock if the holding PID is dead."""
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, str(os.getpid()).encode())
            os.fsync(fd)
            os.close(fd)
            break
        except FileExistsError:
            try:
                holder = int(path.read_text().strip() or "0")
            except (ValueError, OSError):
                holder = 0
            if holder and _pid_alive(holder):
                raise RuntimeError(f"another uploader tick is running (pid {holder})") from None
            logger.warning("removing stale lock from dead pid {}", holder)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    try:
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _seeded_rng(bundle_id: str) -> random.Random:
    return random.Random(zlib.crc32(bundle_id.encode("utf-8")))


def _finalize_resumed(ref: BundleRef, state: State) -> None:
    """A bundle was uploaded but not cleaned up before a crash: ledger-if-needed + remove."""
    record = dict(ref.uploaded_marker or {})
    yt = record.get("youtube_id")
    if yt and not state.ledger_has_youtube_id(yt):
        state.record_upload(record)
        logger.info("resumed: appended ledger for {} (yt {})", ref.bundle_id, yt)
    uploaded_at = _parse_iso(record.get("uploaded_at") if isinstance(record.get("uploaded_at"), str) else None)
    last_upload = state.last_upload_at(ref.project)
    if uploaded_at is not None and (last_upload is None or uploaded_at > last_upload):
        state.touch_project(ref.project, uploaded_at)
        logger.info("resumed: restored cadence clock for {} from marker", ref.project)
    ref.backend.remove(ref)
    logger.info("resumed: removed already-uploaded bundle {}", ref.bundle_id)


def _mark_terminal(ref: BundleRef, state: State, reason: str, *, dry_run: bool) -> int:
    logger.error("{} failed terminally: {}", ref.bundle_id, reason)
    if not dry_run:
        ref.backend.mark_failed(ref, reason)
        state.record_failure({"failed_at": now_iso(), "bundle": ref.bundle_id, "project": ref.project, "reason": reason})
    return EXIT_TERMINAL


def _select_due(fresh: list[BundleRef], cfg: GlobalConfig, state: State, now: float) -> BundleRef | None:
    """Pick the oldest bundle whose project is due per its cadence."""
    due: list[BundleRef] = []
    for ref in fresh:
        try:
            pc = cfg.load_project(ref.project)
        except FileNotFoundError:
            logger.warning("no project config for {!r}; leaving bundle {} in queue", ref.project, ref.bundle_id)
            continue
        if state.seconds_until_due(ref.project, pc.cadence_seconds, now=now) <= 0:
            due.append(ref)
    if not due:
        return None
    due.sort(key=lambda r: (r.created_at, r.bundle_id))
    return due[0]


def run_tick(config_path: Path | None = None, *, dry_run: bool = False) -> int:
    cfg = load_global_config(config_path)
    state = State(cfg.home)
    backends = build_backends(cfg.backends, settle_seconds=cfg.settle_seconds)

    with single_instance_lock():
        refs: list[BundleRef] = []
        for b in backends:
            try:
                refs.extend(b.list_ready())
            except Exception as e:  # one flaky backend must not sink the tick
                logger.warning("backend {} list_ready failed: {}", b.name, e)

        resumed = [r for r in refs if r.is_resumed]
        fresh = [r for r in refs if not r.is_resumed]

        for r in resumed:
            try:
                _finalize_resumed(r, state)
            except Exception as e:
                logger.warning("failed to finalize resumed bundle {}: {}", r.bundle_id, e)

        now = time.time()
        ref = _select_due(fresh, cfg, state, now)
        if ref is None:
            logger.info("nothing due ({} fresh, {} resumed)", len(fresh), len(resumed))
            return EXIT_OK

        return _process_one(ref, cfg, state, dry_run=dry_run)


def _process_one(ref: BundleRef, cfg: GlobalConfig, state: State, *, dry_run: bool) -> int:
    pc = cfg.load_project(ref.project)

    try:
        sidecar_meta = ref.meta
    except (TypeError, ValueError) as e:
        return _mark_terminal(ref, state, f"sidecar meta: {e}", dry_run=dry_run)

    # Resolve metadata (a malformed template/bundle is a terminal failure for this bundle).
    try:
        meta = engine.pick(pc, ref.values, rng=_seeded_rng(ref.bundle_id), overrides=ref.overrides)
    except engine.TemplateError as e:
        return _mark_terminal(ref, state, f"metadata: {e}", dry_run=dry_run)

    logger.info(
        "resolved {}: title={!r} tags={} privacy={} playlist={}",
        ref.bundle_id, meta.title, meta.tags, meta.privacy, meta.playlist,
    )
    if dry_run:
        logger.info("[dry-run] would upload {} ({})", ref.bundle_id, ref.backend.name)
        return EXIT_OK

    with tempfile.TemporaryDirectory(prefix="uploader-fetch-") as tmp:
        try:
            local = ref.backend.fetch(ref, Path(tmp))
        except Exception as e:
            return _mark_terminal(ref, state, f"fetch: {e}", dry_run=dry_run)

        # Probe while the video is local (the temp dir is gone once we leave this block).
        media = probe_media(local.video_path)

        try:
            creds = youtube.load_or_refresh(cfg.credentials_dir)
        except youtube.AuthError as e:
            logger.error("AUTH: {} (keeping bundle {})", e, ref.bundle_id)
            return EXIT_AUTH

        try:
            video_id = youtube.upload(
                creds=creds,
                video_path=local.video_path,
                title=meta.title,
                description=meta.description,
                tags=meta.tags,
                privacy=meta.privacy,
                category_id=meta.category_id,
                playlist_id=meta.playlist,
            )
        except youtube.RateLimitError as e:
            logger.warning("RATE LIMIT: {} (keeping bundle {})", e, ref.bundle_id)
            return EXIT_RATE_LIMIT
        except youtube.UploadError as e:
            return _mark_terminal(ref, state, f"upload: {e}", dry_run=dry_run)

    # --- crash-safe commit: marker → ledger → cadence clock → remove ----
    record = {
        "uploaded_at": now_iso(),
        "youtube_id": video_id,
        "youtube_url": f"https://youtu.be/{video_id}",
        "project": ref.project,
        "bundle": ref.bundle_id,
        "backend": ref.backend.name,
        "title": meta.title,
        "description": meta.description,
        "tags": meta.tags,
        "privacy": meta.privacy,
        "playlist": meta.playlist,
        "values": ref.values,
        "meta": sidecar_meta,
        "media": media,
    }
    ref.backend.mark_uploaded(ref, record)
    state.record_upload(record)
    state.touch_project(ref.project)
    ref.backend.remove(ref)
    logger.info("DONE {} -> {} ({})", ref.bundle_id, record["youtube_url"], ref.project)
    return EXIT_OK


__all__ = [
    "EXIT_AUTH",
    "EXIT_OK",
    "EXIT_RATE_LIMIT",
    "EXIT_TERMINAL",
    "run_tick",
    "single_instance_lock",
]
