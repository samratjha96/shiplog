"""SQLite state management for ShipLog."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def default_db_path() -> Path:
    """Return XDG-compliant default DB path."""
    xdg = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    return Path(xdg) / "shiplog" / "shiplog.db"


def get_db_path(override: str | None = None) -> Path:
    """Resolve DB path from override > env var > XDG default."""
    if override:
        return Path(override)
    env = os.environ.get("SHIPLOG_DB_PATH")
    if env:
        return Path(env)
    return default_db_path()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection and ensure schema exists."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _create_schema(conn)
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image TEXT NOT NULL,
            tag TEXT NOT NULL,
            digest TEXT,
            status TEXT NOT NULL,
            hub_link TEXT,
            platform TEXT,
            provider TEXT,
            created_at TEXT,
            ingested_at TEXT NOT NULL,
            reported INTEGER DEFAULT 0,
            report_id INTEGER,
            metadata TEXT
        );

        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            model TEXT NOT NULL,
            content TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS github_mappings (
            image TEXT PRIMARY KEY,
            github_repo TEXT NOT NULL,
            auto_detected INTEGER DEFAULT 0
        );
    """)


def insert_update(
    conn: sqlite3.Connection,
    *,
    image: str,
    tag: str,
    digest: str | None = None,
    status: str,
    hub_link: str | None = None,
    platform: str | None = None,
    provider: str | None = None,
    created_at: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Insert an update record. Returns the new row id."""
    now = datetime.now(timezone.utc).isoformat()
    meta_json = json.dumps(metadata) if metadata else None
    cur = conn.execute(
        """INSERT INTO updates
           (image, tag, digest, status, hub_link, platform, provider,
            created_at, ingested_at, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (image, tag, digest, status, hub_link, platform, provider,
         created_at, now, meta_json),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def get_pending_updates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all unreported updates, oldest first."""
    return conn.execute(
        "SELECT * FROM updates WHERE reported = 0 ORDER BY ingested_at ASC"
    ).fetchall()


def get_all_updates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all updates, newest first."""
    return conn.execute(
        "SELECT * FROM updates ORDER BY ingested_at DESC"
    ).fetchall()


def mark_reported(conn: sqlite3.Connection, update_ids: list[int], report_id: int) -> None:
    """Mark updates as reported, linking them to a report."""
    if not update_ids:
        return
    placeholders = ",".join("?" for _ in update_ids)
    conn.execute(
        f"UPDATE updates SET reported = 1, report_id = ? WHERE id IN ({placeholders})",
        [report_id, *update_ids],
    )
    conn.commit()


def insert_report(conn: sqlite3.Connection, *, model: str, content: str) -> int:
    """Insert a report. Returns the new row id."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO reports (created_at, model, content) VALUES (?, ?, ?)",
        (now, model, content),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def get_report(conn: sqlite3.Connection, report_id: int) -> sqlite3.Row | None:
    """Fetch a single report by id."""
    return conn.execute(
        "SELECT * FROM reports WHERE id = ?", (report_id,)
    ).fetchone()


def get_all_reports(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all reports, newest first."""
    return conn.execute(
        "SELECT id, created_at, model FROM reports ORDER BY created_at DESC"
    ).fetchall()


def set_github_mapping(
    conn: sqlite3.Connection,
    image: str,
    github_repo: str,
    auto_detected: bool = False,
) -> None:
    """Set or update an image → GitHub repo mapping."""
    conn.execute(
        """INSERT INTO github_mappings (image, github_repo, auto_detected)
           VALUES (?, ?, ?)
           ON CONFLICT(image) DO UPDATE SET github_repo = ?, auto_detected = ?""",
        (image, github_repo, int(auto_detected), github_repo, int(auto_detected)),
    )
    conn.commit()


def get_github_mapping(conn: sqlite3.Connection, image: str) -> str | None:
    """Look up the GitHub repo for an image. Returns 'owner/repo' or None."""
    row = conn.execute(
        "SELECT github_repo FROM github_mappings WHERE image = ?", (image,)
    ).fetchone()
    return row["github_repo"] if row else None


def get_all_github_mappings(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all GitHub mappings."""
    return conn.execute(
        "SELECT * FROM github_mappings ORDER BY image"
    ).fetchall()
