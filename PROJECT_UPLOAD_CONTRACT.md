# Project upload contract

This is the interface between any video-generation project and this uploader.

The short version: a project renders a finished video, puts it in a bundle, writes an
`upload.json` sidecar last, and uses a `project` name that matches
`projects/<project>.toml`. The uploader does the rest: queue scanning, metadata
resolution, cadence, YouTube upload, ledgering, and cleanup.

The uploader does not render, transcode, compose, edit, inspect content decisions, or
choose project-specific values. It only publishes completed videos.

## Terms

- Project: the stable name for a generator, for example `line`. It must match a file
  named `projects/line.toml`.
- Bundle: one completed video plus one `upload.json` sidecar in one directory or object
  prefix.
- Bundle ID: the bundle path relative to the configured inbox or object-store prefix.
  It is unique inside that backend and may contain `/` for organization.
- Sidecar: `upload.json`. Its presence means the bundle is ready to be considered.
- Pool config: `projects/<project>.toml`, owned by the project. It contains title,
  description, tags, playlist, privacy, category, cadence, and optional upload-order
  rules.

## Fast path: use the staging command

If the generator can call this uploader on the same machine as a local inbox, prefer
`uploader stage`. It validates the project config, creates one bundle per video, links
or copies the video, and writes `upload.json` last.

```bash
uv run uploader stage ./out/video.mp4 \
  --project line \
  -V seed=12345 \
  -V rep=0 \
  -M git_sha="$(git rev-parse --short HEAD)" \
  --privacy public
```

From another project directory, use `uv --project /path/to/uploader run uploader ...`.

`uploader stage` names bundles like:

```text
<project>-<video-stem>-<YYYYMMDDTHHMMSS>-<index>
```

Use `--copy` when hardlinking is not appropriate, such as across filesystems. Use
`--inbox /path/to/inbox` to target a specific local inbox instead of the first local
backend from `config.toml`.

For object storage, or for generators that cannot run the uploader CLI, implement the
bundle contract directly.

## 1. Register the project

Create `projects/<project>.toml`. The `<project>` file stem is the project name used in
`upload.json`.

Recommended minimum:

```toml
playlist = "PL..."
privacy = "public"
category_id = "1"
cadence = "1h"
upload_order = "random"

tags = ["line rider", "procedural generation", "satisfying"]

[title]
templates = [
    "Line Rider synced to music",
    "Auto-generated track, seed {seed}",
]
hashtags = ["linerider", "procedural", "satisfying"]
hashtag_count = [0, 2]

[description]
templates = [
    "Procedurally generated Line Rider. Seed {seed}.",
]

[[tags_when]]
when = "rep == 0"
add = ["first take"]
```

Top-level keys must appear before `[title]`, `[description]`, or `[[tags_when]]`.
Unknown keys are rejected by `uploader projects`, so typos should fail loudly.

Project config keys:

| Key | Meaning |
| --- | --- |
| `playlist` | Optional YouTube playlist ID. Omit or use `""` for no playlist insert. |
| `privacy` | `private`, `unlisted`, or `public`. Defaults to `private`. |
| `category_id` | YouTube category ID as a string. Defaults to `"1"` (Film & Animation). |
| `cadence` | Minimum time between successful uploads for this project, such as `40m`, `2h`, `1d`, or seconds. |
| `upload_order` | Optional bundle selection within this project: `first`, `last`, or `random`. If omitted, the global order is used. |
| `tags` | Always-on YouTube tags. Templates are allowed. |
| `[title].templates` | Candidate title templates. One is selected deterministically from the bundle ID. |
| `[title].hashtags` | Hashtags that may be appended to the title. Do not include `#`. |
| `[title].hashtag_count` | `[min, max]` number of title hashtags to sample. |
| `[description].templates` | Candidate description templates. One is selected deterministically from the bundle ID. |
| `[[tags_when]]` | Conditional tags. Each rule has `when` and `add`. |

Template tokens use sidecar `values`:

```text
{key}
{key|filter}
{key|filter1|filter2}
```

Built-in filters: `human`, `comma`, `upper`, `lower`, `title`, `int`.

`tags_when.when` supports comparisons (`>`, `>=`, `<`, `<=`, `==`, `!=`, `in`,
`not in`) plus `and`, `or`, and `not`.

Current bundled project value notes:

| Project | Sidecar `values` documented by that project config |
| --- | --- |
| `double-pendulum` | `count` integer, `boom_time` string |
| `lpt2d` | Optional, for example `rays` |
| `motiontwin` | `score` integer |
| `line` | `seed` integer, `rep` integer |

## 2. Produce one bundle per video

Local backend layout:

```text
<inbox>/
  <optional organization>/
    <bundle-id>/
      video.mp4
      upload.json
```

Object-store backend layout:

```text
s3://<bucket>/<prefix>/<optional organization>/<bundle-id>/video.mp4
s3://<bucket>/<prefix>/<optional organization>/<bundle-id>/upload.json
```

Rules:

- A bundle is any local directory, or object-store prefix, holding `upload.json`.
- The bundle ID is the path from the inbox or object-store prefix to that directory.
  Example: `line/2026/06/16/seed-12345`.
- Use one video per bundle. If there are multiple video files, set `video` explicitly,
  but the preferred contract is still one video file.
- Put the video directly in the bundle. Do not place it in a subdirectory.
- Use `.mp4` unless there is a reason not to. Recognized extensions are `.mp4`, `.mov`,
  `.webm`, `.mkv`, and `.m4v`.
- Do not create files named `uploaded` or `failed`; those are uploader-owned markers.
- Do not put a second `upload.json` inside a bundle. Local scanning does not descend
  into a bundle once it finds the first sidecar.

Recommended bundle ID format:

```text
<project>/<YYYY>/<MM>/<DD>/<project>-<run-or-seed>-<utc-timestamp>
```

Keep it filesystem and object-store safe: letters, numbers, `.`, `_`, `-`, and `/` are
enough. Do not use absolute paths or `..`. Do not reuse a bundle ID while an old bundle
with that ID still exists.

## 3. Write `upload.json`

Only `project` is required.

Minimal sidecar:

```json
{
  "project": "line"
}
```

Typical sidecar:

```json
{
  "project": "line",
  "video": "video.mp4",
  "created_at": "2026-06-16T12:00:00Z",
  "values": {
    "seed": 12345,
    "rep": 0
  },
  "meta": {
    "git_sha": "abc1234",
    "generator_host": "renderbox-1"
  },
  "overrides": {
    "privacy": "public"
  }
}
```

Sidecar fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `project` | Yes | Must match `projects/<project>.toml`. Unknown projects stay in the queue. |
| `video` | No | File name of the video inside the bundle. If omitted, the uploader uses the lone recognized video file. |
| `created_at` | No | ISO-8601 timestamp, preferably UTC with `Z`. Used for `first`/`last` upload ordering. If omitted or invalid, sidecar/object mtime is used. |
| `values` | No | JSON object used by title, description, tag, and `tags_when` templates. Also recorded in the ledger. |
| `meta` | No | JSON object recorded in the ledger only. It is never templated and never sent to YouTube. |
| `overrides` | No | JSON object that replaces resolved upload metadata for one-off uploads. |

Supported `overrides` keys:

| Key | Effect |
| --- | --- |
| `title` | Replaces the generated title. |
| `description` | Replaces the generated description. |
| `tags` | Replaces the generated tags. Must be a JSON array. |
| `playlist` | Replaces the project playlist for this video. |
| `privacy` | Replaces project privacy for this video. |
| `category_id` | Replaces project category for this video. |

Metadata precedence:

1. Load `projects/<project>.toml`.
2. Render title, description, and tags using sidecar `values`.
3. Apply matching `tags_when` rules.
4. Apply sidecar `overrides`.
5. Clamp to YouTube limits: title 100 chars, description 5000 chars, tags about 500 chars.

If a template references a missing value, the bundle is terminally failed and marked
with a `failed` file. Provide all values used by that project's templates.

## 4. Stage safely

`upload.json` is the ready sentinel. Write or upload it last.

The uploader also waits `settle_seconds` after the sidecar mtime before a fresh bundle
is eligible. The default is 5 seconds.

### Local inbox

Local scanning is recursive and skips dot-prefixed directories. A robust producer can
stage in a dot-prefixed temporary directory, then rename the complete directory into
place:

```bash
inbox="$HOME/.local/share/uploader/inbox"
bundle="line/2026/06/16/line-seed-12345-20260616T120000Z"
tmp="$inbox/.staging-line-seed-12345"
final="$inbox/$bundle"

rm -rf "$tmp"
mkdir -p "$tmp" "$(dirname "$final")"
cp ./out/video.mp4 "$tmp/video.mp4"

cat > "$tmp/upload.json" <<'JSON'
{
  "project": "line",
  "video": "video.mp4",
  "created_at": "2026-06-16T12:00:00Z",
  "values": { "seed": 12345, "rep": 0 },
  "meta": { "generator": "line" }
}
JSON

mv "$tmp" "$final"
```

If writing directly into the final directory, copy or rsync the video first, then move
`upload.json` into place as the last operation.

### Object store

Object-store scanning looks for `upload.json` anywhere under the configured prefix.
Upload all other objects first, then upload `upload.json` last:

```bash
bucket="my-video-buffer"
prefix="inbox"
bundle="line/2026/06/16/line-seed-12345-20260616T120000Z"

aws s3 cp ./out/video.mp4 "s3://$bucket/$prefix/$bundle/video.mp4"
aws s3 cp ./upload.json "s3://$bucket/$prefix/$bundle/upload.json"
```

For object storage, do not rely on dot-prefixed staging directories. Use a prefix the
uploader does not scan, or withhold `upload.json` until the bundle is complete.

## 5. Validate before upload

Run these from the uploader repo:

```bash
uv run uploader projects
uv run uploader preview /path/to/inbox/line/2026/06/16/line-seed-12345-20260616T120000Z
uv run uploader tick --dry-run
uv run uploader status
```

What each command proves:

- `projects`: every `projects/*.toml` parses and reports cadence, privacy, project
  upload order, playlist, title count, description count, and tag count.
- `preview`: reads one bundle sidecar and validates that title, description, tags,
  playlist, and privacy can be resolved without uploading.
- `tick --dry-run`: runs the exact scheduler selection and metadata resolution path
  without fetching or uploading the selected video.
- `status`: shows token health, global scheduler settings, and pending counts for each
  backend.

## 6. Runtime behavior project authors should know

- One tick uploads at most one fresh bundle total.
- Fresh selection first applies global `upload_order` (`first`, `last`, or `random`) to
  the full set of bundles whose project cadence is due, which determines the winning
  project for this tick. If that project defines `upload_order`, the project setting then
  chooses which queued bundle from that project uploads.
- Cadence is per project and advances only after a successful upload.
- Auth failures and YouTube rate limits keep the bundle for retry.
- Fetch failures, upload terminal failures, and metadata template errors mark the
  bundle with `failed` and append to `failed.jsonl`.
- After a successful upload, the uploader writes an `uploaded` marker before ledgering
  and cleanup. If the process crashes, the next tick finalizes that marker without
  re-uploading the video.
- Successful uploads are appended to `$UPLOADER_HOME/uploads.jsonl` with YouTube ID,
  URL, resolved metadata, sidecar `values`, sidecar `meta`, and best-effort media probe
  data.

## Generator checklist

Before handing off a video:

- `projects/<project>.toml` exists and `uv run uploader projects` accepts it.
- The video is complete and closed by the renderer.
- The bundle has exactly one direct video file, preferably `video.mp4`.
- `upload.json` is valid UTF-8 JSON.
- `upload.json.project` matches the project config file stem.
- `upload.json.values` contains every key referenced by that project's templates and
  `tags_when` rules.
- `upload.json.video` is present if the bundle contains more than one recognized video
  extension.
- `upload.json` is written or uploaded last.
- `uv run uploader preview <bundle-dir>` shows the expected title, description, tags,
  playlist, and privacy.
