"""``uploader`` command-line interface: auth | tick | preview | projects | ledger."""

from __future__ import annotations

import json
import random
import sys
import zlib
from pathlib import Path

import click
from loguru import logger

from uploader import engine, youtube
from uploader.config import load_global_config
from uploader.queue.base import SIDECAR_NAME
from uploader.state import State
from uploader.tick import run_tick


def _configure_logging(verbose: bool) -> None:
    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if verbose else "INFO", format="{time:HH:mm:ss} | {level: <7} | {message}")


@click.group()
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None, help="Path to config.toml.")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
@click.pass_context
def cli(ctx: click.Context, config_path: Path | None, verbose: bool) -> None:
    """Generic multi-project YouTube uploader."""
    _configure_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@cli.command()
@click.pass_context
def auth(ctx: click.Context) -> None:
    """Run the interactive YouTube OAuth flow and cache the token."""
    cfg = load_global_config(ctx.obj["config_path"])
    cfg.credentials_dir.mkdir(parents=True, exist_ok=True)
    youtube.run_oauth_flow(cfg.credentials_dir)
    click.echo(f"Token written under {cfg.credentials_dir}")


@cli.command()
@click.option("--dry-run", is_flag=True, help="Select + resolve metadata but do not upload.")
@click.pass_context
def tick(ctx: click.Context, dry_run: bool) -> None:
    """Run one scheduler tick (the cron/timer entry point)."""
    code = run_tick(ctx.obj["config_path"], dry_run=dry_run)
    sys.exit(code)


@cli.command()
@click.argument("bundle", type=click.Path(exists=True, path_type=Path))
@click.option("--samples", type=int, default=1, help="Show N random variations (>1 ignores the seed).")
@click.pass_context
def preview(ctx: click.Context, bundle: Path, samples: int) -> None:
    """Resolve metadata for a bundle dir (reads its upload.json) without uploading."""
    cfg = load_global_config(ctx.obj["config_path"])
    sidecar_path = bundle / SIDECAR_NAME if bundle.is_dir() else bundle
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    project = sidecar.get("project")
    if not project:
        raise click.ClickException(f"{sidecar_path} has no 'project'")
    pc = cfg.load_project(project)
    values = sidecar.get("values") or {}
    overrides = sidecar.get("overrides") or {}

    # Seed from the bundle directory name — the same bundle_id the tick seeds with — so a
    # single-sample preview reproduces exactly what the tick would pick.
    bundle_id = bundle.name if bundle.is_dir() else bundle.parent.name

    for i in range(max(1, samples)):
        rng = random.Random(zlib.crc32(bundle_id.encode())) if samples == 1 else random.Random()
        meta = engine.pick(pc, values, rng=rng, overrides=overrides)
        if samples > 1:
            click.echo(f"--- sample {i + 1} ---")
        click.echo(f"title:       {meta.title}")
        click.echo(f"description: {meta.description}")
        click.echo(f"tags:        {', '.join(meta.tags)}")
        click.echo(f"playlist:    {meta.playlist}")
        click.echo(f"privacy:     {meta.privacy}")


@cli.command(name="projects")
@click.pass_context
def projects_cmd(ctx: click.Context) -> None:
    """List and validate the per-project pool configs."""
    cfg = load_global_config(ctx.obj["config_path"])
    names = cfg.known_projects()
    if not names:
        click.echo(f"No project configs in {cfg.projects_dir}")
        return
    click.echo(f"Projects dir: {cfg.projects_dir}\n")
    for name in names:
        try:
            pc = cfg.load_project(name)
        except Exception as e:  # noqa: BLE001 - surface any config error per project
            click.echo(f"  {name}: ERROR {e}")
            continue
        click.echo(
            f"  {name}: cadence={pc.cadence_seconds}s privacy={pc.privacy} "
            f"playlist={pc.playlist or '-'} titles={len(pc.title.templates)} "
            f"descs={len(pc.description.templates)} tags={len(pc.tags)}"
        )


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show token health + configured backends + pending bundle counts (no upload)."""
    cfg = load_global_config(ctx.obj["config_path"])
    t = youtube.inspect_token(cfg.credentials_dir)
    if not t["present"]:
        click.echo(f"token:    MISSING ({cfg.credentials_dir}/token.pickle) — run `uploader auth`")
    elif "error" in t:
        click.echo(f"token:    UNREADABLE — {t['error']}")
    elif t["valid"]:
        click.echo(f"token:    OK (valid, expires {t['expiry']})")
    elif t["refreshable"]:
        click.echo(f"token:    stale but auto-refreshable (expired {t['expiry']}) — next tick refreshes it")
    else:
        click.echo("token:    EXPIRED and NOT refreshable — run `uploader auth` (publish the OAuth app to avoid this)")

    click.echo(f"projects: {len(cfg.known_projects())} ({', '.join(cfg.known_projects()) or '-'})")
    try:
        from uploader.queue import build_backends

        for b in build_backends(cfg.backends, settle_seconds=cfg.settle_seconds):
            refs = b.list_ready()
            ready = sum(1 for r in refs if not r.is_resumed)
            resumed = sum(1 for r in refs if r.is_resumed)
            click.echo(f"backend:  {b.name} — {ready} ready, {resumed} awaiting cleanup")
    except Exception as e:  # noqa: BLE001 - status must never crash
        click.echo(f"backend:  ERROR listing — {e}")


@cli.command()
@click.option("-n", "--limit", type=int, default=10, help="Show the most recent N uploads.")
@click.pass_context
def ledger(ctx: click.Context, limit: int) -> None:
    """Show recent uploads from the ledger."""
    cfg = load_global_config(ctx.obj["config_path"])
    rows = State(cfg.home).uploads()
    if not rows:
        click.echo("No uploads recorded yet.")
        return
    for r in rows[-limit:]:
        click.echo(f"{r.get('uploaded_at')}  {r.get('project'):20}  {r.get('youtube_url')}  {r.get('title')}")


def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
