"""Tests for shiplog.cli — Click CLI commands."""

from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from shiplog import db
from shiplog.changelog import Changelog
from shiplog.cli import cli


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


# --- list ---


class TestList:
    def test_empty(self, runner, db_path):
        result = invoke(runner, ["list"], db_path)
        assert result.exit_code == 0
        assert "No pending updates" in result.output

    def test_with_pending(self, runner, db_path):
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

        # No args = list mappings
        result = invoke(runner, ["map"], db_path)
        assert result.exit_code == 0
        assert "docker.io/linuxserver/sonarr" in result.output
        assert "linuxserver/docker-sonarr" in result.output

    def test_map_invalid_format(self, runner, db_path):
        result = invoke(runner, ["map", "docker.io/foo/bar", "not-a-repo"], db_path)
        assert result.exit_code == 1
        assert "owner/repo" in result.output

    def test_map_no_args_empty(self, runner, db_path):
        result = invoke(runner, ["map"], db_path)
        assert result.exit_code == 0
        assert "No mappings" in result.output

    def test_map_one_arg_errors(self, runner, db_path):
        result = invoke(runner, ["map", "docker.io/foo/bar"], db_path)
        assert result.exit_code == 1


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
        assert "Reports:        1" in result.output


# --- report ---


class TestReport:
    def test_report_no_pending(self, runner, db_path):
        result = invoke(runner, ["report"], db_path)
        assert result.exit_code == 0
        assert "Nothing new to report" in result.output

    @patch("shiplog.cli.fetch_changelog")
    @patch("shiplog.cli.analyze")
    def test_report_dry_run(self, mock_analyze, mock_fetch, runner, db_path):
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

        conn = db.connect(db_path)
        pending = db.get_pending_updates(conn)
        assert len(pending) == 0
        reports = db.get_all_reports(conn)
        assert len(reports) == 1
        conn.close()

    @patch("shiplog.cli.fetch_changelog")
    @patch("shiplog.cli.analyze")
    def test_report_deduplicates_images_uses_latest_tag(self, mock_analyze, mock_fetch, runner, db_path):
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
        assert mock_fetch.call_count == 1
        call_args = mock_fetch.call_args
        assert call_args[0][2] == "docker.io/foo/bar"
        assert call_args[0][3] == "v2.0"

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

    @patch("shiplog.cli.fetch_changelog")
    @patch("shiplog.cli.analyze")
    def test_report_survives_changelog_fetch_error(self, mock_analyze, mock_fetch, runner, db_path):
        conn = db.connect(db_path)
        db.insert_update(conn, image="docker.io/good/image", tag="v1", status="new")
        db.insert_update(conn, image="docker.io/bad/image", tag="v2", status="new")
        conn.close()

        def side_effect(client, conn, image, tag):
            if "bad" in image:
                raise ValueError("Unexpected JSON decode error")
            return Changelog(image=image, github_repo="good/image", releases=[])

        mock_fetch.side_effect = side_effect
        mock_analyze.return_value = ("Report content", "test-model")

        result = invoke(runner, ["report", "--dry-run"], db_path)
        assert result.exit_code == 0
        assert "ShipLog Report" in result.output
        assert mock_analyze.call_count == 1
        changelogs_arg = mock_analyze.call_args[0][0]
        assert len(changelogs_arg) == 2
        errors = [cl for cl in changelogs_arg if cl.error]
        assert len(errors) == 1
        assert "Unexpected JSON decode error" in errors[0].error

    @patch("shiplog.cli.fetch_changelog")
    @patch("shiplog.cli.analyze")
    def test_report_shows_mapping_hints_for_unresolved(self, mock_analyze, mock_fetch, runner, db_path):
        conn = db.connect(db_path)
        db.insert_update(conn, image="docker.io/good/image", tag="v1", status="new")
        db.insert_update(conn, image="registry.local/private-app", tag="v2", status="new")
        conn.close()

        def side_effect(client, conn, image, tag):
            if "good" in image:
                return Changelog(
                    image=image, github_repo="good/image",
                    releases=[{"tag_name": "v1", "name": "v1", "body": "stuff", "published_at": "2024-01-01"}],
                )
            return Changelog(
                image=image, github_repo=None, releases=[],
                error=f"No GitHub repo found. Add mapping with: shiplog map {image} <owner/repo>",
            )

        mock_fetch.side_effect = side_effect
        mock_analyze.return_value = ("Report content", "test-model")

        result = invoke(runner, ["report", "--dry-run"], db_path)
        assert result.exit_code == 0
        assert "shiplog map registry.local/private-app" in result.output
        assert "1 image(s) with changelogs" in result.output
        assert "1 image(s) with no GitHub mapping" in result.output

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
        mock_analyze.assert_called_once()
        call_args = mock_analyze.call_args
        assert call_args.kwargs.get("model") == "custom-model"


# --- end-to-end flow ---


class TestEndToEnd:
    @patch("shiplog.cli.fetch_changelog")
    @patch("shiplog.cli.analyze")
    def test_full_flow(self, mock_analyze, mock_fetch, runner, db_path):
        # 1. Ingest via diun env vars
        env = {
            "DIUN_ENTRY_STATUS": "new",
            "DIUN_ENTRY_IMAGE": "docker.io/crazymax/diun:v4.31.0",
            "DIUN_ENTRY_DIGEST": "sha256:abc",
            "DIUN_ENTRY_PLATFORM": "linux/amd64",
            "DIUN_ENTRY_PROVIDER": "docker",
        }
        result = invoke(runner, ["ingest"], db_path, env=env)
        assert result.exit_code == 0

        # 2. List pending
        result = invoke(runner, ["list"], db_path)
        assert result.exit_code == 0
        assert "crazymax/diun" in result.output

        # 3. Status shows 1 pending
        result = invoke(runner, ["status"], db_path)
        assert "Pending:        1" in result.output

        # 4. Generate report
        mock_fetch.return_value = Changelog(
            image="docker.io/crazymax/diun",
            github_repo="crazy-max/diun",
            releases=[{"tag_name": "v4.31.0", "name": "v4.31.0", "body": "Bug fixes", "published_at": "2024-01-15"}],
        )
        mock_analyze.return_value = (
            "## docker.io/crazymax/diun\n**Summary**: Bug fixes\n**Risk**: 🟢 Safe\n",
            "test-model",
        )

        result = invoke(runner, ["report"], db_path)
        assert result.exit_code == 0
        assert "ShipLog Report" in result.output
        assert "Report saved" in result.output

        # 5. List pending — should be empty
        result = invoke(runner, ["list"], db_path)
        assert "No pending updates" in result.output

        # 6. Status shows 0 pending, 1 report
        result = invoke(runner, ["status"], db_path)
        assert "Pending:        0" in result.output
        assert "Reports:        1" in result.output
