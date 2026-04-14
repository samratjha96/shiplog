# ShipLog

AI-powered container update reports for homelab operators.

ShipLog integrates with [diun](https://crazymax.dev/diun/) to turn raw "new image version available" signals into actionable upgrade reports. It fetches changelogs from GitHub, sends them to an LLM, and outputs concise markdown reports with risk levels, key changes, and upgrade recommendations.

## How It Works

```
diun detects update → calls `shiplog ingest` → SQLite
                                                  ↓
                            shiplog report → fetch changelogs → LLM → markdown report
```

1. **Diun** monitors your container registries for new image versions
2. When an update is found, diun calls `shiplog ingest` via its script notifier
3. ShipLog stores the update event in SQLite
4. On demand, `shiplog report` fetches changelogs from GitHub Releases, sends them to an LLM, and outputs a structured report

## Installation

```bash
# Clone and install with uv
git clone <repo-url> && cd shiplog
uv sync

# Or install directly
uv pip install .
```

## Setup

### 1. Set environment variables

```bash
# Required for report generation
export LLM_API_KEY="your-key-here"

# Optional: higher GitHub API rate limits (60/hr → 5000/hr)
export GITHUB_TOKEN="ghp_your_token"

# Optional: custom database location (default: ~/.local/share/shiplog/shiplog.db)
export SHIPLOG_DB_PATH="/path/to/shiplog.db"
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
```

## Usage

```bash
# Check status
shiplog status

# List pending (unreported) updates
shiplog list

# List all updates (including already-reported ones)
shiplog list --all

# Generate an AI report for pending updates
shiplog report

# Dry run — see the report without marking updates as reported
shiplog report --dry-run

# Use a specific LLM model
shiplog report --model "gcp/google/gemini-2.5-pro"

# Write report to a file
shiplog report -o /var/log/shiplog/report-$(date +%F).md

# List all past reports
shiplog reports

# Show a previous report
shiplog show 1

# Map an image to its GitHub repo
shiplog map docker.io/linuxserver/sonarr linuxserver/docker-sonarr

# Remove a mapping
shiplog unmap docker.io/linuxserver/sonarr

# View all mappings
shiplog mappings

# Test without diun — manually ingest an image
shiplog test-ingest docker.io/crazymax/diun:v4.31.0

# Clean up old reported updates from the database
shiplog purge
```

### Example Report Output

```
# ShipLog Report — 2024-01-15 10:00 UTC

## docker.io/crazymax/diun

**Summary**: Added Kubernetes namespace negation and Matrix notification support.
**Risk Level**: 🟢 Safe
**Key Changes**:
- Support for negating Kubernetes namespaces
- New Matrix server options for notifications
- Go 1.25 and Alpine Linux 3.23 updates
**Action**: Update now
**Breaking Changes**: None

## docker.io/linuxserver/sonarr

**Summary**: Minor upstream update to 4.0.17.
**Risk Level**: 🟡 Review
**Key Changes**:
- Updates to upstream 4.0.17 (check linked changelog)
**Action**: Read changelog first

## TL;DR
- crazymax/diun: Safe (🟢) — update now
- linuxserver/sonarr: Review (🟡) — check changelog
```

### Cron Setup

ShipLog doesn't schedule itself — use cron:

```bash
# Generate a report every Monday morning
0 8 * * 1 shiplog report >> /var/log/shiplog-reports.md
```

## Configuration

| Environment Variable | Description | Default |
|---|---|---|
| `LLM_API_KEY` | OpenAI-compatible inference API key (required for reports) | — |
| `GITHUB_TOKEN` | GitHub personal access token (optional, higher rate limits) | — |
| `SHIPLOG_DB_PATH` | SQLite database path | `~/.local/share/shiplog/shiplog.db` |

The `--db` flag overrides `SHIPLOG_DB_PATH` for any command.

## Supported LLM Models

Any model available on the OpenAI-compatible inference API. Defaults:

| Model | Best For |
|---|---|
| `gcp/google/gemini-2.5-flash-lite` | Fast summarization (default) |
| `azure/anthropic/claude-opus-4-6` | Higher quality analysis |
| `gcp/google/gemini-2.5-pro` | Large context for big changelogs |

## Changelog Resolution

ShipLog fetches changelogs from GitHub Releases. It resolves image → repo mappings via:

1. **Explicit mappings** — set with `shiplog map`
2. **ghcr.io paths** — `ghcr.io/owner/repo` → tries `owner/repo` on GitHub
3. **Docker Hub descriptions** — extracts GitHub URLs from the full description

All candidates are validated against the GitHub API before use. If no repo can be resolved, the image is included in the report with a note to add a mapping.

## Development

```bash
# Install dev dependencies
uv sync

# Run tests (unit only)
uv run pytest tests/ -v

# Run tests including network (hits GitHub/Docker Hub APIs)
uv run pytest tests/ -v --network
```
