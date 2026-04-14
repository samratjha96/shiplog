"""Tests for shiplog.analyzer — LLM prompt building and think-block stripping."""

from unittest.mock import patch

import pytest

from shiplog.analyzer import (
    _extract_signals,
    _strip_think_blocks,
    _summarize_release_oneline,
    analyze,
    build_prompt,
)
from shiplog.changelog import Changelog


class TestExtractSignals:
    def test_cve_detected(self):
        body = "### Bug Fixes\n* Patch XSS in org invite (CVE-2025-1234)"
        signals = _extract_signals(body)
        assert len(signals["security"]) == 1
        assert "CVE-2025-1234" in signals["security"][0]

    def test_multiple_cves(self):
        body = "Fixed CVE-2025-1111 and CVE-2025-2222"
        signals = _extract_signals(body)
        assert len(signals["security"]) == 1  # same line
        assert "CVE-2025-1111" in signals["security"][0]

    def test_security_keyword(self):
        body = "Security fix for token validation"
        signals = _extract_signals(body)
        assert len(signals["security"]) == 1

    def test_vulnerability_keyword(self):
        body = "Patched a vulnerability in auth flow"
        signals = _extract_signals(body)
        assert len(signals["security"]) == 1

    def test_xss_detected(self):
        body = "Fixed XSS in admin panel"
        signals = _extract_signals(body)
        assert len(signals["security"]) == 1

    def test_rce_detected(self):
        body = "Mitigated RCE via crafted input"
        signals = _extract_signals(body)
        assert len(signals["security"]) == 1

    def test_breaking_change(self):
        body = "BREAKING: config format changed"
        signals = _extract_signals(body)
        assert len(signals["breaking"]) == 1

    def test_breaking_change_phrase(self):
        body = "This is a breaking change to the API"
        signals = _extract_signals(body)
        assert len(signals["breaking"]) == 1

    def test_migration_detected(self):
        body = "Run the database migration before upgrading"
        signals = _extract_signals(body)
        assert len(signals["breaking"]) == 1

    def test_deprecated(self):
        body = "The old config key is deprecated"
        signals = _extract_signals(body)
        assert len(signals["breaking"]) == 1

    def test_removed(self):
        body = "Removed support for v1 API"
        signals = _extract_signals(body)
        assert len(signals["breaking"]) == 1

    def test_no_signals(self):
        body = "Bug fixes and performance improvements"
        signals = _extract_signals(body)
        assert signals["security"] == []
        assert signals["breaking"] == []

    def test_empty_body(self):
        signals = _extract_signals("")
        assert signals["security"] == []
        assert signals["breaking"] == []

    def test_both_security_and_breaking(self):
        body = "Fixed CVE-2025-9999\nBREAKING: new auth flow required"
        signals = _extract_signals(body)
        assert len(signals["security"]) == 1
        assert len(signals["breaking"]) == 1


class TestSummarizeReleaseOneline:
    def test_plain_release(self):
        r = {"tag_name": "v1.2.3", "published_at": "2025-03-15T10:00:00Z", "body": "Bug fix"}
        line = _summarize_release_oneline(r)
        assert line == "- v1.2.3 (2025-03-15)"

    def test_release_with_security(self):
        r = {"tag_name": "v1.2.2", "published_at": "2025-03-10", "body": "Fixed CVE-2025-1234"}
        line = _summarize_release_oneline(r)
        assert "🔒 security" in line

    def test_release_with_breaking(self):
        r = {"tag_name": "v2.0.0", "published_at": "2025-01-01", "body": "BREAKING: new config format"}
        line = _summarize_release_oneline(r)
        assert "⚠️ breaking" in line

    def test_release_with_both(self):
        r = {"tag_name": "v2.0.0", "published_at": "2025-01-01", "body": "CVE-2025-5555\nBREAKING: migration required"}
        line = _summarize_release_oneline(r)
        assert "🔒 security" in line
        assert "⚠️ breaking" in line

    def test_no_published_at(self):
        r = {"tag_name": "v1.0.0", "published_at": "", "body": "Initial release"}
        line = _summarize_release_oneline(r)
        assert "(?)" in line


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
        assert "DETECTED VERSION: v3.4.0" in prompt
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
        assert "→ v1.130.3" in prompt
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

    def test_model_precedence_flag_over_env(self):
        """--model flag beats LLM_MODEL env var beats DEFAULT_MODEL."""
        from shiplog.analyzer import DEFAULT_MODEL

        cl = Changelog(image="img", github_repo="o/r", releases=[], tag="v1")
        with patch.dict("os.environ", {
            "LLM_API_KEY": "k",
            "LLM_API_URL": "http://fake",
            "LLM_MODEL": "env-model",
        }):
            with patch("shiplog.analyzer.httpx.Client") as mock_cls:
                mock_client = mock_cls.return_value.__enter__.return_value
                mock_client.post.return_value.status_code = 200
                mock_client.post.return_value.raise_for_status = lambda: None
                mock_client.post.return_value.json.return_value = {
                    "choices": [{"message": {"content": "report"}}]
                }

                # Flag takes priority
                _, model = analyze([cl], model="flag-model")
                assert model == "flag-model"

                # Without flag, env var wins
                _, model = analyze([cl])
                assert model == "env-model"

        # Without flag or env, default wins
        with patch.dict("os.environ", {
            "LLM_API_KEY": "k",
            "LLM_API_URL": "http://fake",
        }, clear=True):
            with patch("shiplog.analyzer.httpx.Client") as mock_cls:
                mock_client = mock_cls.return_value.__enter__.return_value
                mock_client.post.return_value.status_code = 200
                mock_client.post.return_value.raise_for_status = lambda: None
                mock_client.post.return_value.json.return_value = {
                    "choices": [{"message": {"content": "report"}}]
                }

                _, model = analyze([cl])
                assert model == DEFAULT_MODEL
