"""uploader - a generic, multi-project YouTube upload service.

A video project hands the uploader a *finished* video plus a small ``upload.json``
sidecar (the contract). The uploader resolves title/description/tags from a central
per-project pool config + per-video values, then publishes one video at a time on a
per-project cadence to a single shared channel. It never touches video frames.
"""

__version__ = "0.1.0"
