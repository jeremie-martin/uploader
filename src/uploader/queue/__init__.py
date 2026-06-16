"""Pluggable buffer backends.

The scheduler only ever talks to the :class:`~uploader.queue.base.Queue` interface, so
the choice of buffer (a local inbox on the Pi, or an object-storage bucket for
off-network machines / deep buffers) is a *config* decision, not a code change. Multiple
backends can be drained at once.
"""

from uploader.queue.base import BundleRef, LocalBundle, Queue
from uploader.queue.local import LocalQueue

__all__ = ["BundleRef", "LocalBundle", "LocalQueue", "Queue", "build_backends"]


class BackendConfigError(ValueError):
    """A queue backend config entry is missing a required option or has an unknown one."""


# Per-kind option schema: required options and the full set of allowed options. Validated
# up front so a config typo (e.g. `buckeet`, or a missing `inbox`) yields a clear message
# naming the backend, instead of a raw KeyError/TypeError crashing the whole tick.
_BACKEND_OPTIONS: dict[str, tuple[set[str], set[str]]] = {
    # kind: (required, optional)
    "local": ({"inbox"}, set()),
    "objectstore": (
        {"bucket"},
        {"prefix", "endpoint_url", "region", "aws_access_key_id", "aws_secret_access_key"},
    ),
}


def _validated_options(spec) -> dict:
    if spec.kind not in _BACKEND_OPTIONS:
        raise BackendConfigError(
            f"unknown queue backend kind {spec.kind!r} (known: {sorted(_BACKEND_OPTIONS)})"
        )
    required, optional = _BACKEND_OPTIONS[spec.kind]
    keys = set(spec.options)
    missing = required - keys
    if missing:
        raise BackendConfigError(f"backend {spec.kind!r} is missing required option(s): {sorted(missing)}")
    unknown = keys - required - optional
    if unknown:
        raise BackendConfigError(
            f"backend {spec.kind!r} has unknown option(s): {sorted(unknown)} "
            f"(allowed: {sorted(required | optional)})"
        )
    return dict(spec.options)


def build_backends(specs, settle_seconds: float = 5.0) -> list[Queue]:
    """Instantiate queue backends from a list of :class:`~uploader.config.BackendSpec`."""
    backends: list[Queue] = []
    for spec in specs:
        opts = _validated_options(spec)
        if spec.kind == "local":
            backends.append(LocalQueue(settle_seconds=settle_seconds, **opts))
        elif spec.kind == "objectstore":
            from uploader.queue.objectstore import ObjectStoreQueue

            backends.append(ObjectStoreQueue(settle_seconds=settle_seconds, **opts))
    return backends
