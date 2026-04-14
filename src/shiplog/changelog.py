"""Fetch changelogs from GitHub Releases and resolve image→repo mappings."""

import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass

import httpx

from shiplog import db

GITHUB_API = "https://api.github.com"
DOCKER_HUB_API = "https://hub.docker.com/v2"
USER_AGENT = "ShipLog/0.1 (container-update-reporter)"

# Max retries for rate-limited GitHub requests
_MAX_RETRIES = 3
_RATE_LIMIT_BACKOFF_SECONDS = [5, 30, 60]  # escalating backoff


@dataclass
class Changelog:
    """Changelog data for a container image update."""
    image: str
    github_repo: str | None  # owner/repo
    releases: list[dict]     # [{tag_name, name, body, published_at}, ...]
    tag: str = ""            # the detected tag (e.g. "v4.31.0")
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


def _github_get(
    client: httpx.Client,
    url: str,
    *,
    params: dict | None = None,
    _sleep: object = time.sleep,
) -> httpx.Response | None:
    """Make a GitHub API GET request with rate limit retry.

    Returns the Response on success (any status code), or None on network error.
    On 403 with rate limit headers, waits and retries up to _MAX_RETRIES times.
    """
    headers = _github_headers()
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = client.get(url, headers=headers, params=params)
        except httpx.HTTPError:
            return None

        # Check for rate limiting (403 with X-RateLimit-Remaining: 0)
        if resp.status_code == 403:
            remaining = resp.headers.get("x-ratelimit-remaining")
            if remaining == "0" and attempt < _MAX_RETRIES:
                # Calculate wait time from reset header or use backoff
                reset_at = resp.headers.get("x-ratelimit-reset")
                if reset_at:
                    try:
                        wait = max(1, int(reset_at) - int(time.time()))
                        wait = min(wait, 120)  # cap at 2 minutes
                    except ValueError:
                        wait = _RATE_LIMIT_BACKOFF_SECONDS[attempt]
                else:
                    wait = _RATE_LIMIT_BACKOFF_SECONDS[attempt]

                print(
                    f"  ⚠️  GitHub rate limit hit. Waiting {wait}s before retry "
                    f"({attempt + 1}/{_MAX_RETRIES})...",
                    file=sys.stderr,
                )
                _sleep(wait)
                continue

        # 429 Too Many Requests — also rate limited
        if resp.status_code == 429 and attempt < _MAX_RETRIES:
            wait = _RATE_LIMIT_BACKOFF_SECONDS[min(attempt, len(_RATE_LIMIT_BACKOFF_SECONDS) - 1)]
            print(
                f"  ⚠️  GitHub rate limit (429). Waiting {wait}s before retry "
                f"({attempt + 1}/{_MAX_RETRIES})...",
                file=sys.stderr,
            )
            _sleep(wait)
            continue

        return resp

    # Exhausted retries — return the last response
    return resp  # type: ignore[possibly-undefined]


def validate_github_repo(client: httpx.Client, owner_repo: str) -> bool:
    """Confirm a GitHub repo exists by hitting the API. Returns True if 200."""
    resp = _github_get(client, f"{GITHUB_API}/repos/{owner_repo}")
    return resp is not None and resp.status_code == 200


# GitHub paths that look like owner/repo but aren't actual repositories.
_GITHUB_NON_REPO_PREFIXES = frozenset({
    "orgs", "settings", "marketplace", "features", "topics",
    "sponsors", "collections", "explore", "trending", "events",
    "about", "pricing", "security", "login", "signup", "apps",
})


def _extract_github_urls(text: str) -> list[str]:
    """Extract all unique github.com/owner/repo references from text.

    Only matches explicit URLs — not arbitrary patterns.
    Rejects known non-repo paths like github.com/orgs/...
    Returns deduplicated list preserving first-seen order.
    """
    seen: set[str] = set()
    results: list[str] = []
    for match in re.finditer(
        r"https?://github\.com/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)", text
    ):
        owner = match.group(1)
        repo = match.group(2).rstrip("/")
        if repo.endswith(".git"):
            repo = repo[:-4]
        if owner.lower() in _GITHUB_NON_REPO_PREFIXES:
            continue
        candidate = f"{owner}/{repo}"
        if candidate not in seen:
            seen.add(candidate)
            results.append(candidate)
    return results


def _try_candidate(
    client: httpx.Client,
    conn: sqlite3.Connection,
    image: str,
    candidate: str,
) -> str | None:
    """Validate a candidate repo and save the mapping if it has releases."""
    if not validate_github_repo(client, candidate):
        return None
    resp = _github_get(
        client,
        f"{GITHUB_API}/repos/{candidate}/releases",
        params={"per_page": 1},
    )
    if resp is not None and resp.status_code == 200 and resp.json():
        db.set_github_mapping(conn, image, candidate, auto_detected=True)
        return candidate
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
    3. For lscr.io images, look up Docker Hub equivalent
    4. Check Docker Hub description for GitHub URLs
    5. Try Docker Hub namespace/name as a GitHub repo directly

    All candidates are validated via GitHub API before acceptance.

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

    # 3. lscr.io — LinuxServer images, also published on Docker Hub
    if image.startswith("lscr.io/"):
        parts = image.removeprefix("lscr.io/").split("/")
        if len(parts) >= 2:
            docker_hub_image = f"docker.io/{parts[0]}/{parts[1]}"
            candidates = _try_docker_hub_description(client, docker_hub_image)
            for candidate in candidates:
                result = _try_candidate(client, conn, image, candidate)
                if result:
                    return result

    # 4. Docker Hub — scrape description for GitHub URLs
    #    Try all candidates, prefer the first one that has releases
    #    (avoids picking e.g. traefik-library-image over traefik/traefik)
    candidates = _try_docker_hub_description(client, image)
    first_valid: str | None = None
    for candidate in candidates:
        result = _try_candidate(client, conn, image, candidate)
        if result:
            return result
        # Track first repo that exists (even without releases) as fallback
        if first_valid is None and validate_github_repo(client, candidate):
            first_valid = candidate

    if first_valid:
        # No candidate had releases — use the first valid repo anyway
        db.set_github_mapping(conn, image, first_valid, auto_detected=True)
        return first_valid

    # 5. Last resort: try namespace/name as a GitHub repo directly.
    #    Many projects use the same owner/repo on GitHub and Docker Hub
    #    (e.g. grafana/grafana, jellyfin/jellyfin). Validated via API.
    candidate = _image_to_github_candidate(image)
    if candidate:
        result = _try_candidate(client, conn, image, candidate)
        if result:
            return result

    return None


def _image_to_github_candidate(image: str) -> str | None:
    """Extract a namespace/name candidate from a Docker image ref.

    docker.io/grafana/grafana -> grafana/grafana
    docker.io/library/traefik -> None (official images, no useful owner)
    ghcr.io/foo/bar -> None (already handled by ghcr.io strategy)
    lscr.io/foo/bar -> None (already handled by lscr.io strategy)
    """
    img = image
    for prefix in ("docker.io/", "index.docker.io/", "registry-1.docker.io/"):
        if img.startswith(prefix):
            img = img[len(prefix):]
            break
    else:
        # Not a docker.io image — other registries handled by earlier strategies
        return None

    parts = img.split("/")
    if len(parts) == 2 and parts[0] != "library":
        return f"{parts[0]}/{parts[1]}"
    return None


def _try_docker_hub_description(
    client: httpx.Client,
    image: str,
) -> list[str]:
    """Extract GitHub repo candidates from the Docker Hub description.

    Returns a list of 'owner/repo' strings (not yet validated).
    """
    # Parse namespace/name from image
    # docker.io/library/nginx → library/nginx
    # docker.io/crazymax/diun → crazymax/diun
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
        return []

    try:
        resp = client.get(
            f"{DOCKER_HUB_API}/repositories/{namespace}/{name}/",
            headers={"User-Agent": USER_AGENT},
            timeout=10.0,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        desc = data.get("full_description") or data.get("description") or ""
        return _extract_github_urls(desc)
    except httpx.HTTPError:
        return []


def fetch_releases(
    client: httpx.Client,
    github_repo: str,
    per_page: int = 10,
) -> list[dict]:
    """Fetch recent releases from a GitHub repo.

    Returns a list of {tag_name, name, body, published_at} dicts.
    """
    resp = _github_get(
        client,
        f"{GITHUB_API}/repos/{github_repo}/releases",
        params={"per_page": per_page},
    )
    if resp is None or resp.status_code != 200:
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
            tag=tag,
            error=f"No GitHub repo found. Add mapping with: shiplog map {image} <owner/repo>",
        )

    releases = fetch_releases(client, repo)
    if not releases:
        return Changelog(
            image=image,
            github_repo=repo,
            releases=[],
            tag=tag,
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
        return Changelog(image=image, github_repo=repo, releases=relevant, tag=tag)
    else:
        return Changelog(
            image=image,
            github_repo=repo,
            releases=releases[:5],  # Just give recent context
            tag=tag,
        )
