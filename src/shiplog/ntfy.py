"""Send notifications via ntfy (https://ntfy.sh)."""

import os

import httpx


def is_configured() -> bool:
    """Check if ntfy is configured (topic is the minimum requirement)."""
    return bool(os.environ.get("NTFY_TOPIC"))


def send(report: str, title: str = "ShipLog Report") -> None:
    """Send a report to ntfy. No-op if not configured.

    Converts the markdown report to ntfy-friendly format
    (ntfy doesn't render # headers).

    Raises httpx errors on failure so the caller can handle them.
    """
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return

    endpoint = os.environ.get("NTFY_ENDPOINT", "https://ntfy.sh")
    token = os.environ.get("NTFY_TOKEN")
    priority = os.environ.get("NTFY_PRIORITY", "3")

    url = f"{endpoint.rstrip('/')}/{topic}"

    body = _markdown_to_ntfy(report)

    headers: dict[str, str] = {
        "Title": title,
        "Priority": priority,
        "Tags": "package,ship",
        "Markdown": "yes",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with httpx.Client(timeout=15.0) as client:
        resp = client.post(url, content=body.encode("utf-8"), headers=headers)
        resp.raise_for_status()


def _markdown_to_ntfy(report: str) -> str:
    """Convert a markdown report to ntfy-friendly format.

    ntfy renders bold, italic, code, links, and lists,
    but NOT # headers. Convert headers to bold text.
    """
    import re

    lines = []
    for line in report.splitlines():
        # ## Header → **Header**
        m = re.match(r'^(#{1,6})\s+(.+)$', line)
        if m:
            lines.append(f"**{m.group(2)}**")
        else:
            lines.append(line)
    return "\n".join(lines)
