# ShipLog

AI-powered container update reports for homelab operators.

ShipLog integrates with [diun](https://crazymax.dev/diun/) to turn raw "new image version available" signals into actionable upgrade reports. It fetches changelogs from GitHub, sends them to an LLM, and outputs concise markdown reports with risk levels, key changes, and upgrade recommendations.

## How It Works

```
diun detects update → calls `shiplog ingest` → SQLite
                                                  ↓
                            shiplog report → fetch changelogs → LLM → markdown report
                                                                        ↓
                                                              stdout / file / ntfy
```

## Installation

```bash
# From git
uvx --from git+https://github.com/<user>/shiplog shiplog --help

# Or clone and install
git clone <repo-url> && cd shiplog
uv sync
```

## Setup

### 1. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your values:

```bash
# Required — any OpenAI-compatible API
LLM_API_URL=https://api.openai.com/v1/chat/completions
LLM_API_KEY=sk-...

# Optional — higher GitHub rate limits (60 → 5000 req/hr)
GITHUB_TOKEN=ghp_...

# Optional — push notifications via ntfy
NTFY_TOPIC=shiplog-updates
```

### 2. Configure diun

Add ShipLog as a script notifier in your `diun.yml`:

```yaml
notif:
  script:
    cmd: "shiplog"
    args:
      - "ingest"
```

See [`example/diun.yml`](example/diun.yml) for a complete example.

### 3. Map images to GitHub repos

ShipLog auto-detects GitHub repos from Docker Hub descriptions and `ghcr.io` paths. For images it can't resolve, add mappings manually:

```bash
shiplog map docker.io/linuxserver/sonarr linuxserver/docker-sonarr
shiplog map docker.io/homeassistant/home-assistant home-assistant/core
shiplog map ghcr.io/immich-app/immich-server immich-app/immich
```

Run `shiplog map` (no arguments) to see all current mappings.

## Usage

```bash
# Check status and configuration
shiplog status

# List pending (unreported) updates
shiplog list
shiplog list --all    # include already-reported

# Generate a report
shiplog report
shiplog report --dry-run                           # preview without marking as reported
shiplog report --model gpt-4o                      # use a different model
shiplog report -o ~/reports/report-$(date +%F).md  # write to file

# Manage image → GitHub repo mappings
shiplog map                                                          # list all
shiplog map docker.io/linuxserver/sonarr linuxserver/docker-sonarr   # add one
```

### Cron

ShipLog doesn't schedule itself — use cron:

```bash
# Generate a report every Monday morning
0 8 * * 1 cd /path/to/shiplog && shiplog report >> /var/log/shiplog.md
```

### Example Report

```markdown
# ShipLog Report — 2025-01-15 10:00 UTC

## docker.io/traefik/traefik → v3.6.13

**Summary**: Bug fixes and documentation improvements.
**Risk Level**: 🟢 Safe
**Key Changes**:
- Fix rewrite-target annotation handling in Kubernetes Ingress
- Bumped compression library dependency
**Action**: Update now

## docker.io/vaultwarden/server → 1.35.7

**Summary**: Fixed 2FA bug on Android clients.
**Risk Level**: 🟢 Safe
**Action**: Update now

## TL;DR
All updates are 🟢 Safe — update at your next maintenance window.
```

## Configuration

All configuration is via `.env` file (or environment variables). Copy `.env.example` to get started.

| Variable | Required | Description |
|---|---|---|
| `LLM_API_URL` | Yes | OpenAI-compatible chat completions endpoint |
| `LLM_API_KEY` | Yes | API key for the LLM endpoint |
| `LLM_MODEL` | No | Model to use (default: `gcp/google/gemini-2.5-flash-lite`) |
| `GITHUB_TOKEN` | No | GitHub PAT for higher rate limits |
| `SHIPLOG_DB_PATH` | No | SQLite path (default: `~/.local/share/shiplog/shiplog.db`) |
| `NTFY_TOPIC` | No | ntfy topic (enables push notifications) |
| `NTFY_ENDPOINT` | No | ntfy server (default: `https://ntfy.sh`) |
| `NTFY_TOKEN` | No | ntfy access token for private topics |
| `NTFY_PRIORITY` | No | Message priority 1-5 (default: 3) |

## Notifications

When `NTFY_TOPIC` is set, `shiplog report` automatically pushes the report to [ntfy](https://ntfy.sh) after generating it. Subscribe to your topic on your phone or desktop to get notified.

Dry runs (`--dry-run`) do not send notifications.

## Changelog Resolution

ShipLog fetches changelogs from GitHub Releases. It resolves image → repo mappings from:

1. **Explicit mappings** — set with `shiplog map`
2. **ghcr.io paths** — `ghcr.io/owner/repo` → tries `owner/repo` on GitHub
3. **Docker Hub descriptions** — extracts GitHub URLs from the full description

All candidates are validated against the GitHub API before use. If a repo can't be validated, the changelog is skipped — no requests to unvalidated destinations.

## Development

```bash
uv sync
uv run pytest tests/ -v
uv run pytest tests/ -v --network   # include tests that hit GitHub/Docker Hub
```
