"""Fetch changelogs from GitHub Releases and resolve image→repo mappings."""

import os
import re
import sqlite3
from dataclasses import dataclass

import httpx

from shiplog import db

GITHUB_API = "https://api.github.com"
DOCKER_HUB_API = "https://hub.docker.com/v2"
USER_AGENT = "ShipLog/0.1 (container-update-reporter)"


@dataclass
class Changelog:
    """Changelog data for a container image update."""
    image: str
    github_repo: str | None  # owner/repo
    releases: list[dict]     # [{tag_name, name, body, published_at}, ...]
    error: str | None = None  # if we couldn't fetch


def _github_headers() -> dict[str, str]:
    """Build headers for GitHub API requests."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def validate_github_repo(client: httpx.Client, owner_repo: str) -> bool:
    """Confirm a GitHub repo exists by hitting the API. Returns True if 200."""
    try:
        resp = client.get(
            f"{GITHUB_API}/repos/{owner_repo}",
            headers=_github_headers(),
        )
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


# GitHub paths that look like owner/repo but aren't actual repositories.
_GITHUB_NON_REPO_PREFIXES = frozenset({
    "orgs", "settings", "marketplace", "features", "topics",
    "sponsors", "collections", "explore", "trending", "events",
    "about", "pricing", "security", "login", "signup", "apps",
})


def _extract_github_url(text: str) -> str | None:
    """Extract a github.com/owner/repo reference from text.

    Only matches explicit URLs — not arbitrary patterns.
    Rejects known non-repo paths like github.com/orgs/...
    """
    for match in re.finditer(
        r"https?://github\.com/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)", text
    ):
        owner = match.group(1)
        repo = match.group(2).rstrip("/")
        if repo.endswith(".git"):
            repo = repo[:-4]
        # Skip known non-repo URL patterns
        if owner.lower() in _GITHUB_NON_REPO_PREFIXES:
            continue
        return f"{owner}/{repo}"
    return None


def resolve_github_repo(
    client: httpx.Client,
    conn: sqlite3.Connection,
    image: str,
) -> str | None:
    """Resolve a container image to its GitHub repo.

    Strategy (in order):
    1. Check explicit user mappings in DB
    2. For ghcr.io images, try owner/repo from the image path
    3. Check Docker Hub description for GitHub URLs
    4. All candidates are validated via GitHub API before acceptance

    Returns 'owner/repo' or None.
    """
    # 1. Check DB mapping first
    mapping = db.get_github_mapping(conn, image)
    if mapping:
        if validate_github_repo(client, mapping):
            return mapping
        # User-set mapping is invalid — still return it so they know to fix it
        # Actually no: fail closed. Return None if we can't validate.
        return None

    # 2. ghcr.io — image path IS the repo path
    if image.startswith("ghcr.io/"):
        parts = image.removeprefix("ghcr.io/").split("/")
        if len(parts) >= 2:
            candidate = f"{parts[0]}/{parts[1]}"
            if validate_github_repo(client, candidate):
                db.set_github_mapping(conn, image, candidate, auto_detected=True)
                return candidate

    # 3. Docker Hub — scrape description for GitHub URL
    candidate = _try_docker_hub_description(client, image)
    if candidate and validate_github_repo(client, candidate):
        db.set_github_mapping(conn, image, candidate, auto_detected=True)
        return candidate

    return None


def _try_docker_hub_description(client: httpx.Client, image: str) -> str | None:
    """Try to find a GitHub repo URL in the Docker Hub description."""
    # Parse namespace/name from image
    # docker.io/library/nginx → library/nginx
    # docker.io/crazymax/diun → crazymax/diun
    # crazymax/diun → crazymax/diun
    img = image
    for prefix in ("docker.io/", "index.docker.io/", "registry-1.docker.io/"):
        if img.startswith(prefix):
            img = img[len(prefix):]
            break

    parts = img.split("/")
    if len(parts) == 1:
        # Official image like "nginx" → library/nginx
        namespace, name = "library", parts[0]
    elif len(parts) == 2:
        namespace, name = parts[0], parts[1]
    else:
        return None

    try:
        resp = client.get(
            f"{DOCKER_HUB_API}/repositories/{namespace}/{name}/",
            headers={"User-Agent": USER_AGENT},
            timeout=10.0,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        desc = data.get("full_description") or data.get("description") or ""
        return _extract_github_url(desc)
    except httpx.HTTPError:
        return None


def fetch_releases(
    client: httpx.Client,
    github_repo: str,
    per_page: int = 10,
) -> list[dict]:
    """Fetch recent releases from a GitHub repo.

    Returns a list of {tag_name, name, body, published_at} dicts.
    """
    try:
        resp = client.get(
            f"{GITHUB_API}/repos/{github_repo}/releases",
            headers=_github_headers(),
            params={"per_page": per_page},
        )
    except httpx.HTTPError:
        return []
    if resp.status_code != 200:
        return []

    releases = resp.json()
    return [
        {
            "tag_name": r.get("tag_name", ""),
            "name": r.get("name", ""),
            "body": r.get("body", ""),
            "published_at": r.get("published_at", ""),
        }
        for r in releases
    ]


def fetch_changelog(
    client: httpx.Client,
    conn: sqlite3.Connection,
    image: str,
    tag: str,
) -> Changelog:
    """Fetch changelog for a container image update.

    Resolves the GitHub repo, fetches releases, and filters
    to the most relevant ones for the given tag.
    """
    repo = resolve_github_repo(client, conn, image)
    if not repo:
        return Changelog(
            image=image,
            github_repo=None,
            releases=[],
            error=f"No GitHub repo found. Add mapping with: shiplog map {image} <owner/repo>",
        )

    releases = fetch_releases(client, repo)
    if not releases:
        return Changelog(
            image=image,
            github_repo=repo,
            releases=[],
            error=f"No releases found on GitHub for {repo}",
        )

    # Try to find the release matching this tag
    # Tags might be "v4.31.0" while image tag is "4.31.0" or vice versa
    normalized_tag = tag.lstrip("v")
    relevant = []
    found_match = False

    for r in releases:
        release_tag = r["tag_name"].lstrip("v")
        relevant.append(r)
        if release_tag == normalized_tag:
            found_match = True
            break

    # If we found the exact match, return releases up to and including it.
    # Otherwise return the most recent ones for context.
    if found_match:
        return Changelog(image=image, github_repo=repo, releases=relevant)
    else:
        return Changelog(
            image=image,
            github_repo=repo,
            releases=releases[:5],  # Just give recent context
        )
