"""Tests for shiplog.analyzer — LLM prompt building and think-block stripping."""

from unittest.mock import patch

import pytest

from shiplog.analyzer import _strip_think_blocks, analyze, build_prompt
from shiplog.changelog import Changelog


class TestBuildPrompt:
    def test_single_image_with_releases(self):
        cl = Changelog(
            image="docker.io/crazymax/diun",
            github_repo="crazy-max/diun",
            releases=[
                {
                    "tag_name": "v4.31.0",
                    "name": "v4.31.0",
                    "body": "## What's Changed\n- Fixed a bug\n- Added feature X",
                    "published_at": "2024-01-15T10:00:00Z",
                },
            ],
        )
        prompt = build_prompt([cl])
        assert "docker.io/crazymax/diun" in prompt
        assert "github.com/crazy-max/diun" in prompt
        assert "v4.31.0" in prompt
        assert "Fixed a bug" in prompt

    def test_image_with_error(self):
        cl = Changelog(
            image="registry.local/myapp",
            github_repo=None,
            releases=[],
            error="No GitHub repo found.",
        )
        prompt = build_prompt([cl])
        assert "registry.local/myapp" in prompt
        assert "No GitHub repo found" in prompt

    def test_multiple_images(self):
        changelogs = [
            Changelog(image="img1", github_repo="a/b", releases=[
                {"tag_name": "v1", "name": "v1", "body": "Release 1", "published_at": "2024-01-01"},
            ]),
            Changelog(image="img2", github_repo="c/d", releases=[
                {"tag_name": "v2", "name": "v2", "body": "Release 2", "published_at": "2024-02-01"},
            ]),
        ]
        prompt = build_prompt(changelogs)
        assert "img1" in prompt
        assert "img2" in prompt
        assert "Release 1" in prompt
        assert "Release 2" in prompt

    def test_long_body_truncated(self):
        long_body = "x" * 5000
        cl = Changelog(
            image="img",
            github_repo="o/r",
            releases=[
                {"tag_name": "v1", "name": "v1", "body": long_body, "published_at": ""},
            ],
        )
        prompt = build_prompt([cl])
        assert "... (truncated)" in prompt
        # Should be cut to ~3000 chars
        assert len(prompt) < 4000

    def test_tag_included_in_prompt(self):
        cl = Changelog(
            image="docker.io/traefik",
            github_repo="traefik/traefik",
            releases=[
                {"tag_name": "v3.4.0", "name": "v3.4.0", "body": "New stuff", "published_at": "2024-03-01"},
            ],
            tag="v3.4.0",
        )
        prompt = build_prompt([cl])
        assert "Detected version: v3.4.0" in prompt
        assert "→ v3.4.0" in prompt

    def test_error_with_tag(self):
        cl = Changelog(
            image="ghcr.io/immich-app/immich-server",
            github_repo=None,
            releases=[],
            tag="v1.130.3",
            error="No GitHub repo found.",
        )
        prompt = build_prompt([cl])
        assert "Detected version: v1.130.3" in prompt
        assert "No GitHub repo found" in prompt

    def test_no_tag_omits_detected_line(self):
        cl = Changelog(
            image="img",
            github_repo="o/r",
            releases=[{"tag_name": "v1", "name": "v1", "body": "stuff", "published_at": ""}],
        )
        prompt = build_prompt([cl])
        assert "Detected version" not in prompt

    def test_empty_changelogs(self):
        prompt = build_prompt([])
        assert "Analyze" in prompt


class TestStripThinkBlocks:
    def test_removes_think_block(self):
        text = "<think>\nLet me reason...\n</think>\nHere is the actual response."
        assert _strip_think_blocks(text) == "Here is the actual response."

    def test_removes_multiple_think_blocks(self):
        text = "<think>first</think>Hello <think>second</think>world"
        assert _strip_think_blocks(text) == "Hello world"

    def test_no_think_blocks(self):
        text = "Just a normal response."
        assert _strip_think_blocks(text) == "Just a normal response."

    def test_empty_string(self):
        assert _strip_think_blocks("") == ""


class TestAnalyze:
    def test_missing_api_key(self):
        cl = Changelog(image="img", github_repo="o/r", releases=[])
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError, match="LLM_API_KEY"):
                analyze([cl])
