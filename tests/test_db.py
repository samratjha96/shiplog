"""Tests for shiplog.db — SQLite state management."""

import sqlite3

import pytest

from shiplog import db


@pytest.fixture
def conn(tmp_path):
    """Fresh in-memory-like DB for each test."""
    path = tmp_path / "test.db"
    return db.connect(path)


class TestSchema:
    def test_tables_exist(self, conn):
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = [t["name"] for t in tables]
        assert "updates" in names
        assert "reports" in names
        assert "github_mappings" in names

    def test_indices_exist(self, conn):
        indices = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
        ).fetchall()
        names = [i["name"] for i in indices]
        assert "idx_updates_reported" in names
        assert "idx_updates_image" in names

    def test_connect_is_idempotent(self, tmp_path):
        """Calling connect twice on the same DB doesn't crash."""
        path = tmp_path / "test.db"
        c1 = db.connect(path)
        c2 = db.connect(path)
        c1.close()
        c2.close()


class TestUpdates:
    def test_insert_and_query(self, conn):
        row_id = db.insert_update(
            conn,
            image="docker.io/crazymax/diun",
            tag="v4.31.0",
            digest="sha256:abc123",
            status="new",
            platform="linux/amd64",
            provider="docker",
        )
        assert row_id == 1

        pending = db.get_pending_updates(conn)
        assert len(pending) == 1
        assert pending[0]["image"] == "docker.io/crazymax/diun"
        assert pending[0]["tag"] == "v4.31.0"
        assert pending[0]["status"] == "new"
        assert pending[0]["reported"] == 0

    def test_get_all_updates(self, conn):
        db.insert_update(conn, image="img1", tag="v1", status="new")
        db.insert_update(conn, image="img2", tag="v2", status="update")

        all_updates = db.get_all_updates(conn)
        assert len(all_updates) == 2

    def test_mark_reported(self, conn):
        id1 = db.insert_update(conn, image="img1", tag="v1", status="new")
        id2 = db.insert_update(conn, image="img2", tag="v2", status="update")

        report_id = db.insert_report(conn, model="test-model", content="test report")
        db.mark_reported(conn, [id1, id2], report_id)

        pending = db.get_pending_updates(conn)
        assert len(pending) == 0

        all_updates = db.get_all_updates(conn)
        assert all(u["reported"] == 1 for u in all_updates)
        assert all(u["report_id"] == report_id for u in all_updates)

    def test_mark_reported_empty_list(self, conn):
        """Marking empty list doesn't crash."""
        db.mark_reported(conn, [], 1)

    def test_insert_with_metadata(self, conn):
        row_id = db.insert_update(
            conn,
            image="img",
            tag="v1",
            status="new",
            metadata={"extra": "data"},
        )
        row = conn.execute("SELECT metadata FROM updates WHERE id = ?", (row_id,)).fetchone()
        assert '"extra"' in row["metadata"]


class TestReports:
    def test_insert_and_get(self, conn):
        report_id = db.insert_report(conn, model="llama-70b", content="# Report\n\nHello")
        assert report_id == 1

        report = db.get_report(conn, report_id)
        assert report["model"] == "llama-70b"
        assert "Hello" in report["content"]

    def test_get_nonexistent(self, conn):
        assert db.get_report(conn, 999) is None

    def test_get_all_reports(self, conn):
        db.insert_report(conn, model="m1", content="r1")
        db.insert_report(conn, model="m2", content="r2")

        reports = db.get_all_reports(conn)
        assert len(reports) == 2


class TestGitHubMappings:
    def test_set_and_get(self, conn):
        db.set_github_mapping(conn, "docker.io/crazymax/diun", "crazy-max/diun")
        result = db.get_github_mapping(conn, "docker.io/crazymax/diun")
        assert result == "crazy-max/diun"

    def test_get_nonexistent(self, conn):
        assert db.get_github_mapping(conn, "nonexistent") is None

    def test_upsert(self, conn):
        db.set_github_mapping(conn, "img", "old/repo")
        db.set_github_mapping(conn, "img", "new/repo")
        assert db.get_github_mapping(conn, "img") == "new/repo"

    def test_auto_detected_flag(self, conn):
        db.set_github_mapping(conn, "img", "owner/repo", auto_detected=True)
        rows = db.get_all_github_mappings(conn)
        assert rows[0]["auto_detected"] == 1

    def test_delete_mapping(self, conn):
        db.set_github_mapping(conn, "img", "owner/repo")
        assert db.delete_github_mapping(conn, "img") is True
        assert db.get_github_mapping(conn, "img") is None

    def test_delete_nonexistent_mapping(self, conn):
        assert db.delete_github_mapping(conn, "nonexistent") is False

    def test_get_all(self, conn):
        db.set_github_mapping(conn, "a_img", "a/repo")
        db.set_github_mapping(conn, "b_img", "b/repo")
        rows = db.get_all_github_mappings(conn)
        assert len(rows) == 2
        # Ordered by image
        assert rows[0]["image"] == "a_img"
