"""Tests for shiplog.changelog — GitHub/Docker Hub changelog fetching."""
from unittest.mock import patch

import httpx
import pytest

from shiplog import db
from shiplog.changelog import (
    _extract_github_url,
    fetch_changelog,
    fetch_releases,
    validate_github_repo,
)


class TestExtractGitHubUrl:
    def test_https_url(self):
        text = "Check out https://github.com/crazy-max/diun for more info."
        assert _extract_github_url(text) == "crazy-max/diun"

    def test_http_url(self):
        text = "See http://github.com/linuxserver/docker-sonarr"
        assert _extract_github_url(text) == "linuxserver/docker-sonarr"

    def test_url_with_git_suffix(self):
        text = "Clone from https://github.com/example/repo.git"
        assert _extract_github_url(text) == "example/repo"

    def test_url_with_trailing_slash(self):
        text = "https://github.com/owner/repo/"
        assert _extract_github_url(text) == "owner/repo"

    def test_no_match(self):
        assert _extract_github_url("No GitHub link here") is None

    def test_empty_string(self):
        assert _extract_github_url("") is None

    def test_url_with_subpath_extracts_owner_repo(self):
        # Should extract owner/repo even if there's more path after
        text = "https://github.com/crazy-max/diun/releases"
        result = _extract_github_url(text)
        assert result == "crazy-max/diun"

    def test_rejects_github_org_paths(self):
        # github.com/orgs/linuxserver/packages is NOT a repo
        text = "See https://github.com/orgs/linuxserver/packages for downloads."
        assert _extract_github_url(text) is None

    def test_rejects_github_settings_paths(self):
        text = "Visit https://github.com/settings/tokens"
        assert _extract_github_url(text) is None

    def test_skips_non_repo_finds_real_repo(self):
        # If text contains both a non-repo and a real repo URL, find the real one
        text = (
            "See https://github.com/orgs/linuxserver/packages and "
            "source at https://github.com/linuxserver/docker-sonarr"
        )
        assert _extract_github_url(text) == "linuxserver/docker-sonarr"


class TestValidateGitHubRepo:
    """These hit the real GitHub API — require network."""

    @pytest.mark.network
    def test_valid_repo(self):
        with httpx.Client() as client:
            assert validate_github_repo(client, "crazy-max/diun") is True

    @pytest.mark.network
    def test_invalid_repo(self):
        with httpx.Client() as client:
            assert validate_github_repo(client, "nonexistent-user-xyz/nonexistent-repo-abc") is False

    def test_network_error_returns_false(self):
        """Connection failures should return False, not crash."""
        with httpx.Client() as client:
            with patch.object(client, "get", side_effect=httpx.ConnectError("timeout")):
                assert validate_github_repo(client, "any/repo") is False


class TestFetchReleases:
    def test_network_error_returns_empty(self):
        """Connection failures should return [], not crash."""
        with httpx.Client() as client:
            with patch.object(client, "get", side_effect=httpx.ConnectError("timeout")):
                assert fetch_releases(client, "any/repo") == []


class TestFetchChangelog:
    """Integration tests that hit real APIs."""

    @pytest.mark.network
    def test_known_repo_with_mapping(self, tmp_path):
        """Fetch changelog for a known image with a user mapping."""
        conn = db.connect(tmp_path / "test.db")
        db.set_github_mapping(conn, "docker.io/crazymax/diun", "crazy-max/diun")

        with httpx.Client(timeout=15.0) as client:
            cl = fetch_changelog(client, conn, "docker.io/crazymax/diun", "v4.28.0")

        assert cl.github_repo == "crazy-max/diun"
        assert cl.error is None
        assert len(cl.releases) > 0

    @pytest.mark.network
    def test_unknown_image_no_mapping(self, tmp_path):
        """Unknown image with no mapping returns helpful error."""
        conn = db.connect(tmp_path / "test.db")

        with httpx.Client(timeout=15.0) as client:
            cl = fetch_changelog(client, conn, "registry.local:5000/myapp", "v1.0")

        assert cl.github_repo is None
        assert cl.error is not None
        assert "shiplog map" in cl.error

    @pytest.mark.network
    def test_ghcr_image_auto_resolves(self, tmp_path):
        """ghcr.io images should auto-resolve to their GitHub repo."""
        conn = db.connect(tmp_path / "test.db")

        with httpx.Client(timeout=15.0) as client:
            cl = fetch_changelog(client, conn, "ghcr.io/crazy-max/diun", "v4.28.0")

        assert cl.github_repo == "crazy-max/diun"
        assert cl.error is None
