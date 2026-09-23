"""YouTube upload core - OAuth + resumable upload + playlist insert + rate-limit.

Ported (≈ verbatim) from ``motiontwin/scripts/motiontwin/youtube_upload.py``, which is
itself a near-copy of ``lpt2d``'s. Lifted once so no future project re-implements it.

Token storage: a pickled ``google.oauth2.credentials.Credentials`` at
``<credentials_dir>/token.pickle``, refreshed in place when expired. Since everything
publishes to one channel, there is a single token.

Errors are raised as typed exceptions so the scheduler can distinguish "retry later"
(:class:`RateLimitError`) from "token is broken" (:class:`AuthError`) from "this video
is doomed" (:class:`UploadError`).
"""

from __future__ import annotations

import json
import pickle
from collections.abc import Callable
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from loguru import logger

from uploader.atomic import atomic_write_bytes
from uploader.engine import CATEGORY_FILM_ANIMATION

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]
RATE_LIMIT_REASONS = {
    "quotaExceeded",
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "dailyLimitExceeded",
    # The per-channel "videos uploaded per 24h" cap. YouTube reports it as HTTP *400*,
    # not 403/429 - so it must be matched on reason, never on status.
    "uploadLimitExceeded",
}
CHUNKSIZE = 10 * 1024 * 1024  # 10 MiB resumable chunks


class UploadError(RuntimeError):
    """A terminal upload failure - the bundle should be marked failed."""


class RateLimitError(UploadError):
    """YouTube quota/rate-limit hit - retry later; do NOT mark the bundle failed."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AuthError(UploadError):
    """Token missing/invalid/not refreshable - needs ``uploader auth``; keep the bundle."""


def token_path(credentials_dir: Path) -> Path:
    return credentials_dir / "token.pickle"


def client_secrets_path(credentials_dir: Path) -> Path:
    return credentials_dir / "client_secrets.json"


def run_oauth_flow(credentials_dir: Path) -> None:
    """Run the interactive desktop OAuth flow and write ``token.pickle``."""
    secrets = client_secrets_path(credentials_dir)
    if not secrets.exists():
        raise AuthError(
            f"client_secrets.json missing at {secrets}\nDownload a Desktop OAuth client from Google Cloud Console and place it there."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    creds = flow.run_local_server(port=0)
    atomic_write_bytes(token_path(credentials_dir), pickle.dumps(creds))
    logger.info("wrote {}", token_path(credentials_dir))


def load_or_refresh(credentials_dir: Path) -> Credentials:
    """Load the cached token, refreshing in place if expired. Raises :class:`AuthError`."""
    tp = token_path(credentials_dir)
    if not tp.exists():
        raise AuthError(f"no token at {tp} - run `uploader auth` first")
    try:
        creds: Credentials = pickle.loads(tp.read_bytes())
    except Exception as e:  # noqa: BLE001 - token cache errors should be scheduler auth failures
        raise AuthError(f"cached token at {tp} is unreadable - re-run `uploader auth`: {e}") from e
    try:
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            atomic_write_bytes(tp, pickle.dumps(creds))
            logger.info("refreshed access token; pickle updated")
            return creds
    except Exception as e:  # noqa: BLE001 - google auth raises several refresh/cache exceptions
        raise AuthError(f"cached token refresh failed - re-run `uploader auth`: {e}") from e
    raise AuthError("cached token is invalid and not refreshable - re-run `uploader auth`")


def inspect_token(credentials_dir: Path) -> dict:
    """Report token health without making a network call (for `uploader status`).

    Returns a dict with ``present`` and, when present, ``valid``/``expired``/
    ``refreshable``/``expiry`` so an operator can confirm auth on a headless host.
    """
    tp = token_path(credentials_dir)
    if not tp.exists():
        return {"present": False}
    try:
        creds: Credentials = pickle.loads(tp.read_bytes())
    except Exception as e:  # noqa: BLE001 - any unpickle error is just "unreadable"
        return {"present": True, "error": f"unreadable: {e}"}
    return {
        "present": True,
        "valid": creds.valid,
        "expired": creds.expired,
        "refreshable": bool(creds.refresh_token),
        "expiry": creds.expiry.isoformat() + "Z" if creds.expiry else None,
    }


def _is_rate_limit(e: HttpError) -> bool:
    """True if YouTube is throttling us, so the bundle must be kept and retried.

    The machine-readable ``reason`` is authoritative and is checked for *any* status:
    YouTube spreads throttling across 429, 403 (quotaExceeded) and 400
    (uploadLimitExceeded), so keying off the status code silently turns a temporary
    throttle into a terminal failure that burns the bundle.
    """
    if e.resp.status == 429:
        return True
    try:
        content = json.loads(e.content.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(content, dict):
        return False
    error = content.get("error")
    if not isinstance(error, dict):
        return False
    for err in error.get("errors") or []:
        if isinstance(err, dict) and err.get("reason") in RATE_LIMIT_REASONS:
            return True
    return False


def upload(
    *,
    creds: Credentials,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    privacy: str = "private",
    category_id: str = CATEGORY_FILM_ANIMATION,
    playlist_id: str | None = None,
    progress_callback: Callable[[float], None] | None = None,
) -> str:
    """Upload one mp4, optionally add it to a playlist, return the video id.

    Raises :class:`RateLimitError` on quota/429, :class:`UploadError` on other API errors.
    """
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True, chunksize=CHUNKSIZE)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    try:
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                if progress_callback:
                    progress_callback(status.progress())
                logger.debug("upload progress: {:.1f}%", status.progress() * 100)
        video_id = response["id"]
    except HttpError as e:
        if _is_rate_limit(e):
            retry_after = e.resp.get("Retry-After")
            raise RateLimitError(f"YouTube rate limit: {e}", int(retry_after) if retry_after else None) from e
        raise UploadError(f"YouTube API error ({e.resp.status}): {e}") from e

    logger.info("uploaded video_id={}", video_id)

    if playlist_id:
        try:
            youtube.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id},
                    }
                },
            ).execute()
            logger.info("added to playlist {}", playlist_id)
        except HttpError as e:  # playlist failure is non-fatal - the video is up.
            logger.warning("playlist insert failed for {}: {}", video_id, e)

    return video_id


__all__ = [
    "AuthError",
    "RateLimitError",
    "UploadError",
    "inspect_token",
    "load_or_refresh",
    "run_oauth_flow",
    "upload",
]
