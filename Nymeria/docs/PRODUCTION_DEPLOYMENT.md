# Nymeria Production Deployment Guide

This document outlines deployment options for Nymeria, from local development to full Docker-based production deployments.

## Deployment Options Overview

- **Local**: run directly on host (`python run.py api`, or other `run.py` subcommands as needed)
- **Docker Compose**: production-oriented split services (`api`, `worker`, `postgres`, `redis`, `mcp`, optional chat/voice services)

## Quick Start

### Local Development (Recommended for Most Users)

```bash
cd Nymeria
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Optional: only needed when local development uses DATABASE_BACKEND=postgres
pip install -r requirements-postgres.txt

# Optional: Install BrowserAgent dependencies
pip install playwright
playwright install chromium

# Optional: Install Claude Code CLI
npm install -g @anthropic-ai/claude-code

# Configure
cp .env.docker.example .env.docker
# Edit .env.docker with your API keys

# Run
python run.py api
```

### Docker Compose (Server/Multi-device)

```bash
cd Nymeria
cp .env.docker.example .env.docker
# Edit .env.docker with your API keys and secrets

docker compose --env-file .env.docker up -d
```

Docker Compose builds two Nymeria application images:

- `nymeria-full:local` from `Dockerfile.full` for `api`, `worker`, and
  `twitch-bot`. These services own a `NymeriaAgent` or execute scheduled agent
  work, so they keep the Kali/browser/CLI workstation runtime used by
  shell-capable and browser-capable tools.
- `nymeria-slim:local` from `Dockerfile.slim` for `watchdog`, `discord-bot`,
  `telegram-bot`, `slack-bot`, `matrix-bot`, `mattermost-bot`, `zulip-bot`,
  `rocketchat-bot`, `signal-bot`, and `mcp`.
  These processes are HTTP thin clients over the API and do not need Kali
  tools, Playwright browsers, Node.js, Claude Code CLI, or test dependencies.
  WhatsApp Cloud API, Messenger Platform, Instagram Messaging, Webex Messaging,
  Microsoft Teams, Google Chat, and LINE webhooks are handled by the API
  container.

Both images install runtime requirements only. Install `requirements-dev.txt`
on the host when running backend tests, coverage, or Ruff lint checks.

The GitHub Actions CI workflow validates backend Ruff linting, pytest coverage,
and the Docker path on every push and pull request. The Docker job renders the
default and optional-profile Compose configs from `.env.docker.example` and
runs BuildKit's Dockerfile check against both `Dockerfile.full` and
`Dockerfile.slim`.

### Release Versioning

The Python package version is defined once in `Nymeria/nymeria/__init__.py`;
`Nymeria/pyproject.toml` reads that value through setuptools dynamic metadata.
The desktop release metadata must stay in sync with it:

- `nymeria-desktop/package.json`
- `nymeria-desktop/src-tauri/Cargo.toml`
- `nymeria-desktop/src-tauri/tauri.conf.json`
- `nymeria-desktop/src-tauri/Cargo.lock`

Check the current checkout before tagging:

```bash
python scripts/sync_versions.py --check
```

For a beta release, bump all manifests together and then create a matching
`v`-prefixed tag:

```bash
python scripts/sync_versions.py --set 0.2.0-beta.1
python scripts/sync_versions.py --tag v0.2.0-beta.1
git tag v0.2.0-beta.1
```

When the release workflow is enabled, pushing the tag is what starts the
package and desktop publishing jobs.

### Docker Health Checks

Docker Compose owns the health checks for Nymeria services. The Nymeria
application Dockerfiles intentionally do not define a built-in `HEALTHCHECK`
because the images run different commands in different containers.

The API container uses `GET /health`. Worker, watchdog, Discord, Telegram, and
Twitch containers write runtime heartbeat files under `/tmp/nymeria-health/`;
Compose validates those heartbeats with `python -m nymeria.core.service_health`.
Those checks fail when the heartbeat is stale, the heartbeat PID is gone, the
service reports an unhealthy client/ticker loop, or required dependencies such
as the API, PostgreSQL, or Redis are unavailable.

Use `docker compose --env-file .env.docker ps` for the container health summary.
For a direct check inside a container, run a service-specific command such as:

```bash
docker compose --env-file .env.docker exec worker \
  python -m nymeria.core.service_health check worker
```

## Architecture

### Docker Deployment Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Docker Network                               │
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │  PostgreSQL  │  │    Redis     │  │   Nymeria Services    │  │
│  │              │  │              │  │                       │  │
│  │  Checkpoint  │  │  Event Bus   │  │  - API container      │  │
│  │  Storage     │  │  (Pub/Sub)   │  │  - Worker container   │  │
│  │              │  │              │  │  - MCP container      │  │
│  └──────────────┘  └──────────────┘  │  - Optional Discord   │  │
│                                      └───────────────────────┘  │
│                                                 │                │
│  Volumes:                                       │                │
│  - postgres_data: Conversation history          │                │
│  - redis_data: Event persistence                │                │
│  - nymeria_data: Memories, TODOs, profiles      │                │
│  - nymeria_workspace: Project files             │                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
              Access from any device via API
              (Desktop app, mobile, other agents)
```

## Tool Availability by Deployment

| Capability | Local | Docker Compose |
|------------|-------|----------------|
| Core tools | ✅ Host system | ✅ Containerized |
| Optional and callable-thread tools | ✅ | ✅ |
| Browser automation | ⚠️ Install Playwright/Chromium | ✅ Included in `nymeria-full` agent services |
| `claude_code` integration | ⚠️ Install Claude Code CLI | ⚠️ Available only where configured in `nymeria-full` |
| Self-modification persistence | ✅ (local filesystem) | ✅ (via mounted volumes) |

## Configuration

### Environment Variables

See `.env.docker.example` for all available options. Key settings:

```bash
# Required
POSTGRES_PASSWORD=<secure-password>
REDIS_PASSWORD=<secure-url-safe-password>
NYMERIA_SERVICE_TOKEN=<admin-service-token>   # see docs/accounts.md

# LLM (at least one required)
ANTHROPIC_API_KEY=sk-ant-...
OPENROUTER_API_KEY=sk-or-...
OPENAI_API_KEY=sk-...

# LLM Settings
LLM_PROVIDER=anthropic  # or openrouter, openai
LLM_MODEL=claude-sonnet-4-6

# Optional Integrations
PERPLEXITY_API_KEY=pplx-...      # Web search
TELEGRAM_BOT_TOKEN=...           # Telegram notifications
DISCORD_WEBHOOK_URL=...          # Discord notifications
SLACK_WEBHOOK_URL=...            # Slack notifications
MATTERMOST_BASE_URL=...          # Mattermost bot/tools server URL
MATTERMOST_ACCESS_TOKEN=...      # Mattermost bot account token
ZULIP_BASE_URL=...               # Zulip realm URL
ZULIP_EMAIL=...                  # Zulip bot email
ZULIP_API_KEY=...                # Zulip bot API key
ROCKETCHAT_BASE_URL=...          # Rocket.Chat server URL
ROCKETCHAT_USER_ID=...           # Rocket.Chat bot/user ID
ROCKETCHAT_AUTH_TOKEN=...        # Rocket.Chat bot/user token
SIGNAL_HTTP_URL=...              # signal-cli-rest-api base URL
SIGNAL_ACCOUNT=+15551234567      # Signal bot account phone number
WEBEX_ACCESS_TOKEN=...           # Webex webhook replies
WEBEX_WEBHOOK_SECRET=...         # Webex webhook HMAC secret
TEAMS_BOT_APP_ID=...             # Microsoft Teams Bot Framework app ID
TEAMS_BOT_APP_PASSWORD=...       # Microsoft Teams Bot Framework client secret
GOOGLE_CHAT_SERVICE_ACCOUNT_FILE=/run/secrets/google-chat-service-account.json # Google Chat replies
LINE_CHANNEL_ACCESS_TOKEN=...    # LINE Messaging API webhook replies
LINE_CHANNEL_SECRET=...          # LINE webhook HMAC secret
WHATSAPP_ACCESS_TOKEN=...        # WhatsApp Cloud API webhook replies
WHATSAPP_PHONE_NUMBER_ID=...     # WhatsApp Cloud API sender
WHATSAPP_WEBHOOK_VERIFY_TOKEN=... # Meta webhook challenge token
MESSENGER_PAGE_ACCESS_TOKEN=...  # Messenger Send API webhook replies
MESSENGER_PAGE_ID=...            # Facebook Page ID
MESSENGER_WEBHOOK_VERIFY_TOKEN=... # Messenger webhook challenge token
INSTAGRAM_ACCESS_TOKEN=...       # Instagram Messaging webhook replies
INSTAGRAM_IG_USER_ID=...         # Instagram professional account ID
INSTAGRAM_WEBHOOK_VERIFY_TOKEN=... # Instagram webhook challenge token
```

### CORS for Remote Access

To access from other devices:

```bash
# Allow specific origins
CORS_ORIGINS=http://192.168.1.100:1420,http://myphone.local:1420
```

Do not use `CORS_ORIGINS=*`. The API allows credentialed CORS requests and
refuses to start when wildcard origins are configured.

### Messaging Integrations

Telegram, Discord, Slack, Matrix, Mattermost, Zulip, Rocket.Chat, Signal, and Twitch
bots run as dedicated bot containers. Configure their tokens in `.env.docker`
and enable their Docker Compose profiles; those clients do not need external
webhook URLs. See `docs/telegram-bot.md`, `docs/discord-bot.md`,
`docs/slack-bot.md`, `docs/matrix-bot.md`, `docs/mattermost-bot.md`,
`docs/zulip-bot.md`, `docs/rocketchat-bot.md`, `docs/signal-bot.md`, and
`docs/twitch-bot.md`. Signal additionally requires a separately managed
`signal-cli-rest-api` daemon in JSON-RPC/SSE mode.

WhatsApp, Messenger, Instagram, Webex, Microsoft Teams, Google Chat, and LINE are API-hosted webhook integrations.
WhatsApp uses the official WhatsApp Business Cloud API at
`/integrations/whatsapp/webhook`; configure a public HTTPS callback URL in Meta
and set `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, and
`WHATSAPP_WEBHOOK_VERIFY_TOKEN`; see `docs/whatsapp-bot.md`. Messenger uses
Meta Messenger Platform webhooks at `/integrations/messenger/webhook`;
configure a public HTTPS callback URL in Meta and set
`MESSENGER_PAGE_ACCESS_TOKEN`, `MESSENGER_PAGE_ID`, and
`MESSENGER_WEBHOOK_VERIFY_TOKEN`; see `docs/messenger-bot.md`. Instagram uses
Meta Instagram Messaging webhooks at `/integrations/instagram/webhook`;
configure a public HTTPS callback URL in Meta and set
`INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_IG_USER_ID`, and
`INSTAGRAM_WEBHOOK_VERIFY_TOKEN`; see `docs/instagram-bot.md`. Webex uses Webex
Messaging webhooks at `/integrations/webex/webhook`; configure a public HTTPS
callback URL in Webex and set `WEBEX_ACCESS_TOKEN` and optionally
`WEBEX_WEBHOOK_SECRET`; see `docs/webex-bot.md`. Microsoft Teams uses Bot
Framework message activities at `/integrations/teams/webhook`; configure that
URL as the bot messaging endpoint and set `TEAMS_BOT_APP_ID` and
`TEAMS_BOT_APP_PASSWORD`; see `docs/teams-bot.md`. Google Chat uses interaction
events at `/integrations/google-chat/webhook`; configure that URL as the Chat
app endpoint and set `GOOGLE_CHAT_SERVICE_ACCOUNT_FILE` or
`GOOGLE_CHAT_SERVICE_ACCOUNT_JSON`; see `docs/google-chat-bot.md`. LINE uses
Messaging API webhooks at `/integrations/line/webhook`; configure that URL in
the LINE Developers Console and set `LINE_CHANNEL_ACCESS_TOKEN` and
`LINE_CHANNEL_SECRET`; see `docs/line-bot.md`.

For event-driven automations from external services (IFTTT, Zapier, etc.),
use the trigger system: `POST /triggers/fire/{trigger_id}`. See `docs/triggers.md`.

## MCP Server (Agent-to-Agent Communication)

Nymeria can expose an MCP server for other AI agents to use her capabilities.
The MCP process is a thin client: it talks to the running REST/SSE API with
`NYMERIA_SERVICE_TOKEN` and uses `X-Nymeria-Act-As` for user-scoped tools.
`python run.py mcp` exits during startup if that token is missing, matching the
worker, bot, watchdog, and foreground service launch checks.

### Local MCP

```bash
# STDIO mode (for Claude Code, etc.)
python run.py mcp --api-url http://localhost:8000

# HTTP mode (for network access)
python run.py mcp --http --port 8001 --api-url http://localhost:8000
```

### Docker MCP

The `mcp` service starts by default in `docker compose --env-file .env.docker up -d`, waits for the API health check, and runs:

```bash
python run.py mcp --http --host 0.0.0.0 --port 8001 --api-url http://nymeria-api:8000
```

```yaml
# In docker-compose.yml
ports:
  - "8000:8000"  # REST API
  - "8001:8001"  # MCP Server (HTTP mode)
```

Configure Claude Code to use Nymeria:
```json
{
  "mcpServers": {
    "nymeria": {
      "url": "http://localhost:8001"
    }
  }
}
```

## Scaling

### Horizontal Scaling (API)

The API process can be scaled behind a load balancer:

```bash
docker compose --env-file .env.docker up -d --scale api=3
```

When Redis is enabled, the API container creates the event bus connection and runs with ticker disabled, so SSE clients can still receive worker-published autonomous events.

### Single Worker

The worker (`python run.py worker`) should only run one instance to avoid duplicate scheduled-task execution.

## Backup & Recovery

### Data Locations

| Data | Location | Backup Method |
|------|----------|---------------|
| Conversations | PostgreSQL | `pg_dump` |
| Memories/Profiles | /data/users/ | Volume backup |
| TODOs | /data/todos/ | Volume backup |
| Custom Tools | /data/custom_tools/ | Volume backup |
| Self-modifications | Host repo `./nymeria` (bind-mounted to `/app/nymeria`) | Git + host filesystem backup |

### Backup Script

```bash
# Backup all volumes
docker run --rm \
  -v nymeria_data:/data \
  -v $(pwd)/backup:/backup \
  alpine tar czf /backup/nymeria-backup-$(date +%Y%m%d).tar.gz /data

# Backup PostgreSQL
docker exec nymeria-postgres pg_dump -U nymeria nymeria > backup.sql
```

## Secrets Management

`Nymeria/.env.docker` and other secret files are encrypted at rest in the repo via [git-crypt](https://github.com/AGWA/git-crypt). After `git-crypt unlock`, they are transparent in the working tree and bind-mounted directly into containers.

- **Fresh clone:** `git-crypt unlock /path/to/nymeria-gitcrypt.key`, or copy `.env.docker.example` and fill in your own keys.
- **Per-machine drift:** Each machine may have different values in `.env.docker` (different API keys, proxy URLs, etc.). A modified `.env.docker` in `git status` is expected — only commit when updating the shared baseline.
- **Rotation:** See `docs/git-crypt.md` for per-secret rotation checklists covering LLM API keys, CLIProxy OAuth, Postgres, Redis, service tokens, Fernet keys, and Firebase/Google credentials.
- **Docker images:** `.env.docker` is excluded from the Docker build context; Compose injects it with `--env-file` and bind-mounts it at runtime instead of copying secrets into image layers.
- **CLIProxy OAuth tokens** are per-machine and gitignored at `CLIProxyAPI-main/temp/latest/auths/` — they are not managed by git-crypt. Never copy them between machines.

## Reverse Proxy (Caddy)

The compose stack ships with a Caddy reverse proxy that terminates TLS and
fronts the api/mcp containers. Production hosts should expose **only** 80
and 443 to the public internet — the api (8000) and mcp (8001) ports are
bound to `127.0.0.1` inside the host and only reachable via SSH tunnel.

### One-time setup

1. Point a DNS A record at the host (e.g. `nymeria.example.com` → host IP).
2. Set `NYMERIA_HOSTNAME` and `ACME_EMAIL` in `.env.docker`. Caddy will
   auto-issue a Let's Encrypt cert on first request.
3. Open 80 and 443 on the host firewall, close 8000 and 8001:
   ```
   sudo ufw allow 80/tcp
   sudo ufw allow 443/tcp
   sudo ufw delete allow 8000/tcp 2>/dev/null
   sudo ufw delete allow 8001/tcp 2>/dev/null
   sudo ufw reload
   ```
4. `docker compose up -d caddy` — Caddy joins the `edge` network and is
   the only host-published service.

If you don't have a domain yet, leave `NYMERIA_HOSTNAME` unset. Caddy
listens on `:80` over plain HTTP and you can front it with Cloudflare
Tunnel, Tailscale Funnel, or an SSH tunnel for testing — none of those
need a public TLS cert.

### Local-dev tunnel

When developing against the live host, SSH-tunnel the API port:

```
ssh -L 8000:127.0.0.1:8000 nymeria@<host>
```

The desktop and mobile apps can then point at `http://localhost:8000`
just as they do for local Docker.

## Security Considerations

1. **Account tokens**: Per-user bearer tokens (`nym_…`) are minted via `python run.py users add`. The legacy shared `NYMERIA_API_KEY` was retired — see `docs/accounts.md`. Bots/ticker/watchdog authenticate with the admin `NYMERIA_SERVICE_TOKEN` plus `X-Nymeria-Act-As: <user_id>` for per-user routing.
2. **Secrets at rest**: `Nymeria/.env.docker`, `.env`, `firebase-service-account.json`, and `google_credentials.json` are git-crypt encrypted. See `docs/git-crypt.md` for policy, rotation, and history-rewriting decisions.
3. **CORS**: Restrict origins in production. `CORS_ORIGINS` should list your `NYMERIA_HOSTNAME` (and the local Tauri origins for desktop/mobile clients) — no wildcards.
4. **Trigger secrets**: Per-trigger shared secrets for webhook fire endpoints (see `docs/triggers.md`)
5. **Network**: TLS is terminated at the Caddy reverse proxy (above). Only 80/443 should be open on the host firewall; 8000/8001 are loopback-only.
6. **Container privileges**: Two privilege tiers. **Agent-bearing containers** (api, worker) run as `root` with `cap_drop: ALL` plus three install caps (`DAC_OVERRIDE`, `CHOWN`, `FOWNER`) — exactly what `apt-get install`, `pip install` (system-wide), and `/etc` writes need. This lets the agent extend its own sandbox at runtime via `bash_execute`. **Thin-client containers** (watchdog, mcp, chat bots, caddy) run as the non-root `nymeria` user (uid 999) with `cap_drop: ALL`, `read_only: true` rootfs, and tmpfs for `/tmp` + `/home/nymeria` — they never need to write to the rootfs and shouldn't be able to. `no-new-privileges:true` applies to every service; the kernel rejects `setuid` and `sudo` escalation everywhere.
7. **Network segmentation**: Two Docker networks — `edge` (proxy + api + mcp + bots + cli-proxy + hexstrike) and `backend` (postgres + redis + api + worker). Bots cannot reach the database directly even if compromised.
8. **Resource limits**: Every service declares `mem_limit`, `cpus`, and `pids_limit` (see the `x-limits-*` anchors in `docker-compose.yml`). A runaway tool call cannot exhaust host memory or fork-bomb the kernel.
9. **Bind mounts**: `./nymeria`, `run.py`, and `.env.docker` are mounted **read-only** into containers. A container compromise cannot backdoor the host source tree or rewrite secrets. `.env.docker` must stay out of image layers and is ignored by the Docker build context.
10. **Kali Tools**: The full image includes nmap, hydra, sqlmap, etc. for `bash_execute` access. Use responsibly and only on authorized targets. Since the container is non-root, nmap loses SYN-scan privileges and falls back to TCP-connect scans.

## Troubleshooting

### Container won't start

```bash
docker compose --env-file .env.docker logs api
```

Common issues:
- Missing environment variables
- PostgreSQL not ready (wait a few seconds)
- Port already in use

### BrowserAgent not working

Ensure Playwright is installed and Chromium is available:
```bash
docker exec nymeria-api playwright install chromium
```

### Self-modifications lost after restart

Ensure code/data mounts are intact in `docker-compose.yml`:
```yaml
volumes:
  - ./nymeria:/app/nymeria
  - ./run.py:/app/run.py
  - ./.env.docker:/app/.env.docker
  - nymeria_data:/data
```

## Upgrading

```bash
git pull
docker compose --env-file .env.docker build
docker compose --env-file .env.docker up -d
```

Self-modifications and runtime config are preserved by bind-mounted code/config files (`./nymeria`, `run.py`, `.env.docker`) plus the `nymeria_data` volume. Re-apply carefully after upstream upgrades if merge conflicts occur. Compose tags the shared app images as `nymeria-full:local` and `nymeria-slim:local`, so rebuilding updates the grouped services instead of creating separate duplicate images for each command.
