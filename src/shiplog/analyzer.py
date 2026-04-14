"""LLM analysis via OpenAI-compatible inference API."""

import os
import re

import httpx

from shiplog.changelog import Changelog

DEFAULT_MODEL = "gcp/google/gemini-2.5-flash-lite"

SYSTEM_PROMPT = """\
You are ShipLog, a container update analyst for a homelab operator.

You receive structured changelog data for container image updates.
Each image has:
- A DETECTED VERSION section with full release notes for the new version
- A FLAGGED SIGNALS section (if any) highlighting security issues and
  breaking changes found across recent releases — DO NOT ignore these
- A list of other recent releases (one-line summaries for context)

For each updated container, produce:
1. **Summary**: One sentence on what changed in the detected version.
   Use the exact detected version tag — do not substitute internal
   version numbers from the release notes.
2. **Risk Level**: 🟢 Safe / 🟡 Review / 🔴 Breaking
   - If FLAGGED SIGNALS contains security items → at minimum 🟡 Review
   - If FLAGGED SIGNALS contains breaking/migration items → 🔴 Breaking
3. **Key Changes**: Bullet points of what matters to a homelab operator
4. **Action**: "Update now", "Read changelog first", or "Hold — breaking changes"
5. **Breaking Changes**: Migration steps if any, otherwise "None"

Be direct. The reader is technical. They care about security fixes,
breaking changes, and major features. Skip CI changes, dependency
bumps, and contributor acknowledgments.

If no changelog is available, say so briefly and link to the project.

Format as markdown. Use ## headers per image. End with ## TL;DR
summarizing overall risk and which images need attention.\
"""


# Patterns that indicate security-relevant content
_SECURITY_PATTERNS = [
    re.compile(r"CVE-\d{4}-\d+", re.IGNORECASE),
    re.compile(r"\bsecurity\b", re.IGNORECASE),
    re.compile(r"\bvulnerabilit", re.IGNORECASE),  # vulnerability, vulnerabilities
    re.compile(r"\bXSS\b"),
    re.compile(r"\bSQL injection\b", re.IGNORECASE),
    re.compile(r"\bRCE\b"),
    re.compile(r"\bauth(?:entication|orization)\s+bypass\b", re.IGNORECASE),
]

# Patterns that indicate breaking changes
_BREAKING_PATTERNS = [
    re.compile(r"\bbreaking\s+change", re.IGNORECASE),
    re.compile(r"\bBREAKING\b"),
    re.compile(r"\bmigrat(?:e|ion)\b", re.IGNORECASE),
    re.compile(r"\bdeprecated?\b", re.IGNORECASE),
    re.compile(r"\bremoved\b", re.IGNORECASE),
    re.compile(r"\brename[ds]?\b", re.IGNORECASE),
]


def _extract_signals(body: str) -> dict[str, list[str]]:
    """Extract security and breaking-change signals from release notes.

    Returns {"security": [...], "breaking": [...]} with matching lines.
    """
    signals: dict[str, list[str]] = {"security": [], "breaking": []}
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for pat in _SECURITY_PATTERNS:
            if pat.search(stripped):
                signals["security"].append(stripped)
                break
        for pat in _BREAKING_PATTERNS:
            if pat.search(stripped):
                signals["breaking"].append(stripped)
                break
    return signals


def _summarize_release_oneline(r: dict) -> str:
    """One-line summary of a release for context."""
    tag = r["tag_name"]
    date = r["published_at"][:10] if r.get("published_at") else "?"
    body = r.get("body") or ""
    signals = _extract_signals(body)
    flags = []
    if signals["security"]:
        flags.append("🔒 security")
    if signals["breaking"]:
        flags.append("⚠️ breaking")
    flag_str = f"  [{', '.join(flags)}]" if flags else ""
    return f"- {tag} ({date}){flag_str}"


def build_prompt(changelogs: list[Changelog]) -> str:
    """Build the user prompt from a list of changelogs.

    For each image:
    - The detected version's full release notes are included
    - Older releases are condensed to one-line summaries
    - Security/breaking signals are extracted and highlighted
    """
    sections = []
    for cl in changelogs:
        header = f"## {cl.image} → {cl.tag}" if cl.tag else f"## {cl.image}"
        if cl.github_repo:
            header += f"\nGitHub: https://github.com/{cl.github_repo}"

        if cl.error:
            sections.append(f"{header}\n\n⚠️ {cl.error}")
            continue

        if not cl.releases:
            sections.append(f"{header}\n\nNo releases found.")
            continue

        # Split into detected release and older context.
        # Releases come from the API in newest-first order, so everything
        # before the detected release is newer, and everything after is older.
        normalized_tag = cl.tag.lstrip("v")
        detected_release = None
        older_releases = []
        newer_releases = []

        for r in cl.releases:
            rtag = r["tag_name"].lstrip("v")
            if rtag == normalized_tag:
                detected_release = r
            elif detected_release is None:
                # Haven't found detected yet — these are newer
                newer_releases.append(r)
            else:
                older_releases.append(r)

        # Build the section
        parts = [header]

        # Primary: detected version's full notes
        if detected_release:
            body = detected_release["body"] or "(no release notes)"
            if len(body) > 3000:
                body = body[:3000] + "\n\n... (truncated)"
            date = detected_release["published_at"][:10] if detected_release.get("published_at") else "?"
            parts.append(f"\n### DETECTED VERSION: {detected_release['tag_name']} ({date})\n\n{body}")

            # Extract signals from the detected release
            signals = _extract_signals(detected_release["body"] or "")
        else:
            # Couldn't match the tag — include the most recent release as best guess
            r = cl.releases[0]
            body = r["body"] or "(no release notes)"
            if len(body) > 3000:
                body = body[:3000] + "\n\n... (truncated)"
            date = r["published_at"][:10] if r.get("published_at") else "?"
            parts.append(f"\n### LATEST RELEASE: {r['tag_name']} ({date}) (could not match detected tag {cl.tag})\n\n{body}")
            signals = _extract_signals(r["body"] or "")

        # Also scan older releases for security/breaking signals
        all_context_releases = newer_releases + older_releases
        for r in all_context_releases:
            r_signals = _extract_signals(r.get("body") or "")
            signals["security"].extend(
                f"[{r['tag_name']}] {line}" for line in r_signals["security"]
            )
            signals["breaking"].extend(
                f"[{r['tag_name']}] {line}" for line in r_signals["breaking"]
            )

        # Add signal alerts
        if signals["security"] or signals["breaking"]:
            parts.append("\n### ⚠️ FLAGGED SIGNALS (from this and nearby releases)")
            if signals["security"]:
                parts.append("\nSecurity:")
                for line in signals["security"][:10]:
                    parts.append(f"  - {line}")
            if signals["breaking"]:
                parts.append("\nBreaking/Migration:")
                for line in signals["breaking"][:10]:
                    parts.append(f"  - {line}")

        # Condensed context for other releases
        if all_context_releases:
            parts.append("\n### Other recent releases (for context)")
            for r in all_context_releases[:5]:
                parts.append(_summarize_release_oneline(r))

        sections.append("\n".join(parts))

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
    api_url = os.environ.get("LLM_API_URL")
    if not api_key:
        raise RuntimeError(
            "LLM_API_KEY is not set. Add it to your .env file."
        )
    if not api_url:
        raise RuntimeError(
            "LLM_API_URL is not set. Add it to your .env file.\n"
            "Example: LLM_API_URL=https://api.openai.com/v1/chat/completions"
        )

    model_used = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
    user_prompt = build_prompt(changelogs)

    with httpx.Client(timeout=180.0) as client:
        resp = client.post(
            api_url,
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
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()
