"""Offline pipeline tests: ingest → changelog resolution → prompt → report.

These tests mock HTTP (no network required) but exercise the real
changelog resolution logic, prompt builder, and report flow end-to-end.
"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from shiplog import db
from shiplog.analyzer import SYSTEM_PROMPT, build_prompt
from shiplog.changelog import (
    Changelog,
    _extract_github_urls,
    fetch_changelog,
    fetch_releases,
    resolve_github_repo,
)
from shiplog.cli import cli


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "pipeline.db")


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "pipeline.db")


def invoke(runner, args, db_path, env=None):
    return runner.invoke(cli, ["--db", db_path] + args, env=env or {})


# --- Fake GitHub/Docker Hub responses ---

FAKE_DIUN_RELEASES = [
    {
        "tag_name": "v4.31.0",
        "name": "4.31.0",
        "body": (
            "## What's Changed\n"
            "* Support negating Kubernetes namespaces\n"
            "* Add Matrix notification server support\n"
            "* Bump Go to 1.25 and Alpine to 3.23\n"
            "* Fix digest comparison for multi-arch images\n"
        ),
        "published_at": "2025-12-10T10:00:00Z",
    },
    {
        "tag_name": "v4.30.0",
        "name": "4.30.0",
        "body": "* Add Gotify X-Gotify-Key header\n* Discord embed support\n",
        "published_at": "2025-10-05T10:00:00Z",
    },
]

FAKE_VAULTWARDEN_RELEASES = [
    {
        "tag_name": "1.33.2",
        "name": "v1.33.2",
        "body": (
            "### Bug Fixes\n"
            "* Fix TOTP validation edge case\n"
            "* Improve SMTP TLS handling\n"
            "\n### Security\n"
            "* Patch XSS in org invite flow (CVE-2025-1234)\n"
        ),
        "published_at": "2025-11-20T10:00:00Z",
    },
]

FAKE_DOCKER_HUB_DIUN = {
    "full_description": (
        "# Diun\n\nDocker Image Update Notifier.\n\n"
        "Source: https://github.com/crazy-max/diun\n\n"
        "Documentation: https://crazymax.dev/diun/\n"
    ),
}

FAKE_DOCKER_HUB_VAULTWARDEN = {
    "full_description": (
        "# Vaultwarden\n\nAlternative Bitwarden server.\n\n"
        "GitHub: https://github.com/dani-garcia/vaultwarden\n"
    ),
}


def _make_github_response(data, status=200):
    """Create a fake httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = data
    resp.headers = {}
    return resp


def _make_docker_hub_response(data, status=200):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = data
    return resp


class TestChangelogResolution:
    """Test the full resolution pipeline with mocked HTTP."""

    def _make_client(self, routes):
        """Create a mock httpx.Client that dispatches by URL pattern."""
        client = MagicMock(spec=httpx.Client)

        def get_side_effect(url, **kwargs):
            for pattern, response in routes.items():
                if pattern in url:
                    return response
            # Default: 404
            return _make_github_response({}, status=404)

        client.get.side_effect = get_side_effect
        return client

    def test_explicit_mapping_resolution(self, conn):
        """User-provided mapping is validated and used."""
        db.set_github_mapping(conn, "docker.io/crazymax/diun", "crazy-max/diun")

        routes = {
            "api.github.com/repos/crazy-max/diun": _make_github_response({"full_name": "crazy-max/diun"}),
        }
        client = self._make_client(routes)

        with patch("shiplog.changelog._github_get", side_effect=lambda c, url, **kw: client.get(url, **kw)):
            repo = resolve_github_repo(client, conn, "docker.io/crazymax/diun")

        assert repo == "crazy-max/diun"

    def test_ghcr_auto_resolution(self, conn):
        """ghcr.io images resolve from path."""
        routes = {
            "api.github.com/repos/crazy-max/diun": _make_github_response({"full_name": "crazy-max/diun"}),
        }
        client = self._make_client(routes)

        with patch("shiplog.changelog._github_get", side_effect=lambda c, url, **kw: client.get(url, **kw)):
            repo = resolve_github_repo(client, conn, "ghcr.io/crazy-max/diun")

        assert repo == "crazy-max/diun"
        # Should be cached
        assert db.get_github_mapping(conn, "ghcr.io/crazy-max/diun") == "crazy-max/diun"

    def test_docker_hub_resolution(self, conn):
        """Docker Hub description GitHub URL extraction."""
        routes = {
            "hub.docker.com/v2/repositories/crazymax/diun/": _make_docker_hub_response(FAKE_DOCKER_HUB_DIUN),
            "api.github.com/repos/crazy-max/diun/releases": _make_github_response(FAKE_DIUN_RELEASES),
            "api.github.com/repos/crazy-max/diun": _make_github_response({"full_name": "crazy-max/diun"}),
        }
        client = self._make_client(routes)

        with patch("shiplog.changelog._github_get", side_effect=lambda c, url, **kw: client.get(url, **kw)):
            repo = resolve_github_repo(client, conn, "docker.io/crazymax/diun")

        assert repo == "crazy-max/diun"

    def test_no_resolution_fails_closed(self, conn):
        """Unknown images with no Docker Hub entry return None."""
        routes = {}  # Everything 404s
        client = self._make_client(routes)

        with patch("shiplog.changelog._github_get", side_effect=lambda c, url, **kw: client.get(url, **kw)):
            repo = resolve_github_repo(client, conn, "registry.local:5000/private-app")

        assert repo is None

    def test_invalid_mapping_fails_closed(self, conn):
        """User mapping that doesn't validate returns None (fail closed)."""
        db.set_github_mapping(conn, "docker.io/foo/bar", "nonexistent/repo")

        routes = {
            "api.github.com/repos/nonexistent/repo": _make_github_response({}, status=404),
        }
        client = self._make_client(routes)

        with patch("shiplog.changelog._github_get", side_effect=lambda c, url, **kw: client.get(url, **kw)):
            repo = resolve_github_repo(client, conn, "docker.io/foo/bar")

        assert repo is None


class TestPromptQuality:
    """Verify the LLM prompt contains the right structure and content."""

    def test_prompt_includes_all_images(self):
        changelogs = [
            Changelog(
                image="docker.io/crazymax/diun",
                github_repo="crazy-max/diun",
                releases=FAKE_DIUN_RELEASES,
            ),
            Changelog(
                image="docker.io/vaultwarden/server",
                github_repo="dani-garcia/vaultwarden",
                releases=FAKE_VAULTWARDEN_RELEASES,
            ),
        ]
        prompt = build_prompt(changelogs)

        assert "docker.io/crazymax/diun" in prompt
        assert "docker.io/vaultwarden/server" in prompt
        assert "crazy-max/diun" in prompt
        assert "dani-garcia/vaultwarden" in prompt

    def test_prompt_includes_release_content(self):
        changelogs = [
            Changelog(
                image="docker.io/crazymax/diun",
                github_repo="crazy-max/diun",
                releases=FAKE_DIUN_RELEASES,
            ),
        ]
        prompt = build_prompt(changelogs)

        # Should contain actual release note content
        assert "Support negating Kubernetes namespaces" in prompt
        assert "Matrix notification" in prompt
        assert "v4.31.0" in prompt
        assert "v4.30.0" in prompt

    def test_prompt_includes_error_for_unresolved(self):
        changelogs = [
            Changelog(
                image="docker.io/crazymax/diun",
                github_repo="crazy-max/diun",
                releases=FAKE_DIUN_RELEASES,
            ),
            Changelog(
                image="registry.local/private-app",
                github_repo=None,
                releases=[],
                error="No GitHub repo found. Add mapping with: shiplog map registry.local/private-app <owner/repo>",
            ),
        ]
        prompt = build_prompt(changelogs)

        assert "registry.local/private-app" in prompt
        assert "No GitHub repo found" in prompt
        # The resolved image should still have its content
        assert "Kubernetes namespaces" in prompt

    def test_prompt_truncates_long_release_bodies(self):
        long_body = "x" * 5000  # Much longer than 3000 char limit
        changelogs = [
            Changelog(
                image="img",
                github_repo="o/r",
                releases=[{
                    "tag_name": "v1",
                    "name": "v1",
                    "body": long_body,
                    "published_at": "2025-01-01",
                }],
            ),
        ]
        prompt = build_prompt(changelogs)

        # Body should be truncated
        assert "truncated" in prompt
        assert len(prompt) < len(long_body)

    def test_system_prompt_is_opinionated(self):
        """The system prompt should guide the LLM to produce useful reports."""
        assert "Risk Level" in SYSTEM_PROMPT
        assert "Breaking Changes" in SYSTEM_PROMPT
        assert "TL;DR" in SYSTEM_PROMPT
        assert "homelab" in SYSTEM_PROMPT
        assert "🟢" in SYSTEM_PROMPT
        assert "🟡" in SYSTEM_PROMPT
        assert "🔴" in SYSTEM_PROMPT

    def test_prompt_with_security_cve(self):
        """Security-relevant content should be preserved in the prompt."""
        changelogs = [
            Changelog(
                image="docker.io/vaultwarden/server",
                github_repo="dani-garcia/vaultwarden",
                releases=FAKE_VAULTWARDEN_RELEASES,
            ),
        ]
        prompt = build_prompt(changelogs)

        assert "CVE-2025-1234" in prompt
        assert "XSS" in prompt
        assert "Security" in prompt


class TestFullOfflinePipeline:
    """End-to-end: ingest → resolve → fetch releases → build prompt → report.

    Mocks all HTTP at the httpx.Client level — no network needed.
    """

    def test_full_pipeline_two_images(self, runner, db_path):
        """Ingest two images, generate a report, verify the full flow."""

        # 1. Ingest
        env = {
            "DIUN_ENTRY_STATUS": "update",
            "DIUN_ENTRY_IMAGE": "docker.io/crazymax/diun:v4.31.0",
            "DIUN_ENTRY_DIGEST": "sha256:abc123",
            "DIUN_ENTRY_PLATFORM": "linux/amd64",
            "DIUN_ENTRY_PROVIDER": "docker",
            "DIUN_ENTRY_HUBLINK": "https://hub.docker.com/r/crazymax/diun",
        }
        result = invoke(runner, ["ingest"], db_path, env=env)
        assert result.exit_code == 0

        result = invoke(runner, ["test-ingest", "docker.io/vaultwarden/server:1.33.2"], db_path)
        assert result.exit_code == 0

        # 2. Verify list shows both
        result = invoke(runner, ["list"], db_path)
        assert "crazymax/diun" in result.output
        assert "vaultwarden/server" in result.output

        # 3. Generate report with mocked HTTP
        fake_llm_response = (
            "## docker.io/crazymax/diun\n\n"
            "**Summary**: Added Kubernetes namespace negation and Matrix notification support.\n"
            "**Risk Level**: 🟢 Safe\n"
            "**Key Changes**:\n"
            "- Kubernetes namespace negation\n"
            "- Matrix server support\n"
            "**Action**: Update now\n"
            "**Breaking Changes**: None\n\n"
            "## docker.io/vaultwarden/server\n\n"
            "**Summary**: Security patch for XSS vulnerability.\n"
            "**Risk Level**: 🔴 Breaking\n"
            "**Key Changes**:\n"
            "- Fix XSS in org invite flow (CVE-2025-1234)\n"
            "- TOTP validation fix\n"
            "**Action**: Update immediately — security fix\n"
            "**Breaking Changes**: None\n\n"
            "## TL;DR\n"
            "- crazymax/diun: Safe (🟢) — update now\n"
            "- vaultwarden/server: Breaking (🔴) — update immediately, security fix\n"
        )

        # Mock both fetch_changelog and analyze
        def fake_fetch(client, conn, image, tag):
            if "crazymax/diun" in image:
                return Changelog(
                    image=image,
                    github_repo="crazy-max/diun",
                    releases=FAKE_DIUN_RELEASES,
                )
            elif "vaultwarden" in image:
                return Changelog(
                    image=image,
                    github_repo="dani-garcia/vaultwarden",
                    releases=FAKE_VAULTWARDEN_RELEASES,
                )
            return Changelog(image=image, github_repo=None, releases=[], error="Unknown")

        with patch("shiplog.cli.fetch_changelog", side_effect=fake_fetch):
            with patch("shiplog.cli.analyze") as mock_analyze:
                mock_analyze.return_value = (fake_llm_response, "gcp/google/gemini-2.5-flash-lite")
                result = invoke(runner, ["report"], db_path)

        assert result.exit_code == 0
        assert "ShipLog Report" in result.output
        assert "Report saved" in result.output

        # 4. Verify the analyze call received proper changelogs
        changelogs_arg = mock_analyze.call_args[0][0]
        assert len(changelogs_arg) == 2
        images = {cl.image for cl in changelogs_arg}
        assert "docker.io/crazymax/diun" in images
        assert "docker.io/vaultwarden/server" in images

        # 5. Verify updates are marked reported
        result = invoke(runner, ["list"], db_path)
        assert "No pending updates" in result.output

        # 6. Show the report
        result = invoke(runner, ["show", "1"], db_path)
        assert result.exit_code == 0
        assert "crazymax/diun" in result.output
        assert "vaultwarden" in result.output
        assert "CVE-2025-1234" in result.output or "XSS" in result.output

        # 7. Reports list should have 1 entry
        result = invoke(runner, ["reports"], db_path)
        assert "llama-3.3-nemotron" in result.output

    def test_pipeline_with_unresolvable_image(self, runner, db_path):
        """Images without GitHub repos get included with mapping hints."""
        invoke(runner, ["test-ingest", "registry.local:5000/my-app:v2"], db_path)

        def fake_fetch(client, conn, image, tag):
            return Changelog(
                image=image,
                github_repo=None,
                releases=[],
                error=f"No GitHub repo found. Add mapping with: shiplog map {image} <owner/repo>",
            )

        with patch("shiplog.cli.fetch_changelog", side_effect=fake_fetch):
            with patch("shiplog.cli.analyze") as mock_analyze:
                mock_analyze.return_value = (
                    "## registry.local:5000/my-app\n**Summary**: No changelog available.\n",
                    "test-model",
                )
                result = invoke(runner, ["report", "--dry-run"], db_path)

        assert result.exit_code == 0
        # Should show mapping hint in stderr (mixed in output for CliRunner)
        assert "shiplog map registry.local:5000/my-app" in result.output

    def test_pipeline_with_output_file(self, runner, db_path, tmp_path):
        """Report written to file contains the full content."""
        invoke(runner, ["test-ingest", "docker.io/nginx:1.27"], db_path)

        def fake_fetch(client, conn, image, tag):
            return Changelog(image=image, github_repo="nginx/nginx", releases=[{
                "tag_name": "release-1.27.0",
                "name": "1.27.0",
                "body": "Stable release with HTTP/3 improvements.",
                "published_at": "2025-06-01",
            }])

        out_file = tmp_path / "report.md"

        with patch("shiplog.cli.fetch_changelog", side_effect=fake_fetch):
            with patch("shiplog.cli.analyze") as mock_analyze:
                mock_analyze.return_value = (
                    "## docker.io/nginx\n**Summary**: HTTP/3 improvements.\n**Risk**: 🟢 Safe\n",
                    "test-model",
                )
                result = invoke(runner, ["report", "--dry-run", "-o", str(out_file)], db_path)

        assert result.exit_code == 0
        content = out_file.read_text()
        assert "ShipLog Report" in content
        assert "HTTP/3" in content
        assert "test-model" in content


class TestVersionFlag:
    """Verify --version works."""

    def test_version(self, runner):
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "shiplog" in result.output
        assert "0.1.0" in result.output


class TestHTTPErrorHandling:
    """Verify graceful handling of various HTTP error scenarios in report."""

    def test_report_handles_connection_error(self, runner, db_path):
        conn = db.connect(db_path)
        db.insert_update(conn, image="img", tag="v1", status="new")
        conn.close()

        with patch("shiplog.cli.fetch_changelog") as mock_fetch:
            mock_fetch.return_value = Changelog(image="img", github_repo="o/r", releases=[])
            with patch("shiplog.cli.analyze") as mock_analyze:
                mock_analyze.side_effect = httpx.ConnectError("Connection refused")
                result = invoke(runner, ["report"], db_path)

        assert result.exit_code == 1
        assert "failed" in result.output.lower() or "error" in result.output.lower()

    def test_report_handles_api_500(self, runner, db_path):
        conn = db.connect(db_path)
        db.insert_update(conn, image="img", tag="v1", status="new")
        conn.close()

        resp = MagicMock()
        resp.status_code = 500
        resp.text = "Internal Server Error"

        with patch("shiplog.cli.fetch_changelog") as mock_fetch:
            mock_fetch.return_value = Changelog(image="img", github_repo="o/r", releases=[])
            with patch("shiplog.cli.analyze") as mock_analyze:
                mock_analyze.side_effect = httpx.HTTPStatusError(
                    "500", request=MagicMock(), response=resp
                )
                result = invoke(runner, ["report"], db_path)

        assert result.exit_code == 1
        assert "500" in result.output
