"""Tests for shiplog.ntfy — notification formatting and sending."""

from unittest.mock import patch

from shiplog.ntfy import _markdown_to_ntfy, is_configured, send


class TestMarkdownToNtfy:
    def test_h1_becomes_bold(self):
        assert _markdown_to_ntfy("# Hello") == "**Hello**"

    def test_h2_becomes_bold(self):
        assert _markdown_to_ntfy("## Section") == "**Section**"

    def test_h3_becomes_bold(self):
        assert _markdown_to_ntfy("### Subsection") == "**Subsection**"

    def test_h6_becomes_bold(self):
        assert _markdown_to_ntfy("###### Deep") == "**Deep**"

    def test_non_header_unchanged(self):
        assert _markdown_to_ntfy("Just text") == "Just text"

    def test_bold_unchanged(self):
        assert _markdown_to_ntfy("**already bold**") == "**already bold**"

    def test_bullet_list_unchanged(self):
        assert _markdown_to_ntfy("- item one") == "- item one"

    def test_empty_string(self):
        assert _markdown_to_ntfy("") == ""

    def test_header_with_emoji(self):
        assert _markdown_to_ntfy("## TL;DR 🟢") == "**TL;DR 🟢**"

    def test_header_with_arrow(self):
        assert _markdown_to_ntfy("## docker.io/traefik/traefik → v3.6.13") == "**docker.io/traefik/traefik → v3.6.13**"

    def test_mixed_content(self):
        report = (
            "# ShipLog Report\n"
            "\n"
            "*Model: test*\n"
            "\n"
            "## traefik → v3.6.13\n"
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
        result = _markdown_to_ntfy(report)

        assert "**ShipLog Report**" in result
        assert "**traefik → v3.6.13**" in result
        assert "**TL;DR**" in result
        # Non-headers preserved
        assert "*Model: test*" in result
        assert "- Fix annotation handling" in result
        assert "- Bump dependency" in result
        assert "All safe." in result
        # No raw # left
        assert "\n# " not in result
        assert "\n## " not in result

    def test_hash_in_middle_of_line_not_converted(self):
        assert _markdown_to_ntfy("Issue #123 fixed") == "Issue #123 fixed"

    def test_code_with_hash_unchanged(self):
        assert _markdown_to_ntfy("`# comment`") == "`# comment`"

    def test_multiline_preserves_blank_lines(self):
        text = "# Title\n\nParagraph\n\n## Section"
        result = _markdown_to_ntfy(text)
        assert result == "**Title**\n\nParagraph\n\n**Section**"

    def test_header_no_space_not_converted(self):
        # "#word" without space is not a markdown header
        assert _markdown_to_ntfy("#notaheader") == "#notaheader"


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
            # Should not raise, just no-op
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
                assert headers["Markdown"] == "yes"
                assert headers["Authorization"] == "Bearer tk_secret"
                # Body should have converted headers
                body = call_args[1]["content"].decode("utf-8")
                assert "**Report**" in body
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
