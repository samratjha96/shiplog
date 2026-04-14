"""ShipLog CLI — container update reports powered by AI."""

import os
import sqlite3
import sys
from datetime import datetime, timezone

import click
import httpx

from shiplog import db
from shiplog.analyzer import analyze, build_prompt
from shiplog.changelog import Changelog, fetch_changelog
from shiplog.diun import DiunParseError, parse_env


def _connect(ctx: click.Context) -> sqlite3.Connection:
    """Get or create the DB connection from click context."""
    if "conn" not in ctx.ensure_object(dict):
        db_path = db.get_db_path(ctx.obj.get("db_path"))
        ctx.obj["conn"] = db.connect(db_path)
    return ctx.obj["conn"]


@click.group()
@click.option("--db", "db_path", envvar="SHIPLOG_DB_PATH", default=None,
              help="Path to SQLite database.")
@click.pass_context
def cli(ctx: click.Context, db_path: str | None) -> None:
    """ShipLog — AI-powered container update reports."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db_path


@cli.command()
@click.pass_context
def ingest(ctx: click.Context) -> None:
    """Ingest a container update from diun environment variables.

    Called by diun's script notifier. Reads DIUN_ENTRY_* env vars
    and writes the update to SQLite.
    """
    try:
        event = parse_env()
    except DiunParseError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    conn = _connect(ctx)
    row_id = db.insert_update(
        conn,
        image=event.image_name,
        tag=event.tag,
        digest=event.digest or None,
        status=event.status,
        hub_link=event.hub_link or None,
        platform=event.platform or None,
        provider=event.provider or None,
        created_at=event.created or None,
    )
    click.echo(f"Ingested: {event.image} ({event.status}) → id={row_id}")


@cli.command("test-ingest")
@click.argument("image_ref")
@click.option("--status", default="update", help="Status: 'new' or 'update'.")
@click.pass_context
def test_ingest(ctx: click.Context, image_ref: str, status: str) -> None:
    """Manually ingest an image for testing (no diun needed).

    IMAGE_REF is like 'docker.io/crazymax/diun:v4.31.0'
    """
    # Split image:tag
    if ":" in image_ref:
        image, tag = image_ref.rsplit(":", 1)
    else:
        image, tag = image_ref, "latest"

    conn = _connect(ctx)
    row_id = db.insert_update(
        conn,
        image=image,
        tag=tag,
        status=status,
    )
    click.echo(f"Ingested: {image}:{tag} ({status}) → id={row_id}")


@cli.command("list")
@click.option("--all", "show_all", is_flag=True, help="Show all updates, not just pending.")
@click.pass_context
def list_updates(ctx: click.Context, show_all: bool) -> None:
    """List pending (unreported) updates."""
    conn = _connect(ctx)
    rows = db.get_all_updates(conn) if show_all else db.get_pending_updates(conn)

    if not rows:
        label = "updates" if show_all else "pending updates"
        click.echo(f"No {label}.")
        return

    for row in rows:
        status_icon = "📦" if row["status"] == "new" else "🔄"
        reported = " ✅" if row["reported"] else ""
        click.echo(
            f"  {status_icon} [{row['id']}] {row['image']}:{row['tag']} "
            f"({row['status']}){reported}  — {row['ingested_at'][:19]}"
        )


@cli.command()
@click.option("--dry-run", is_flag=True, help="Generate report but don't mark updates as reported.")
@click.option("--model", default=None, help="LLM model to use.")
@click.pass_context
def report(ctx: click.Context, dry_run: bool, model: str | None) -> None:
    """Generate an AI-powered report for pending updates."""
    conn = _connect(ctx)
    pending = db.get_pending_updates(conn)

    if not pending:
        click.echo("Nothing new to report. All updates have been reported.")
        return

    click.echo(f"Found {len(pending)} pending update(s). Fetching changelogs...", err=True)

    # Fetch changelogs
    changelogs: list[Changelog] = []
    with httpx.Client(timeout=15.0) as client:
        # Deduplicate by image (may have multiple updates for same image)
        seen_images: set[str] = set()
        for row in pending:
            image = row["image"]
            if image in seen_images:
                continue
            seen_images.add(image)

            click.echo(f"  Fetching changelog for {image}:{row['tag']}...", err=True)
            cl = fetch_changelog(client, conn, image, row["tag"])
            changelogs.append(cl)

    if not changelogs:
        click.echo("No changelogs to analyze.")
        return

    # Call LLM
    click.echo("Analyzing with LLM...", err=True)
    try:
        content, model_used = analyze(changelogs, model=model)
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        click.echo(f"LLM API error: {e.response.status_code} — {e.response.text[:200]}", err=True)
        sys.exit(1)

    # Output report
    header = (
        f"# ShipLog Report — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        f"*Model: {model_used}*\n\n"
    )
    full_report = header + content

    click.echo(full_report)

    # Save and mark reported (unless dry run)
    if not dry_run:
        report_id = db.insert_report(conn, model=model_used, content=full_report)
        update_ids = [row["id"] for row in pending]
        db.mark_reported(conn, update_ids, report_id)
        click.echo(f"\nReport saved (id={report_id}). {len(update_ids)} update(s) marked as reported.", err=True)
    else:
        click.echo("\n(Dry run — updates not marked as reported.)", err=True)


@cli.command()
@click.argument("report_id", type=int)
@click.pass_context
def show(ctx: click.Context, report_id: int) -> None:
    """Show a previously generated report."""
    conn = _connect(ctx)
    row = db.get_report(conn, report_id)
    if not row:
        click.echo(f"Report {report_id} not found.", err=True)
        sys.exit(1)
    click.echo(row["content"])


@cli.command("map")
@click.argument("image")
@click.argument("github_repo")
@click.pass_context
def map_image(ctx: click.Context, image: str, github_repo: str) -> None:
    """Map a container image to its GitHub repo.

    Example: shiplog map docker.io/linuxserver/sonarr linuxserver/docker-sonarr
    """
    # Basic validation
    if "/" not in github_repo or len(github_repo.split("/")) != 2:
        click.echo("Error: github_repo must be 'owner/repo' format.", err=True)
        sys.exit(1)

    conn = _connect(ctx)
    db.set_github_mapping(conn, image, github_repo, auto_detected=False)
    click.echo(f"Mapped: {image} → https://github.com/{github_repo}")


@cli.command()
@click.pass_context
def mappings(ctx: click.Context) -> None:
    """Show all image → GitHub repo mappings."""
    conn = _connect(ctx)
    rows = db.get_all_github_mappings(conn)

    if not rows:
        click.echo("No mappings configured. Use 'shiplog map <image> <owner/repo>' to add one.")
        return

    for row in rows:
        auto = " (auto)" if row["auto_detected"] else ""
        click.echo(f"  {row['image']} → {row['github_repo']}{auto}")


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show ShipLog status and configuration."""
    db_path = db.get_db_path(ctx.obj.get("db_path"))
    conn = _connect(ctx)

    pending = db.get_pending_updates(conn)
    all_updates = db.get_all_updates(conn)
    all_mappings = db.get_all_github_mappings(conn)
    all_reports = db.get_all_reports(conn)

    click.echo("ShipLog Status")
    click.echo("=" * 40)
    click.echo(f"  Database:       {db_path}")
    click.echo(f"  Total updates:  {len(all_updates)}")
    click.echo(f"  Pending:        {len(pending)}")
    click.echo(f"  Reports:        {len(all_reports)}")
    click.echo(f"  Mappings:       {len(all_mappings)}")
    click.echo(f"  LLM API key: {'✅ set' if os.environ.get('LLM_API_KEY') else '❌ not set'}")
    click.echo(f"  GitHub token:   {'✅ set' if os.environ.get('GITHUB_TOKEN') else '⚠️  not set (60 req/hr limit)'}")
