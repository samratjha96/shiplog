"""Tests for shiplog.cli — Click CLI commands."""

from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from shiplog import db
from shiplog.changelog import Changelog
from shiplog.cli import _split_image_ref, cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def db_path(tmp_path):
    """Return a string path for the CLI --db flag."""
    return str(tmp_path / "test.db")


def invoke(runner, args, db_path, env=None):
    """Invoke CLI with --db pointing to a temp database."""
    return runner.invoke(cli, ["--db", db_path] + args, env=env or {})


# --- _split_image_ref ---


class TestSplitImageRef:
    def test_standard(self):
        assert _split_image_ref("docker.io/foo/bar:v1") == ("docker.io/foo/bar", "v1")

    def test_no_tag(self):
        assert _split_image_ref("docker.io/foo/bar") == ("docker.io/foo/bar", "latest")

    def test_port_with_tag(self):
        assert _split_image_ref("registry.local:5000/app:v2") == ("registry.local:5000/app", "v2")

    def test_port_no_tag(self):
        assert _split_image_ref("registry.local:5000/app") == ("registry.local:5000/app", "latest")

    def test_ghcr(self):
        assert _split_image_ref("ghcr.io/owner/repo:sha-abc") == ("ghcr.io/owner/repo", "sha-abc")

    def test_simple_name(self):
        assert _split_image_ref("nginx:alpine") == ("nginx", "alpine")

    def test_simple_name_no_tag(self):
        assert _split_image_ref("nginx") == ("nginx", "latest")


# --- ingest ---


class TestIngest:
    def test_ingest_from_env(self, runner, db_path):
        env = {
            "DIUN_ENTRY_STATUS": "new",
            "DIUN_ENTRY_IMAGE": "docker.io/crazymax/diun:v4.31.0",
            "DIUN_ENTRY_HUBLINK": "https://hub.docker.com/r/crazymax/diun",
            "DIUN_ENTRY_DIGEST": "sha256:abc123",
            "DIUN_ENTRY_PLATFORM": "linux/amd64",
            "DIUN_ENTRY_PROVIDER": "docker",
        }
        result = invoke(runner, ["ingest"], db_path, env=env)
        assert result.exit_code == 0
        assert "Ingested" in result.output
        assert "docker.io/crazymax/diun:v4.31.0" in result.output

        # Verify it's in the DB
        conn = db.connect(db_path)
        pending = db.get_pending_updates(conn)
        assert len(pending) == 1
        assert pending[0]["image"] == "docker.io/crazymax/diun"
        assert pending[0]["tag"] == "v4.31.0"
        conn.close()

    def test_ingest_minimal_env(self, runner, db_path):
        env = {
            "DIUN_ENTRY_STATUS": "update",
            "DIUN_ENTRY_IMAGE": "docker.io/library/nginx:latest",
        }
        result = invoke(runner, ["ingest"], db_path, env=env)
        assert result.exit_code == 0
        assert "Ingested" in result.output

    def test_ingest_missing_env_fails(self, runner, db_path):
        result = invoke(runner, ["ingest"], db_path, env={})
        assert result.exit_code == 1
        assert "Error" in result.output


# --- test-ingest ---


class TestTestIngest:
    def test_basic(self, runner, db_path):
        result = invoke(runner, ["test-ingest", "docker.io/crazymax/diun:v4.31.0"], db_path)
        assert result.exit_code == 0
        assert "Ingested" in result.output
        assert "docker.io/crazymax/diun" in result.output

        conn = db.connect(db_path)
        pending = db.get_pending_updates(conn)
        assert len(pending) == 1
        assert pending[0]["tag"] == "v4.31.0"
        assert pending[0]["status"] == "update"
        conn.close()

    def test_without_tag_defaults_to_latest(self, runner, db_path):
        result = invoke(runner, ["test-ingest", "docker.io/library/nginx"], db_path)
        assert result.exit_code == 0

        conn = db.connect(db_path)
        pending = db.get_pending_updates(conn)
        assert pending[0]["tag"] == "latest"
        conn.close()

    def test_custom_status(self, runner, db_path):
        result = invoke(runner, ["test-ingest", "--status", "new", "docker.io/foo/bar:v1"], db_path)
        assert result.exit_code == 0

        conn = db.connect(db_path)
        pending = db.get_pending_updates(conn)
        assert pending[0]["status"] == "new"
        conn.close()

    def test_docker_hub_link(self, runner, db_path):
        invoke(runner, ["test-ingest", "docker.io/crazymax/diun:v4"], db_path)
        conn = db.connect(db_path)
        row = db.get_pending_updates(conn)[0]
        assert row["hub_link"] == "https://hub.docker.com/r/crazymax/diun"
        conn.close()

    def test_registry_with_port_and_tag(self, runner, db_path):
        invoke(runner, ["test-ingest", "registry.local:5000/myapp:v1.2"], db_path)
        conn = db.connect(db_path)
        row = db.get_pending_updates(conn)[0]
        assert row["image"] == "registry.local:5000/myapp"
        assert row["tag"] == "v1.2"
        conn.close()

    def test_registry_with_port_no_tag(self, runner, db_path):
        invoke(runner, ["test-ingest", "registry.local:5000/myapp"], db_path)
        conn = db.connect(db_path)
        row = db.get_pending_updates(conn)[0]
        assert row["image"] == "registry.local:5000/myapp"
        assert row["tag"] == "latest"
        conn.close()

    def test_ghcr_link(self, runner, db_path):
        invoke(runner, ["test-ingest", "ghcr.io/immich-app/immich-server:v1.0"], db_path)
        conn = db.connect(db_path)
        row = db.get_pending_updates(conn)[0]
        assert row["hub_link"] is not None
        assert "github.com" in row["hub_link"]
        conn.close()


# --- list ---


class TestList:
    def test_empty(self, runner, db_path):
        result = invoke(runner, ["list"], db_path)
        assert result.exit_code == 0
        assert "No pending updates" in result.output

    def test_with_pending(self, runner, db_path):
        # Seed data
        conn = db.connect(db_path)
        db.insert_update(conn, image="docker.io/foo/bar", tag="v1", status="new")
        db.insert_update(conn, image="docker.io/baz/qux", tag="v2", status="update")
        conn.close()

        result = invoke(runner, ["list"], db_path)
        assert result.exit_code == 0
        assert "docker.io/foo/bar" in result.output
        assert "docker.io/baz/qux" in result.output

    def test_all_flag(self, runner, db_path):
        conn = db.connect(db_path)
        id1 = db.insert_update(conn, image="img1", tag="v1", status="new")
        db.insert_update(conn, image="img2", tag="v2", status="new")
        report_id = db.insert_report(conn, model="test", content="report")
        db.mark_reported(conn, [id1], report_id)
        conn.close()

        # Without --all: only pending
        result = invoke(runner, ["list"], db_path)
        assert "img2" in result.output
        assert "img1" not in result.output

        # With --all: everything
        result = invoke(runner, ["list", "--all"], db_path)
        assert "img1" in result.output
        assert "img2" in result.output


# --- map / mappings ---


class TestMap:
    def test_map_and_list(self, runner, db_path):
        result = invoke(runner, ["map", "docker.io/linuxserver/sonarr", "linuxserver/docker-sonarr"], db_path)
        assert result.exit_code == 0
        assert "Mapped" in result.output

        result = invoke(runner, ["mappings"], db_path)
        assert result.exit_code == 0
        assert "docker.io/linuxserver/sonarr" in result.output
        assert "linuxserver/docker-sonarr" in result.output

    def test_map_invalid_format(self, runner, db_path):
        result = invoke(runner, ["map", "docker.io/foo/bar", "not-a-repo"], db_path)
        assert result.exit_code == 1
        assert "owner/repo" in result.output

    def test_mappings_empty(self, runner, db_path):
        result = invoke(runner, ["mappings"], db_path)
        assert result.exit_code == 0
        assert "No mappings" in result.output


# --- unmap ---


class TestUnmap:
    def test_unmap_existing(self, runner, db_path):
        invoke(runner, ["map", "docker.io/foo/bar", "foo/bar"], db_path)
        result = invoke(runner, ["unmap", "docker.io/foo/bar"], db_path)
        assert result.exit_code == 0
        assert "Removed" in result.output

        # Verify it's gone
        result = invoke(runner, ["mappings"], db_path)
        assert "foo/bar" not in result.output

    def test_unmap_nonexistent(self, runner, db_path):
        result = invoke(runner, ["unmap", "docker.io/nope"], db_path)
        assert result.exit_code == 1
        assert "No mapping found" in result.output


# --- reports ---


class TestReports:
    def test_reports_empty(self, runner, db_path):
        result = invoke(runner, ["reports"], db_path)
        assert result.exit_code == 0
        assert "No reports" in result.output

    def test_reports_with_data(self, runner, db_path):
        conn = db.connect(db_path)
        db.insert_report(conn, model="model-a", content="# Report A")
        db.insert_report(conn, model="model-b", content="# Report B")
        conn.close()

        result = invoke(runner, ["reports"], db_path)
        assert result.exit_code == 0
        assert "model-a" in result.output
        assert "model-b" in result.output


# --- show ---


class TestShow:
    def test_show_existing(self, runner, db_path):
        conn = db.connect(db_path)
        report_id = db.insert_report(conn, model="test-model", content="# Test Report\n\nHello world")
        conn.close()

        result = invoke(runner, ["show", str(report_id)], db_path)
        assert result.exit_code == 0
        assert "Test Report" in result.output
        assert "Hello world" in result.output

    def test_show_nonexistent(self, runner, db_path):
        result = invoke(runner, ["show", "999"], db_path)
        assert result.exit_code == 1
        assert "not found" in result.output


# --- status ---


class TestStatus:
    def test_status_empty(self, runner, db_path):
        result = invoke(runner, ["status"], db_path)
        assert result.exit_code == 0
        assert "ShipLog Status" in result.output
        assert "Total updates:" in result.output
        assert "Pending:" in result.output
        assert "Database:" in result.output

    def test_status_with_data(self, runner, db_path):
        conn = db.connect(db_path)
        db.insert_update(conn, image="img1", tag="v1", status="new")
        db.insert_update(conn, image="img2", tag="v2", status="new")
        db.set_github_mapping(conn, "img1", "owner/repo")
        conn.close()

        result = invoke(runner, ["status"], db_path)
        assert result.exit_code == 0
        assert "Total updates:  2" in result.output
        assert "Pending:        2" in result.output
        assert "Mappings:       1" in result.output
        assert "Last report:    never" in result.output
        assert "Pending images:" in result.output
        assert "img1" in result.output
        assert "img2" in result.output

    def test_status_shows_last_report_date(self, runner, db_path):
        conn = db.connect(db_path)
        db.insert_report(conn, model="m", content="r")
        conn.close()

        result = invoke(runner, ["status"], db_path)
        assert result.exit_code == 0
        assert "Last report:    never" not in result.output
        # Should show a timestamp, not "never"
        assert "Reports:        1" in result.output


# --- purge ---


class TestPurge:
    def test_purge_nothing(self, runner, db_path):
        result = invoke(runner, ["purge", "--yes"], db_path)
        assert result.exit_code == 0
        assert "Nothing to purge" in result.output

    def test_purge_reported_updates(self, runner, db_path):
        conn = db.connect(db_path)
        id1 = db.insert_update(conn, image="img1", tag="v1", status="new")
        db.insert_update(conn, image="img2", tag="v2", status="new")  # stays pending
        report_id = db.insert_report(conn, model="m", content="r")
        db.mark_reported(conn, [id1], report_id)
        conn.close()

        result = invoke(runner, ["purge", "--yes"], db_path)
        assert result.exit_code == 0
        assert "Purged 1" in result.output

        # Pending update should survive
        conn = db.connect(db_path)
        assert len(db.get_pending_updates(conn)) == 1
        assert len(db.get_all_updates(conn)) == 1
        conn.close()


# --- report ---


class TestReport:
    def test_report_no_pending(self, runner, db_path):
        result = invoke(runner, ["report"], db_path)
        assert result.exit_code == 0
        assert "Nothing new to report" in result.output

    @patch("shiplog.cli.fetch_changelog")
    @patch("shiplog.cli.analyze")
    def test_report_dry_run(self, mock_analyze, mock_fetch, runner, db_path):
        # Seed an update
        conn = db.connect(db_path)
        db.insert_update(conn, image="docker.io/foo/bar", tag="v2.0", status="new")
        conn.close()

        mock_fetch.return_value = Changelog(
            image="docker.io/foo/bar",
            github_repo="foo/bar",
            releases=[{"tag_name": "v2.0", "name": "v2.0", "body": "New stuff", "published_at": "2024-01-15"}],
        )
        mock_analyze.return_value = ("## docker.io/foo/bar\n\n**Summary**: New stuff\n", "test-model")

        result = invoke(runner, ["report", "--dry-run"], db_path)
        assert result.exit_code == 0
        assert "ShipLog Report" in result.output
        assert "docker.io/foo/bar" in result.output
        assert "Dry run" in result.output

        # Should NOT be marked as reported
        conn = db.connect(db_path)
        pending = db.get_pending_updates(conn)
        assert len(pending) == 1
        conn.close()

    @patch("shiplog.cli.fetch_changelog")
    @patch("shiplog.cli.analyze")
    def test_report_marks_reported(self, mock_analyze, mock_fetch, runner, db_path):
        conn = db.connect(db_path)
        db.insert_update(conn, image="docker.io/foo/bar", tag="v2.0", status="new")
        conn.close()

        mock_fetch.return_value = Changelog(
            image="docker.io/foo/bar",
            github_repo="foo/bar",
            releases=[{"tag_name": "v2.0", "name": "v2.0", "body": "stuff", "published_at": "2024-01-15"}],
        )
        mock_analyze.return_value = ("Report content", "test-model")

        result = invoke(runner, ["report"], db_path)
        assert result.exit_code == 0
        assert "Report saved" in result.output

        # Should be marked as reported
        conn = db.connect(db_path)
        pending = db.get_pending_updates(conn)
        assert len(pending) == 0
        reports = db.get_all_reports(conn)
        assert len(reports) == 1
        conn.close()

    @patch("shiplog.cli.fetch_changelog")
    @patch("shiplog.cli.analyze")
    def test_report_deduplicates_images_uses_latest_tag(self, mock_analyze, mock_fetch, runner, db_path):
        """Multiple updates for same image should only fetch changelog once, using the latest tag."""
        conn = db.connect(db_path)
        db.insert_update(conn, image="docker.io/foo/bar", tag="v1.0", status="update")
        db.insert_update(conn, image="docker.io/foo/bar", tag="v2.0", status="update")
        conn.close()

        mock_fetch.return_value = Changelog(
            image="docker.io/foo/bar",
            github_repo="foo/bar",
            releases=[],
            error="No releases",
        )
        mock_analyze.return_value = ("Report", "model")

        result = invoke(runner, ["report", "--dry-run"], db_path)
        assert result.exit_code == 0
        # fetch_changelog should only be called once despite 2 updates for same image
        assert mock_fetch.call_count == 1
        # Should use the latest tag (v2.0), not the first one (v1.0)
        call_args = mock_fetch.call_args
        assert call_args[0][2] == "docker.io/foo/bar"  # image
        assert call_args[0][3] == "v2.0"               # tag (latest)

    @patch("shiplog.cli.analyze")
    @patch("shiplog.cli.fetch_changelog")
    def test_report_llm_error(self, mock_fetch, mock_analyze, runner, db_path):
        conn = db.connect(db_path)
        db.insert_update(conn, image="img", tag="v1", status="new")
        conn.close()

        mock_fetch.return_value = Changelog(image="img", github_repo="o/r", releases=[])
        mock_analyze.side_effect = RuntimeError("LLM_API_KEY not set")

        result = invoke(runner, ["report"], db_path)
        assert result.exit_code == 1
        assert "LLM_API_KEY" in result.output

    @patch("shiplog.cli.analyze")
    @patch("shiplog.cli.fetch_changelog")
    def test_report_timeout_error(self, mock_fetch, mock_analyze, runner, db_path):
        conn = db.connect(db_path)
        db.insert_update(conn, image="img", tag="v1", status="new")
        conn.close()

        mock_fetch.return_value = Changelog(image="img", github_repo="o/r", releases=[])
        mock_analyze.side_effect = httpx.ReadTimeout("timed out")

        result = invoke(runner, ["report"], db_path)
        assert result.exit_code == 1
        assert "timed out" in result.output

    @patch("shiplog.cli.fetch_changelog")
    @patch("shiplog.cli.analyze")
    def test_report_output_to_file(self, mock_analyze, mock_fetch, runner, db_path, tmp_path):
        conn = db.connect(db_path)
        db.insert_update(conn, image="img", tag="v1", status="new")
        conn.close()

        mock_fetch.return_value = Changelog(image="img", github_repo="o/r", releases=[])
        mock_analyze.return_value = ("Report content here", "test-model")

        out_file = str(tmp_path / "output" / "report.md")
        result = invoke(runner, ["report", "--dry-run", "-o", out_file], db_path)
        assert result.exit_code == 0
        assert "Report written to" in result.output

        from pathlib import Path
        written = Path(out_file).read_text()
        assert "ShipLog Report" in written
        assert "Report content here" in written

    @patch("shiplog.cli.analyze")
    @patch("shiplog.cli.fetch_changelog")
    def test_report_model_override(self, mock_fetch, mock_analyze, runner, db_path):
        conn = db.connect(db_path)
        db.insert_update(conn, image="img", tag="v1", status="new")
        conn.close()

        mock_fetch.return_value = Changelog(image="img", github_repo="o/r", releases=[])
        mock_analyze.return_value = ("Report", "custom-model")

        result = invoke(runner, ["report", "--model", "custom-model", "--dry-run"], db_path)
        assert result.exit_code == 0
        # Verify model was passed through
        mock_analyze.assert_called_once()
        call_args = mock_analyze.call_args
        assert call_args.kwargs.get("model") == "custom-model"


# --- end-to-end flow ---


class TestEndToEnd:
    """Test the full ingest → list → report → show flow."""

    @patch("shiplog.cli.fetch_changelog")
    @patch("shiplog.cli.analyze")
    def test_full_flow(self, mock_analyze, mock_fetch, runner, db_path):
        # 1. Ingest two updates
        env1 = {
            "DIUN_ENTRY_STATUS": "new",
            "DIUN_ENTRY_IMAGE": "docker.io/crazymax/diun:v4.31.0",
            "DIUN_ENTRY_DIGEST": "sha256:abc",
            "DIUN_ENTRY_PLATFORM": "linux/amd64",
            "DIUN_ENTRY_PROVIDER": "docker",
        }
        result = invoke(runner, ["ingest"], db_path, env=env1)
        assert result.exit_code == 0

        result = invoke(runner, ["test-ingest", "docker.io/linuxserver/sonarr:4.0.17"], db_path)
        assert result.exit_code == 0

        # 2. List pending — should show both
        result = invoke(runner, ["list"], db_path)
        assert result.exit_code == 0
        assert "crazymax/diun" in result.output
        assert "linuxserver/sonarr" in result.output

        # 3. Status should show 2 pending
        result = invoke(runner, ["status"], db_path)
        assert "Pending:        2" in result.output

        # 4. Generate report
        mock_fetch.side_effect = [
            Changelog(
                image="docker.io/crazymax/diun",
                github_repo="crazy-max/diun",
                releases=[{"tag_name": "v4.31.0", "name": "v4.31.0", "body": "Bug fixes", "published_at": "2024-01-15"}],
            ),
            Changelog(
                image="docker.io/linuxserver/sonarr",
                github_repo=None,
                releases=[],
                error="No GitHub repo found. Add mapping with: shiplog map docker.io/linuxserver/sonarr <owner/repo>",
            ),
        ]
        mock_analyze.return_value = (
            "## docker.io/crazymax/diun\n**Summary**: Bug fixes\n**Risk**: 🟢 Safe\n\n"
            "## docker.io/linuxserver/sonarr\n**Summary**: No changelog available\n",
            "test-model",
        )

        result = invoke(runner, ["report"], db_path)
        assert result.exit_code == 0
        assert "ShipLog Report" in result.output
        assert "Report saved" in result.output

        # 5. List pending — should be empty
        result = invoke(runner, ["list"], db_path)
        assert "No pending updates" in result.output

        # 6. Show the saved report
        result = invoke(runner, ["show", "1"], db_path)
        assert result.exit_code == 0
        assert "crazymax/diun" in result.output

        # 7. Status should show 0 pending, 1 report
        result = invoke(runner, ["status"], db_path)
        assert "Pending:        0" in result.output
        assert "Reports:        1" in result.output
