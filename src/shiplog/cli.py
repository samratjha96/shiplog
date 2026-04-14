"""ShipLog CLI — container update reports powered by AI."""

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import httpx
from dotenv import load_dotenv

# Load .env before anything reads env vars
load_dotenv()

from shiplog import __version__, db
from shiplog.analyzer import analyze
from shiplog.changelog import Changelog, fetch_changelog
from shiplog.diun import DiunParseError, parse_env, split_image_ref
from shiplog import ntfy


def _connect(ctx: click.Context) -> sqlite3.Connection:
    """Get or create the DB connection from click context."""
    if "conn" not in ctx.ensure_object(dict):
        db_path = db.get_db_path(ctx.obj.get("db_path"))
        ctx.obj["conn"] = db.connect(db_path)
    return ctx.obj["conn"]


@click.group()
@click.version_option(version=__version__, prog_name="shiplog")
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
@click.option("-o", "--output", "output_path", default=None, type=click.Path(),
              help="Write report to a file instead of stdout.")
@click.pass_context
def report(ctx: click.Context, dry_run: bool, model: str | None, output_path: str | None) -> None:
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
        # Deduplicate by image — use the latest (last-ingested) tag per image
        latest_by_image: dict[str, str] = {}
        for row in pending:
            latest_by_image[row["image"]] = row["tag"]

        for image, tag in latest_by_image.items():
            click.echo(f"  Fetching changelog for {image}:{tag}...", err=True)
            try:
                cl = fetch_changelog(client, conn, image, tag)
            except Exception as e:
                click.echo(f"  ⚠️  Failed to fetch changelog for {image}: {e}", err=True)
                cl = Changelog(
                    image=image,
                    github_repo=None,
                    releases=[],
                    tag=tag,
                    error=f"Changelog fetch failed: {e}",
                )
            changelogs.append(cl)

    if not changelogs:
        click.echo("No changelogs to analyze.")
        return

    # Show summary: which images resolved, which didn't
    resolved = [cl for cl in changelogs if cl.github_repo and not cl.error]
    unresolved = [cl for cl in changelogs if not cl.github_repo]
    no_releases = [cl for cl in changelogs if cl.github_repo and cl.error]

    if resolved:
        click.echo(f"  ✅ {len(resolved)} image(s) with changelogs", err=True)
    if no_releases:
        click.echo(f"  ⚠️  {len(no_releases)} image(s) with no releases found", err=True)
    if unresolved:
        click.echo(f"  ❌ {len(unresolved)} image(s) with no GitHub mapping", err=True)
        click.echo("  Hint: add mappings to get changelogs for these images:", err=True)
        for cl in unresolved:
            click.echo(f"    shiplog map {cl.image} <owner/repo>", err=True)

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
    except httpx.TimeoutException:
        click.echo("Error: LLM API request timed out. Try a faster model with --model.", err=True)
        sys.exit(1)
    except httpx.HTTPError as e:
        click.echo(f"Error: LLM API request failed: {e}", err=True)
        sys.exit(1)

    # Output report
    header = (
        f"# ShipLog Report — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        f"*Model: {model_used}*\n\n"
    )
    full_report = header + content

    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(full_report + "\n")
        click.echo(f"Report written to {output_path}", err=True)
    else:
        click.echo(full_report)

    # Save and mark reported (unless dry run)
    if not dry_run:
        report_id = db.insert_report(conn, model=model_used, content=full_report)
        update_ids = [row["id"] for row in pending]
        db.mark_reported(conn, update_ids, report_id)
        click.echo(f"\nReport saved (id={report_id}). {len(update_ids)} update(s) marked as reported.", err=True)

        # Send notification
        if ntfy.is_configured():
            try:
                ntfy.send(full_report)
                click.echo("Notification sent via ntfy.", err=True)
            except httpx.HTTPError as e:
                click.echo(f"\u26a0\ufe0f  ntfy notification failed: {e}", err=True)
    else:
        click.echo("\n(Dry run — updates not marked as reported.)", err=True)


@cli.command("map")
@click.argument("image", required=False)
@click.argument("github_repo", required=False)
@click.pass_context
def map_image(ctx: click.Context, image: str | None, github_repo: str | None) -> None:
    """Map a container image to its GitHub repo, or list all mappings.

    \b
    With no arguments, lists all current mappings.
    With IMAGE and GITHUB_REPO, creates a new mapping.

    Example: shiplog map docker.io/linuxserver/sonarr linuxserver/docker-sonarr
    """
    conn = _connect(ctx)

    # No args: list mappings
    if image is None:
        rows = db.get_all_github_mappings(conn)
        if not rows:
            click.echo("No mappings configured. Use 'shiplog map <image> <owner/repo>' to add one.")
            return
        for row in rows:
            auto = " (auto)" if row["auto_detected"] else ""
            click.echo(f"  {row['image']} → {row['github_repo']}{auto}")
        return

    # One arg without the other
    if github_repo is None:
        click.echo("Usage: shiplog map <image> <owner/repo>", err=True)
        sys.exit(1)

    if "/" not in github_repo or len(github_repo.split("/")) != 2:
        click.echo("Error: github_repo must be 'owner/repo' format.", err=True)
        sys.exit(1)

    db.set_github_mapping(conn, image, github_repo, auto_detected=False)
    click.echo(f"Mapped: {image} → https://github.com/{github_repo}")


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

    last_report = all_reports[0]["created_at"][:19] if all_reports else None

    click.echo("ShipLog Status")
    click.echo("=" * 40)
    click.echo(f"  Database:       {db_path}")
    click.echo(f"  Total updates:  {len(all_updates)}")
    click.echo(f"  Pending:        {len(pending)}")
    click.echo(f"  Reports:        {len(all_reports)}")
    click.echo(f"  Last report:    {last_report or 'never'}")
    click.echo(f"  Mappings:       {len(all_mappings)}")
    click.echo(f"  LLM API URL:    {'✅ set' if os.environ.get('LLM_API_URL') else '❌ not set'}")
    click.echo(f"  LLM API key:    {'✅ set' if os.environ.get('LLM_API_KEY') else '❌ not set'}")
    click.echo(f"  GitHub token:   {'✅ set' if os.environ.get('GITHUB_TOKEN') else '⚠️  not set (60 req/hr limit)'}")
    click.echo(f"  ntfy:           {'✅ ' + os.environ.get('NTFY_TOPIC', '') if ntfy.is_configured() else '⚠️  not configured'}")

    if pending:
        images = sorted({row['image'] for row in pending})
        click.echo(f"\nPending images:")
        for img in images:
            click.echo(f"  • {img}")
