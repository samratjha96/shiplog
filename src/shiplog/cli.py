"""ShipLog CLI — container update reports powered by AI."""

import json as json_mod
import os
import sqlite3
import sys
from datetime import datetime, timezone

import click
import httpx

from shiplog import __version__, db
from shiplog.analyzer import analyze
from shiplog.changelog import Changelog, fetch_changelog
from shiplog.diun import DiunParseError, parse_env, split_image_ref


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


@cli.command("test-ingest")
@click.argument("image_ref")
@click.option("--status", default="update", help="Status: 'new' or 'update'.")
@click.pass_context
def test_ingest(ctx: click.Context, image_ref: str, status: str) -> None:
    """Manually ingest an image for testing (no diun needed).

    IMAGE_REF is like 'docker.io/crazymax/diun:v4.31.0'
    """
    # Split image:tag — handle port numbers (e.g. registry.local:5000/app:v1)
    image, tag = split_image_ref(image_ref)

    # Auto-generate hub_link
    hub_link = _generate_hub_link(image)

    conn = _connect(ctx)
    row_id = db.insert_update(
        conn,
        image=image,
        tag=tag,
        status=status,
        hub_link=hub_link,
    )
    click.echo(f"Ingested: {image}:{tag} ({status}) → id={row_id}")


# _split_image_ref kept as alias for test compatibility
_split_image_ref = split_image_ref


def _generate_hub_link(image: str) -> str | None:
    """Generate a registry link for an image."""
    # Docker Hub
    for prefix in ("docker.io/", "index.docker.io/"):
        if image.startswith(prefix):
            name = image[len(prefix):]
            if "/" in name:
                return f"https://hub.docker.com/r/{name}"
            return None

    # GitHub Container Registry
    if image.startswith("ghcr.io/"):
        path = image.removeprefix("ghcr.io/")
        return f"https://github.com/{path}/pkgs/container/{path.split('/')[-1]}"

    return None


@cli.command("list")
@click.option("--all", "show_all", is_flag=True, help="Show all updates, not just pending.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON (for scripting).")
@click.pass_context
def list_updates(ctx: click.Context, show_all: bool, as_json: bool) -> None:
    """List pending (unreported) updates."""
    conn = _connect(ctx)
    rows = db.get_all_updates(conn) if show_all else db.get_pending_updates(conn)

    if as_json:
        items = [
            {
                "id": row["id"],
                "image": row["image"],
                "tag": row["tag"],
                "status": row["status"],
                "reported": bool(row["reported"]),
                "ingested_at": row["ingested_at"],
            }
            for row in rows
        ]
        click.echo(json_mod.dumps(items, indent=2))
        return

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
        from pathlib import Path
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
    else:
        click.echo("\n(Dry run — updates not marked as reported.)", err=True)


@cli.command("reports")
@click.pass_context
def list_reports(ctx: click.Context) -> None:
    """List all generated reports."""
    conn = _connect(ctx)
    rows = db.get_all_reports(conn)

    if not rows:
        click.echo("No reports generated yet. Run 'shiplog report' to create one.")
        return

    for row in rows:
        click.echo(f"  [{row['id']}] {row['created_at'][:19]}  model={row['model']}")


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
    # Report content already includes its own header, just print it
    click.echo(row["content"])
    click.echo(f"\n---\nReport #{report_id} | Generated {row['created_at'][:19]} | Model: {row['model']}", err=True)


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
@click.argument("image")
@click.pass_context
def unmap(ctx: click.Context, image: str) -> None:
    """Remove a GitHub repo mapping for an image."""
    conn = _connect(ctx)
    existing = db.get_github_mapping(conn, image)
    if not existing:
        click.echo(f"No mapping found for {image}.", err=True)
        sys.exit(1)
    db.delete_github_mapping(conn, image)
    click.echo(f"Removed mapping: {image} → {existing}")


@cli.command()
@click.option("--yes", is_flag=True, help="Skip confirmation.")
@click.pass_context
def purge(ctx: click.Context, yes: bool) -> None:
    """Delete all reported updates from the database.

    Keeps pending (unreported) updates and all reports.
    """
    conn = _connect(ctx)
    count = conn.execute("SELECT COUNT(*) as n FROM updates WHERE reported = 1").fetchone()["n"]

    if count == 0:
        click.echo("Nothing to purge — no reported updates.")
        return

    if not yes:
        click.confirm(f"Delete {count} reported update(s)?", abort=True)

    conn.execute("DELETE FROM updates WHERE reported = 1")
    conn.commit()
    click.echo(f"Purged {count} reported update(s).")


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON (for scripting).")
@click.pass_context
def status(ctx: click.Context, as_json: bool) -> None:
    """Show ShipLog status and configuration."""
    db_path = db.get_db_path(ctx.obj.get("db_path"))
    conn = _connect(ctx)

    pending = db.get_pending_updates(conn)
    all_updates = db.get_all_updates(conn)
    all_mappings = db.get_all_github_mappings(conn)
    all_reports = db.get_all_reports(conn)

    last_report = all_reports[0]["created_at"][:19] if all_reports else None

    if as_json:
        data = {
            "database": str(db_path),
            "total_updates": len(all_updates),
            "pending": len(pending),
            "reports": len(all_reports),
            "last_report": last_report,
            "mappings": len(all_mappings),
            "llm_api_key": bool(os.environ.get("LLM_API_KEY")),
            "github_token": bool(os.environ.get("GITHUB_TOKEN")),
            "pending_images": sorted({row["image"] for row in pending}),
        }
        click.echo(json_mod.dumps(data, indent=2))
        return

    click.echo("ShipLog Status")
    click.echo("=" * 40)
    click.echo(f"  Database:       {db_path}")
    click.echo(f"  Total updates:  {len(all_updates)}")
    click.echo(f"  Pending:        {len(pending)}")
    click.echo(f"  Reports:        {len(all_reports)}")
    click.echo(f"  Last report:    {last_report or 'never'}")
    click.echo(f"  Mappings:       {len(all_mappings)}")
    click.echo(f"  LLM API key: {'✅ set' if os.environ.get('LLM_API_KEY') else '❌ not set'}")
    click.echo(f"  GitHub token:   {'✅ set' if os.environ.get('GITHUB_TOKEN') else '⚠️  not set (60 req/hr limit)'}")

    if pending:
        images = sorted({row['image'] for row in pending})
        click.echo(f"\nPending images:")
        for img in images:
            click.echo(f"  • {img}")
