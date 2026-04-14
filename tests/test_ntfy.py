"""Tests for shiplog.ntfy — notification formatting and sending."""

from unittest.mock import patch

from shiplog.ntfy import _markdown_to_plain, is_configured, send


class TestMarkdownToPlain:
    def test_h1_becomes_uppercase(self):
        assert _markdown_to_plain("# Hello") == "HELLO"

    def test_h2_becomes_uppercase(self):
        assert _markdown_to_plain("## Section Title") == "SECTION TITLE"

    def test_h3_becomes_uppercase(self):
        assert _markdown_to_plain("### Sub") == "SUB"

    def test_non_header_unchanged(self):
        assert _markdown_to_plain("Just text") == "Just text"

    def test_bold_becomes_uppercase(self):
        assert _markdown_to_plain("**Summary**: Bug fix") == "SUMMARY: Bug fix"

    def test_multiple_bold_in_line(self):
        assert _markdown_to_plain("**Risk**: 🟢 **Safe**") == "RISK: 🟢 SAFE"

    def test_italic_stripped(self):
        assert _markdown_to_plain("*Model: test*") == "Model: test"

    def test_backtick_code_stripped(self):
        assert _markdown_to_plain("Run `shiplog report`") == "Run shiplog report"

    def test_link_converted(self):
        assert _markdown_to_plain("[click here](https://example.com)") == "click here (https://example.com)"

    def test_bullet_dash_to_dot(self):
        assert _markdown_to_plain("- item one") == "• item one"

    def test_bullet_asterisk_to_dot(self):
        assert _markdown_to_plain("* item one") == "• item one"

    def test_nested_bullet_preserved(self):
        assert _markdown_to_plain("  - nested") == "  • nested"

    def test_empty_string(self):
        assert _markdown_to_plain("") == ""

    def test_header_with_emoji(self):
        assert _markdown_to_plain("## TL;DR 🟢") == "TL;DR 🟢"

    def test_header_with_arrow(self):
        assert _markdown_to_plain("## docker.io/traefik → v3.6.13") == "DOCKER.IO/TRAEFIK → V3.6.13"

    def test_hash_in_middle_not_converted(self):
        assert _markdown_to_plain("Issue #123 fixed") == "Issue #123 fixed"

    def test_hash_no_space_not_converted(self):
        assert _markdown_to_plain("#notaheader") == "#notaheader"

    def test_multiline_preserves_blank_lines(self):
        text = "# Title\n\nParagraph\n\n## Section"
        result = _markdown_to_plain(text)
        assert result == "TITLE\n\nParagraph\n\nSECTION"

    def test_full_report(self):
        report = (
            "# ShipLog Report — 2025-01-15\n"
            "\n"
            "*Model: gcp/google/gemini-2.5-flash-lite*\n"
            "\n"
            "## docker.io/traefik/traefik → v3.6.13\n"
            "\n"
            "**Summary**: Bug fixes.\n"
            "**Risk Level**: 🟢 Safe\n"
            "**Key Changes**:\n"
            "- Fix annotation handling\n"
            "- Bump dependency\n"
            "**Action**: Update now\n"
            "\n"
            "## TL;DR\n"
            "All safe."
        )
        result = _markdown_to_plain(report)

        assert "SHIPLOG REPORT — 2025-01-15" in result
        assert "DOCKER.IO/TRAEFIK/TRAEFIK → V3.6.13" in result
        assert "TL;DR" in result
        assert "SUMMARY: Bug fixes." in result
        assert "RISK LEVEL: 🟢 Safe" in result  # Safe not uppercased (not bold)
        assert "• Fix annotation handling" in result
        assert "• Bump dependency" in result
        assert "Model: gcp/google/gemini-2.5-flash-lite" in result  # italic stripped
        assert "All safe." in result
        # No markdown artifacts
        assert "**" not in result
        assert "##" not in result


class TestIsConfigured:
    def test_configured_with_topic(self):
        with patch.dict("os.environ", {"NTFY_TOPIC": "my-topic"}):
            assert is_configured() is True

    def test_not_configured_without_topic(self):
        with patch.dict("os.environ", {}, clear=True):
            assert is_configured() is False

    def test_empty_topic_not_configured(self):
        with patch.dict("os.environ", {"NTFY_TOPIC": ""}):
            assert is_configured() is False


class TestSend:
    def test_noop_when_not_configured(self):
        with patch.dict("os.environ", {}, clear=True):
            send("report text")

    def test_sends_with_correct_headers(self):
        with patch.dict("os.environ", {
            "NTFY_TOPIC": "test-topic",
            "NTFY_ENDPOINT": "https://ntfy.example.com",
            "NTFY_TOKEN": "tk_secret",
            "NTFY_PRIORITY": "5",
        }):
            with patch("shiplog.ntfy.httpx.Client") as mock_client_cls:
                mock_client = mock_client_cls.return_value.__enter__.return_value
                mock_client.post.return_value.raise_for_status = lambda: None

                send("# Report\n\nContent", title="Test Title")

                mock_client.post.assert_called_once()
                call_args = mock_client.post.call_args
                assert call_args[0][0] == "https://ntfy.example.com/test-topic"
                headers = call_args[1]["headers"]
                assert headers["Title"] == "Test Title"
                assert headers["Priority"] == "5"
                assert headers["Authorization"] == "Bearer tk_secret"
                assert "Markdown" not in headers
                # Body should be plain text
                body = call_args[1]["content"].decode("utf-8")
                assert "REPORT" in body
                assert "# Report" not in body

    def test_default_endpoint_is_ntfy_sh(self):
        with patch.dict("os.environ", {"NTFY_TOPIC": "t"}, clear=True):
            with patch("shiplog.ntfy.httpx.Client") as mock_client_cls:
                mock_client = mock_client_cls.return_value.__enter__.return_value
                mock_client.post.return_value.raise_for_status = lambda: None

                send("text")

                url = mock_client.post.call_args[0][0]
                assert url == "https://ntfy.sh/t"

    def test_no_auth_header_without_token(self):
        with patch.dict("os.environ", {"NTFY_TOPIC": "t"}, clear=True):
            with patch("shiplog.ntfy.httpx.Client") as mock_client_cls:
                mock_client = mock_client_cls.return_value.__enter__.return_value
                mock_client.post.return_value.raise_for_status = lambda: None

                send("text")

                headers = mock_client.post.call_args[1]["headers"]
                assert "Authorization" not in headers
