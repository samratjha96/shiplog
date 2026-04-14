"""Send notifications via ntfy (https://ntfy.sh)."""

import os

import httpx


def is_configured() -> bool:
    """Check if ntfy is configured (topic is the minimum requirement)."""
    return bool(os.environ.get("NTFY_TOPIC"))


def send(report: str, title: str = "ShipLog Report") -> None:
    """Send a report to ntfy. No-op if not configured.

    Raises httpx errors on failure so the caller can handle them.
    """
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return

    endpoint = os.environ.get("NTFY_ENDPOINT", "https://ntfy.sh")
    token = os.environ.get("NTFY_TOKEN")
    priority = os.environ.get("NTFY_PRIORITY", "3")

    url = f"{endpoint.rstrip('/')}/{topic}"

    headers: dict[str, str] = {
        "Title": title,
        "Priority": priority,
        "Tags": "package,ship",
        "Markdown": "yes",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with httpx.Client(timeout=15.0) as client:
        resp = client.post(url, content=report.encode("utf-8"), headers=headers)
        resp.raise_for_status()
