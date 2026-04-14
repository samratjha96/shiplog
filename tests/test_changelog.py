"""Tests for shiplog.changelog — GitHub/Docker Hub changelog fetching."""
from unittest.mock import MagicMock, call, patch

import httpx
import pytest

from shiplog import db
from shiplog.changelog import (
    _extract_github_urls,
    _github_get,
    fetch_changelog,
    fetch_releases,
    resolve_github_repo,
    validate_github_repo,
)


class TestExtractGitHubUrls:
    def test_https_url(self):
        text = "Check out https://github.com/crazy-max/diun for more info."
        assert _extract_github_urls(text) == ["crazy-max/diun"]

    def test_http_url(self):
        text = "See http://github.com/linuxserver/docker-sonarr"
        assert _extract_github_urls(text) == ["linuxserver/docker-sonarr"]

    def test_url_with_git_suffix(self):
        text = "Clone from https://github.com/example/repo.git"
        assert _extract_github_urls(text) == ["example/repo"]

    def test_url_with_trailing_slash(self):
        text = "https://github.com/owner/repo/"
        assert _extract_github_urls(text) == ["owner/repo"]

    def test_no_match(self):
        assert _extract_github_urls("No GitHub link here") == []

    def test_empty_string(self):
        assert _extract_github_urls("") == []

    def test_url_with_subpath_extracts_owner_repo(self):
        text = "https://github.com/crazy-max/diun/releases"
        assert _extract_github_urls(text) == ["crazy-max/diun"]

    def test_rejects_github_org_paths(self):
        text = "See https://github.com/orgs/linuxserver/packages for downloads."
        assert _extract_github_urls(text) == []

    def test_rejects_github_settings_paths(self):
        text = "Visit https://github.com/settings/tokens"
        assert _extract_github_urls(text) == []

    def test_skips_non_repo_finds_real_repo(self):
        text = (
            "See https://github.com/orgs/linuxserver/packages and "
            "source at https://github.com/linuxserver/docker-sonarr"
        )
        assert _extract_github_urls(text) == ["linuxserver/docker-sonarr"]

    def test_multiple_unique_urls(self):
        text = (
            "See https://github.com/traefik/traefik-library-image and "
            "https://github.com/traefik/traefik for the real repo"
        )
        assert _extract_github_urls(text) == [
            "traefik/traefik-library-image",
            "traefik/traefik",
        ]

    def test_deduplicates(self):
        text = (
            "https://github.com/owner/repo and "
            "https://github.com/owner/repo again"
        )
        assert _extract_github_urls(text) == ["owner/repo"]


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


class TestResolveGitHubRepo:
    """Test repo resolution strategy with mocks."""

    def test_prefers_repo_with_releases_over_library_image(self, tmp_path):
        """When Docker Hub has multiple GitHub URLs, prefer one with releases."""
        conn = db.connect(tmp_path / "test.db")

        # Mock Docker Hub returning a description with two repos
        docker_hub_resp = httpx.Response(
            200,
            json={
                "full_description": (
                    "Maintained by https://github.com/traefik/traefik-library-image\n"
                    "Source at https://github.com/traefik/traefik"
                ),
            },
            request=httpx.Request("GET", "https://hub.docker.com/v2/repositories/library/traefik/"),
        )
        # Mock GitHub API: both repos exist
        repo_exists = httpx.Response(
            200,
            json={"id": 1},
            request=httpx.Request("GET", "https://api.github.com/repos/traefik/traefik"),
        )
        # Library-image has no releases, traefik/traefik has releases
        no_releases = httpx.Response(
            200,
            json=[],
            request=httpx.Request("GET", "https://api.github.com/repos/traefik/traefik-library-image/releases"),
        )
        has_releases = httpx.Response(
            200,
            json=[{"tag_name": "v3.0.0"}],
            request=httpx.Request("GET", "https://api.github.com/repos/traefik/traefik/releases"),
        )

        def mock_get(url, **kwargs):
            if "hub.docker.com" in url:
                return docker_hub_resp
            if "traefik-library-image/releases" in url:
                return no_releases
            if "traefik/traefik/releases" in url:
                return has_releases
            # Repo existence checks
            return repo_exists

        with httpx.Client() as client:
            with patch.object(client, "get", side_effect=mock_get):
                result = resolve_github_repo(client, conn, "docker.io/traefik")

        assert result == "traefik/traefik"
        # Should have been auto-saved to DB
        assert db.get_github_mapping(conn, "docker.io/traefik") == "traefik/traefik"

    def test_falls_back_to_first_valid_if_none_have_releases(self, tmp_path):
        """When no candidate has releases, use the first valid one."""
        conn = db.connect(tmp_path / "test.db")

        docker_hub_resp = httpx.Response(
            200,
            json={"full_description": "See https://github.com/owner/repo-a"},
            request=httpx.Request("GET", "https://hub.docker.com/v2/repositories/owner/app/"),
        )
        repo_exists = httpx.Response(
            200, json={"id": 1},
            request=httpx.Request("GET", "https://api.github.com/repos/owner/repo-a"),
        )
        no_releases = httpx.Response(
            200, json=[],
            request=httpx.Request("GET", "https://api.github.com/repos/owner/repo-a/releases"),
        )

        def mock_get(url, **kwargs):
            if "hub.docker.com" in url:
                return docker_hub_resp
            if "/releases" in url:
                return no_releases
            return repo_exists

        with httpx.Client() as client:
            with patch.object(client, "get", side_effect=mock_get):
                result = resolve_github_repo(client, conn, "docker.io/owner/app")

        assert result == "owner/repo-a"

    def test_user_mapping_takes_priority(self, tmp_path):
        """Explicit user mapping should be used without hitting Docker Hub."""
        conn = db.connect(tmp_path / "test.db")
        db.set_github_mapping(conn, "docker.io/my/app", "my/app-source")

        repo_exists = httpx.Response(
            200, json={"id": 1},
            request=httpx.Request("GET", "https://api.github.com/repos/my/app-source"),
        )

        def mock_get(url, **kwargs):
            assert "hub.docker.com" not in url, "Should not hit Docker Hub"
            return repo_exists

        with httpx.Client() as client:
            with patch.object(client, "get", side_effect=mock_get):
                result = resolve_github_repo(client, conn, "docker.io/my/app")

        assert result == "my/app-source"


class TestGitHubRateLimit:
    """Test _github_get rate limit detection and retry."""

    def test_rate_limit_403_retries(self):
        """403 with x-ratelimit-remaining: 0 triggers retry."""
        rate_limited = httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"},
            request=httpx.Request("GET", "https://api.github.com/repos/o/r"),
        )
        ok = httpx.Response(
            200,
            json={"id": 1},
            request=httpx.Request("GET", "https://api.github.com/repos/o/r"),
        )

        mock_sleep = MagicMock()
        with httpx.Client() as client:
            with patch.object(client, "get", side_effect=[rate_limited, ok]):
                resp = _github_get(client, "https://api.github.com/repos/o/r", _sleep=mock_sleep)

        assert resp is not None
        assert resp.status_code == 200
        mock_sleep.assert_called_once()  # slept once before retry

    def test_rate_limit_429_retries(self):
        """429 responses trigger retry."""
        throttled = httpx.Response(
            429,
            request=httpx.Request("GET", "https://api.github.com/repos/o/r"),
        )
        ok = httpx.Response(
            200,
            json={"id": 1},
            request=httpx.Request("GET", "https://api.github.com/repos/o/r"),
        )

        mock_sleep = MagicMock()
        with httpx.Client() as client:
            with patch.object(client, "get", side_effect=[throttled, ok]):
                resp = _github_get(client, "https://api.github.com/repos/o/r", _sleep=mock_sleep)

        assert resp is not None
        assert resp.status_code == 200
        mock_sleep.assert_called_once()

    def test_rate_limit_exhausts_retries(self):
        """After max retries, returns the last 403 response."""
        rate_limited = httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "0"},
            request=httpx.Request("GET", "https://api.github.com/repos/o/r"),
        )

        mock_sleep = MagicMock()
        with httpx.Client() as client:
            with patch.object(client, "get", return_value=rate_limited):
                resp = _github_get(client, "https://api.github.com/repos/o/r", _sleep=mock_sleep)

        assert resp is not None
        assert resp.status_code == 403
        assert mock_sleep.call_count == 3  # _MAX_RETRIES

    def test_403_without_rate_limit_header_not_retried(self):
        """403 without rate limit headers is not a rate limit — don't retry."""
        forbidden = httpx.Response(
            403,
            request=httpx.Request("GET", "https://api.github.com/repos/o/r"),
        )

        mock_sleep = MagicMock()
        with httpx.Client() as client:
            with patch.object(client, "get", return_value=forbidden):
                resp = _github_get(client, "https://api.github.com/repos/o/r", _sleep=mock_sleep)

        assert resp is not None
        assert resp.status_code == 403
        mock_sleep.assert_not_called()

    def test_network_error_returns_none(self):
        """Connection failures return None."""
        mock_sleep = MagicMock()
        with httpx.Client() as client:
            with patch.object(client, "get", side_effect=httpx.ConnectError("timeout")):
                resp = _github_get(client, "https://api.github.com/repos/o/r", _sleep=mock_sleep)

        assert resp is None
        mock_sleep.assert_not_called()

    def test_uses_reset_header_for_wait_time(self):
        """When x-ratelimit-reset is set, use it to calculate wait time."""
        import time as _time

        future_reset = str(int(_time.time()) + 15)
        rate_limited = httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": future_reset},
            request=httpx.Request("GET", "https://api.github.com/repos/o/r"),
        )
        ok = httpx.Response(
            200,
            json={},
            request=httpx.Request("GET", "https://api.github.com/repos/o/r"),
        )

        mock_sleep = MagicMock()
        with httpx.Client() as client:
            with patch.object(client, "get", side_effect=[rate_limited, ok]):
                _github_get(client, "https://api.github.com/repos/o/r", _sleep=mock_sleep)

        # Should sleep for roughly 15 seconds (the reset header value)
        wait_arg = mock_sleep.call_args[0][0]
        assert 10 <= wait_arg <= 20


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
