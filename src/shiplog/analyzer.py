"""LLM analysis via OpenAI-compatible inference API."""

import os

import httpx

from shiplog.changelog import Changelog

LLM_API_URL = "https://your-llm-api.example.com/v1/chat/completions"
DEFAULT_MODEL = "gcp/google/gemini-2.5-flash-lite"

SYSTEM_PROMPT = """\
You are ShipLog, a container update analyst for a homelab operator.
You receive changelog data for container image updates and produce
concise, actionable reports.

For each updated container, provide:
1. **Summary**: One sentence on what changed
2. **Risk Level**: 🟢 Safe / 🟡 Review / 🔴 Breaking
3. **Key Changes**: Bullet points of what actually matters
4. **Action**: "Update now", "Read changelog first", or "Skip this version"
5. **Breaking Changes**: Any migration steps needed

Be direct. Skip boilerplate. The reader is technical and runs
a homelab — they care about breaking changes, security fixes,
and major features. They don't care about CI changes, dependency
bumps, or contributor acknowledgments.

If no changelog is available for an image, say so briefly and
recommend checking the project's release page manually.

Format the entire response as a single markdown document.
Use ## headers for each container image.
End with a brief "## TL;DR" section summarizing the overall
risk level and which images need attention.\
"""


def build_prompt(changelogs: list[Changelog]) -> str:
    """Build the user prompt from a list of changelogs."""
    sections = []
    for cl in changelogs:
        header = f"## {cl.image}"
        if cl.github_repo:
            header += f" (https://github.com/{cl.github_repo})"

        if cl.error:
            sections.append(f"{header}\n\n⚠️ {cl.error}\n")
            continue

        release_text = []
        for r in cl.releases:
            body = r["body"] or "(no release notes)"
            # Truncate very long release bodies to stay within context
            if len(body) > 3000:
                body = body[:3000] + "\n\n... (truncated)"
            release_text.append(
                f"### {r['tag_name']} ({r['published_at'][:10] if r['published_at'] else 'unknown date'})\n\n{body}"
            )

        sections.append(f"{header}\n\n" + "\n\n".join(release_text))

    return (
        "Here are the container image updates and their changelogs.\n"
        "Analyze each one and produce the report.\n\n"
        + "\n\n---\n\n".join(sections)
    )


def analyze(
    changelogs: list[Changelog],
    model: str | None = None,
) -> tuple[str, str]:
    """Send changelogs to the LLM and get back a report.

    Returns (report_content, model_used).
    Raises if LLM_API_KEY is not set or API call fails.
    """
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        raise RuntimeError(
            "LLM_API_KEY environment variable is not set.\n"
            "Get a key from the OpenAI-compatible inference API portal and export it."
        )

    model_used = model or DEFAULT_MODEL
    user_prompt = build_prompt(changelogs)

    with httpx.Client(timeout=180.0) as client:
        resp = client.post(
            LLM_API_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json={
                "model": model_used,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 4096,
                "temperature": 0.3,
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

    # Strip any <think>...</think> blocks leaked by reasoning models
    content = _strip_think_blocks(content)

    return content, model_used


def _strip_think_blocks(text: str) -> str:
    """Remove <think>...</think> blocks from LLM output."""
    import re
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()
