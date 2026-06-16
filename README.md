# uploader

One generic YouTube upload service shared by every video-generation project. A project
produces a **finished video + a small `upload.json` sidecar**; the uploader resolves the
title/description/tags from a central per-project pool config + per-video values, and
publishes one video at a time on a per-project cadence to a single shared channel.

It **never touches video frames** - all rendering/composition stays on the generation
side. This is the deliberate line that the older per-project uploaders
(`double-pendulum`, `lpt2d`, `motiontwin`) blurred.

## The contract (what a project produces)

A **bundle** = a finished video + an `upload.json` sidecar, in a directory (local
backend) or under an object-store prefix (cloud backend). Write/upload the sidecar
**last** - it's the "ready" sentinel.

```json
{
  "project": "double-pendulum",
  "values": { "count": 1000000, "boom_time": "12s" },
  "overrides": { "title": "...", "tags": ["..."], "playlist": "PL...", "privacy": "public" },
  "video": "video.mp4",
  "created_at": "2026-06-16T12:00:00Z"
}
```

Only `project` is required. `values` feed the templates; `overrides` (any subset) bypass
the engine for one-offs. If `video` is omitted, the lone video file in the bundle is used.

## Per-project pool config (`projects/<name>.toml`)

The clean replacement for the scattered title "pools". The uploader owns the *mechanism*;
each project owns its *content*:

```toml
playlist = "PL..."
privacy  = "public"
cadence  = "2h"            # this project uploads at most once every 2h

[title]
templates     = ["{count|human} double pendulums, one tiny difference", "Chaos Theory Visualized"]
hashtags      = ["chaos", "physics", "satisfying"]
hashtag_count = [0, 2]

[description]
templates = ["{count|human} identical starts. Complete chaos."]

tags = ["double pendulum", "chaos theory"]

[[tags_when]]              # value-conditioned tags
when = "count >= 1000000"
add  = ["million pendulums", "1 million"]
```

Template tokens: `{key}`, `{key|filter}`, `{key|f1|f2}`. Built-in filters: `human`
(`1000000` → `1 Million`), `comma` (`1,000,000`), `upper`, `lower`, `title`, `int`.
`tags_when` conditions support `> >= < <= == != in not in`, `and`/`or`/`not`.

## Commands

```bash
uv run uploader auth                 # one-time: OAuth flow -> token.pickle
uv run uploader status               # token health + backends + pending counts (no upload)
uv run uploader projects             # list + validate per-project pool configs
uv run uploader preview <bundle-dir> # resolve metadata for a bundle without uploading
uv run uploader preview <dir> --samples 5   # browse random pool variations
uv run uploader tick                 # run one scheduler tick (the cron/timer entry)
uv run uploader tick --dry-run       # select + resolve, but don't upload
uv run uploader ledger -n 20         # recent uploads
```

## Credentials & token

A single OAuth `token.pickle` (one shared channel) lives in `credentials_dir`. Token
handling is a side-effect of the oneshot design and needs almost no babysitting:

- **Auto-refresh.** Each tick is a fresh process that re-reads `token.pickle` from disk
  and refreshes the 1-hour access token in place when expired. No daemon holds stale
  creds in memory.
- **Rotate with no restart.** Drop a new `token.pickle` into `credentials_dir` and the
  next tick uses it - nothing to reload or restart. `uploader status` shows current
  health.
- **If the refresh token dies** (revoked, or the weekly Testing-mode expiry below), a
  tick logs the problem, returns a non-zero "needs auth" code, and *keeps* the bundle.
  Uploads pause safely and resume automatically once you re-auth - no crash, no loss.

**Stop the weekly re-auth (do this once):** Google expires refresh tokens after 7 days
for OAuth apps in **"Testing"** publishing status (`youtube.upload` is a sensitive
scope). In Google Cloud Console → *OAuth consent screen* → **Publish app** ("In
production"). Refresh tokens then no longer expire weekly - you auth once.

**Headless Pi:** the browser OAuth flow is awkward on a headless host, so run
`uploader auth` on a desktop and `scp credentials/token.pickle` to the Pi's
`credentials_dir`. With the app published, that's a one-time copy. Running the uploader
on an always-on **desktop** instead avoids the scp entirely (`uploader auth` writes the
token directly in place) and is the leanest setup to start with.

## Deployment

The scheduler and the buffer are decoupled (backends are config, not code), so you can
start simple and grow:

- **Phase 1 - Pi as uploader + datastore.** Run the uploader on the always-on Pi with a
  `local` backend; on-network generators rsync bundles into the inbox. Free, no cloud.
- **Phase 2 - videos in the cloud.** Add an `objectstore` backend (Backblaze B2 / Cloudflare
  R2) for off-network machines or buffers larger than the Pi's disk. Off-network generators
  push to the bucket; the uploader downloads one video at a time. Both backends can run at
  once during the transition. **No code change** - just config. Durable state (ledger,
  cadence clock) stays on the Pi throughout; only the video buffer relocates.

Install the timer (model: `systemd/user/uploader.{timer,service}`):

```bash
cp config.toml.example ~/.local/share/uploader/config.toml   # then edit
uv run uploader auth
cp systemd/user/uploader.{timer,service} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now uploader.timer
loginctl enable-linger "$USER"        # so the timer runs at boot without an active login
```

The timer fires every ~15 min (poll granularity); the real per-project rate is each
project's `cadence`, enforced inside the tick. `enable-linger` is the one piece that
makes it survive reboots / logouts - without it, user timers only run while you're
logged in. Check it's alive with `systemctl --user list-timers uploader.timer` and
`uploader status`.

## Crash safety

Per upload, the commit order is: write `uploaded` marker (fsync) → append ledger (dedup
by youtube id) → advance the project's cadence clock → remove the bundle. A crash between
any steps is recovered on the next tick (finalize-not-reupload), so a video is never
uploaded twice.

## Develop

```bash
uv venv && uv sync --extra dev --extra objectstore
uv run ruff check .
uv run pytest -q
```
