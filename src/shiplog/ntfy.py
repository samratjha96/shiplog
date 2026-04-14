"""Send notifications via ntfy (https://ntfy.sh)."""

import os

import httpx


def is_configured() -> bool:
    """Check if ntfy is configured (topic is the minimum requirement)."""
    return bool(os.environ.get("NTFY_TOPIC"))


def send(report: str, title: str = "ShipLog Report") -> None:
    """Send a report to ntfy. No-op if not configured.

    Converts the markdown report to plain text for reliable rendering
    across all ntfy clients.

    Raises httpx errors on failure so the caller can handle them.
    """
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return

    endpoint = os.environ.get("NTFY_ENDPOINT", "https://ntfy.sh")
    token = os.environ.get("NTFY_TOKEN")
    priority = os.environ.get("NTFY_PRIORITY", "3")

    url = f"{endpoint.rstrip('/')}/{topic}"

    body = _markdown_to_plain(report)

    headers: dict[str, str] = {
        "Title": title,
        "Priority": priority,
        "Tags": "package,ship",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with httpx.Client(timeout=15.0) as client:
        resp = client.post(url, content=body.encode("utf-8"), headers=headers)
        resp.raise_for_status()


def _markdown_to_plain(text: str) -> str:
    """Convert markdown to clean plain text for ntfy.

    ntfy clients inconsistently render markdown,
    so we strip it to plain text that looks good everywhere.
    """
    import re

    lines = []
    for line in text.splitlines():
        # ## Header → HEADER (uppercase for visual weight)
        m = re.match(r'^(#{1,6})\s+(.+)$', line)
        if m:
            lines.append(m.group(2).upper())
            continue

        # **bold** → BOLD
        line = re.sub(r'\*\*(.+?)\*\*', lambda m: m.group(1).upper(), line)

        # *italic* → italic (just remove the markers)
        line = re.sub(r'\*(.+?)\*', r'\1', line)

        # `code` → code (just remove backticks)
        line = re.sub(r'`(.+?)`', r'\1', line)

        # [text](url) → text (url)
        line = re.sub(r'\[(.+?)\]\((.+?)\)', r'\1 (\2)', line)

        # - bullet → • bullet
        line = re.sub(r'^(\s*)[-*]\s+', r'\1• ', line)

        lines.append(line)
    return "\n".join(lines)
