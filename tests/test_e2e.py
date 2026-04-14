"""End-to-end tests for the full ingest → changelog → prompt pipeline.

These tests hit real GitHub/Docker Hub APIs and require --network.
They do NOT call the LLM — they validate everything up to the prompt.
"""

from unittest.mock import patch

import httpx
import pytest

from shiplog import db
from shiplog.analyzer import build_prompt
from shiplog.changelog import Changelog, fetch_changelog, resolve_github_repo


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "e2e.db")


@pytest.mark.network
class TestFullPipeline:
    """Exercises ingest → changelog fetch → prompt build for real images."""

    def test_diun_image_with_mapping(self, conn):
        """Simulate diun detecting a crazymax/diun update."""
        # 1. Ingest
        row_id = db.insert_update(
            conn,
            image="docker.io/crazymax/diun",
            tag="v4.28.0",
            digest="sha256:abc123",
            status="update",
            platform="linux/amd64",
            provider="docker",
        )
        assert row_id == 1

        # 2. Add mapping (user would do: shiplog map ...)
        db.set_github_mapping(conn, "docker.io/crazymax/diun", "crazy-max/diun")

        # 3. Fetch changelog
        with httpx.Client(timeout=15.0) as client:
            cl = fetch_changelog(client, conn, "docker.io/crazymax/diun", "v4.28.0")

        assert cl.github_repo == "crazy-max/diun"
        assert cl.error is None
        assert len(cl.releases) > 0
        # Should have the matching release or recent ones
        tags = [r["tag_name"] for r in cl.releases]
        assert any(t for t in tags)  # At least some releases

        # 4. Build prompt
        prompt = build_prompt([cl])
        assert "docker.io/crazymax/diun" in prompt
        assert "crazy-max/diun" in prompt
        # Should contain actual release note content
        assert len(prompt) > 200

    def test_ghcr_auto_resolution(self, conn):
        """ghcr.io images auto-resolve to GitHub repos."""
        with httpx.Client(timeout=15.0) as client:
            repo = resolve_github_repo(client, conn, "ghcr.io/crazy-max/diun")

        assert repo == "crazy-max/diun"

        # Verify it was cached in the DB
        mapping = db.get_github_mapping(conn, "ghcr.io/crazy-max/diun")
        assert mapping == "crazy-max/diun"

    def test_unknown_image_fails_closed(self, conn):
        """Unknown images with no mapping produce a helpful error, not a guess."""
        with httpx.Client(timeout=15.0) as client:
            cl = fetch_changelog(
                client, conn, "registry.local:5000/my-private-app", "v1.0"
            )

        assert cl.github_repo is None
        assert cl.error is not None
        assert "shiplog map" in cl.error

    def test_multiple_images_prompt(self, conn):
        """Build a prompt from multiple images — some with changelogs, some without."""
        db.set_github_mapping(conn, "docker.io/crazymax/diun", "crazy-max/diun")

        with httpx.Client(timeout=15.0) as client:
            cl1 = fetch_changelog(client, conn, "docker.io/crazymax/diun", "v4.28.0")
            cl2 = fetch_changelog(client, conn, "registry.local/myapp", "v1.0")

        prompt = build_prompt([cl1, cl2])
        # Should contain both images
        assert "docker.io/crazymax/diun" in prompt
        assert "registry.local/myapp" in prompt
        # The one without a repo should have an error note
        assert "No GitHub repo found" in prompt

    def test_docker_hub_description_resolution(self, conn):
        """Docker Hub description scraping can find GitHub URLs."""
        # vaultwarden/server has a GitHub link in its Docker Hub description
        with httpx.Client(timeout=15.0) as client:
            repo = resolve_github_repo(
                client, conn, "docker.io/vaultwarden/server"
            )

        # This may or may not resolve depending on Docker Hub description content.
        # The important thing is it doesn't crash and either resolves correctly
        # or returns None (fail closed).
        if repo is not None:
            assert "/" in repo
            # Verify it was cached
            assert db.get_github_mapping(conn, "docker.io/vaultwarden/server") is not None


@pytest.mark.network
class TestRateLimitHeaders:
    """Verify we send proper headers that won't get us blocked."""

    def test_github_user_agent(self):
        """GitHub API requests include a User-Agent header."""
        from shiplog.changelog import _github_headers

        headers = _github_headers()
        assert "User-Agent" in headers
        assert "ShipLog" in headers["User-Agent"]
        assert "X-GitHub-Api-Version" in headers
