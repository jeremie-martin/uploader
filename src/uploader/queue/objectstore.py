"""Object-storage queue backend - S3-compatible (Backblaze B2 / Cloudflare R2 / S3).

For off-network generators and deep buffers beyond the Pi's disk. The bucket is the
queue + the durable buffer; the uploader downloads exactly one video at a time, so the
host stays disk-light. Generators upload ``<prefix>/<bundle_id>/{video, upload.json}``,
with ``upload.json`` PUT **last** as the ready sentinel.

Credentials come from the standard boto3 chain (env / shared config) or the explicit
``aws_access_key_id`` / ``aws_secret_access_key`` options. Point ``endpoint_url`` at B2
(``https://s3.<region>.backblazeb2.com``) or R2 (``https://<acct>.r2.cloudflarestorage.com``).
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3
from loguru import logger

from uploader.queue.base import (
    MARKER_FAILED,
    MARKER_UPLOADED,
    SIDECAR_NAME,
    BundleRef,
    LocalBundle,
    Queue,
    select_video,
)


class ObjectStoreQueue(Queue):
    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "inbox",
        endpoint_url: str | None = None,
        region: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        settle_seconds: float = 5.0,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.settle_seconds = settle_seconds
        self.name = f"objectstore:{bucket}/{self.prefix}"
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )

    def _key(self, bundle_id: str, name: str) -> str:
        return f"{self.prefix}/{bundle_id}/{name}"

    def _list_objects(self) -> dict[str, dict[str, Any]]:
        """Group objects into bundles -> {name: {size, mtime}}, scanned recursively.

        A bundle is any "directory" (key prefix) holding an ``upload.json`` object,
        at any depth under the prefix, mirroring the recursive local backend. A bundle's
        files are the objects sitting directly in that same prefix. bundle_id is the
        bundle's path relative to ``self.prefix`` (may contain slashes)."""
        base = f"{self.prefix}/"
        objs: dict[str, dict[str, Any]] = {}
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=base):
            for obj in page.get("Contents", []):
                objs[obj["Key"]] = {"size": obj["Size"], "mtime": obj["LastModified"].timestamp()}

        bundles: dict[str, dict[str, Any]] = {}
        for key in objs:
            if key.rsplit("/", 1)[-1] != SIDECAR_NAME:
                continue
            bundle_dir = key[: -len(SIDECAR_NAME)].rstrip("/")  # full key prefix of the bundle
            bundle_id = bundle_dir[len(base) :]
            if not bundle_id:
                continue  # stray sidecar at the root prefix; ignore
            files = {k.rpartition("/")[2]: meta for k, meta in objs.items() if k.rpartition("/")[0] == bundle_dir}
            bundles[bundle_id] = files
        return bundles

    def _get_json(self, bundle_id: str, name: str) -> dict[str, Any] | None:
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=self._key(bundle_id, name))
        except self._client.exceptions.NoSuchKey:
            return None
        try:
            return json.loads(resp["Body"].read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def list_ready(self) -> list[BundleRef]:
        now = time.time()
        refs: list[BundleRef] = []
        for bundle_id, files in self._list_objects().items():
            if MARKER_FAILED in files:
                continue
            if SIDECAR_NAME not in files:
                continue
            resumed = MARKER_UPLOADED in files
            if not resumed and (now - files[SIDECAR_NAME]["mtime"]) < self.settle_seconds:
                continue
            sidecar = self._get_json(bundle_id, SIDECAR_NAME)
            if not sidecar or not sidecar.get("project"):
                logger.warning("skipping {}: missing/invalid {}", bundle_id, SIDECAR_NAME)
                continue
            marker = self._get_json(bundle_id, MARKER_UPLOADED) if resumed else None
            created = files[SIDECAR_NAME]["mtime"]
            ca = sidecar.get("created_at")
            if ca:
                try:
                    created = datetime.fromisoformat(ca.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
            refs.append(
                BundleRef(
                    backend=self,
                    bundle_id=bundle_id,
                    project=sidecar["project"],
                    created_at=created,
                    sidecar=sidecar,
                    uploaded_marker=marker,
                )
            )
        return refs

    def fetch(self, ref: BundleRef, dest_dir: Path) -> LocalBundle:
        files = self._list_objects().get(ref.bundle_id, {})
        video_name = select_video(list(files.keys()), ref.sidecar)
        if video_name is None:
            raise FileNotFoundError(f"no video object in bundle {ref.bundle_id}")
        dest_dir.mkdir(parents=True, exist_ok=True)
        local_video = dest_dir / video_name
        logger.info("downloading {}/{} -> {}", ref.bundle_id, video_name, local_video)
        self._client.download_file(self.bucket, self._key(ref.bundle_id, video_name), str(local_video))
        return LocalBundle(ref=ref, video_path=local_video, sidecar=ref.sidecar)

    def mark_uploaded(self, ref: BundleRef, record: dict[str, Any]) -> None:
        self._client.put_object(
            Bucket=self.bucket,
            Key=self._key(ref.bundle_id, MARKER_UPLOADED),
            Body=json.dumps(record).encode("utf-8"),
        )

    def mark_failed(self, ref: BundleRef, reason: str) -> None:
        self._client.put_object(
            Bucket=self.bucket,
            Key=self._key(ref.bundle_id, MARKER_FAILED),
            Body=json.dumps({"reason": reason}).encode("utf-8"),
        )

    def remove(self, ref: BundleRef) -> None:
        files = self._list_objects().get(ref.bundle_id, {})
        objects = [{"Key": self._key(ref.bundle_id, name)} for name in files]
        if objects:
            self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": objects})


__all__ = ["ObjectStoreQueue"]
