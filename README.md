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
uv tool install git+https://github.com/samratjha96/shiplog
```

Or run directly without installing:

```bash
uvx --from git+https://github.com/samratjha96/shiplog shiplog --help
```

## Setup

### 1. Configure environment

```bash
mkdir -p ~/.config/shiplog
cp .env.example ~/.config/shiplog/.env
```

Edit `~/.config/shiplog/.env`:

```bash
# Required — any OpenAI-compatible API
LLM_API_URL=https://api.openai.com/v1/chat/completions
LLM_API_KEY=sk-...

# Optional — higher GitHub rate limits (60 → 5000 req/hr)
GITHUB_TOKEN=ghp_...

# Optional — push notifications via ntfy
NTFY_TOPIC=shiplog-updates
```

ShipLog looks for `.env` in `~/.config/shiplog/` first, then the current directory.

### 2. Wire up diun

Add ShipLog as a script notifier in your `diun.yml`:

```yaml
notif:
  script:
    cmd: "shiplog"
    args:
      - "ingest"
```

That's it. Every time diun detects a new image version, it calls `shiplog ingest` which reads the `DIUN_ENTRY_*` environment variables that diun passes and stores the update in SQLite.

If you run diun in Docker, mount the shiplog config and DB:

```yaml
services:
  diun:
    image: crazymax/diun:latest
    volumes:
      - "./data:/data"
      - "/var/run/docker.sock:/var/run/docker.sock"
      - "$HOME/.config/shiplog:/.config/shiplog"
      - "$HOME/.local/share/shiplog:/.local/share/shiplog"
    environment:
      - "DIUN_WATCH_SCHEDULE=0 */6 * * *"
      - "DIUN_PROVIDERS_DOCKER=true"
      - "DIUN_NOTIF_SCRIPT_CMD=shiplog"
      - "DIUN_NOTIF_SCRIPT_ARGS=ingest"
```

### 3. Scan your compose files

ShipLog can auto-detect GitHub repos for most images:

```bash
shiplog scan docker-compose.yml
# or just run from a directory with docker-compose.yml:
shiplog scan
```

This resolves repos from Docker Hub descriptions, ghcr.io paths, and lscr.io (LinuxServer) images. For anything it can't resolve, it prints the exact `shiplog map` commands you need:

```
Auto-resolved (7):
  docker.io/traefik/traefik → traefik/traefik
  docker.io/vaultwarden/server → dani-garcia/vaultwarden
  lscr.io/linuxserver/sonarr → linuxserver/docker-sonarr
  ...

Could not resolve (3):
  ghcr.io/home-assistant/home-assistant
  ghcr.io/immich-app/immich-server
  docker.io/jellyfin/jellyfin

Add mappings manually:
  shiplog map ghcr.io/home-assistant/home-assistant home-assistant/core
  shiplog map ghcr.io/immich-app/immich-server immich-app/immich
  shiplog map docker.io/jellyfin/jellyfin jellyfin/jellyfin
```

### 4. Schedule reports

Add a cron job to generate reports on your preferred cadence:

```bash
# Weekly Monday 8am
0 8 * * 1 shiplog report

# Daily, write to file
0 8 * * * shiplog report -o ~/reports/shiplog-$(date +\%F).md

# With ntfy configured, you'll get a push notification automatically
```

## Usage

```bash
shiplog status                     # check config and pending updates
shiplog scan                       # auto-detect repos from docker-compose.yml
shiplog scan ~/homelab/*.yml       # scan specific compose files
shiplog list                       # pending (unreported) updates
shiplog list --all                 # include already-reported
shiplog report                     # generate report, mark as reported, notify
shiplog report --dry-run           # preview without side effects
shiplog report --model gpt-4o     # override the LLM model
shiplog report -o report.md       # write to file
shiplog map                        # list all image → repo mappings
shiplog map <image> <owner/repo>   # add a mapping
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

All configuration is via `~/.config/shiplog/.env` (or environment variables).

| Variable | Required | Description |
|---|---|---|
| `LLM_API_URL` | Yes | OpenAI-compatible chat completions endpoint |
| `LLM_API_KEY` | Yes | API key for the LLM endpoint |
| `LLM_MODEL` | No | Model to use (default: `gcp/google/gemini-2.5-flash-lite`) |
| `GITHUB_TOKEN` | No | GitHub PAT for higher rate limits |
| `SHIPLOG_DB_PATH` | No | SQLite path (default: `~/.local/share/shiplog/shiplog.db`) |
| `NTFY_TOPIC` | No | ntfy topic — enables push notifications |
| `NTFY_ENDPOINT` | No | ntfy server (default: `https://ntfy.sh`) |
| `NTFY_TOKEN` | No | ntfy access token for private topics |
| `NTFY_PRIORITY` | No | Message priority 1–5 (default: 3) |

## Notifications

When `NTFY_TOPIC` is set, `shiplog report` pushes the report to [ntfy](https://ntfy.sh) after generating it. Install the ntfy app on your phone, subscribe to your topic, and you'll get upgrade reports as push notifications.

Dry runs (`--dry-run`) do not send notifications.

## Changelog Resolution

ShipLog fetches changelogs from GitHub Releases. It resolves image → repo mappings from:

1. **Explicit mappings** — set with `shiplog map`
2. **ghcr.io paths** — `ghcr.io/owner/repo` → tries `owner/repo` on GitHub
3. **lscr.io images** — looks up the equivalent Docker Hub page for LinuxServer images
4. **Docker Hub descriptions** — extracts GitHub URLs from the full description

All candidates are validated against the GitHub API before use. If a repo can't be validated, the changelog is skipped — ShipLog never makes requests to unvalidated destinations.

## Development

```bash
git clone https://github.com/samratjha96/shiplog && cd shiplog
uv sync
uv run pytest tests/ -v
uv run pytest tests/ -v --network   # include tests that hit GitHub/Docker Hub
```
