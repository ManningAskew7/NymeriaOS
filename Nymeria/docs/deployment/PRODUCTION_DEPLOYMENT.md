# Nymeria Production Deployment Guide

This document outlines deployment options for Nymeria, from local development to full Docker-based production deployments.

## Deployment Options Overview

- **Local**: run directly on host (`python3 run.py api`, or other `run.py` subcommands as needed)
- **Docker Compose**: production-oriented split services (`api`, `worker`, `watchdog`, `postgres`, `redis`, `caddy`, `mcp`, optional chat/voice services)

## Quick Start

### Local Development (Recommended for Most Users)

```bash
cd Nymeria
python3 -m pip install --user -r requirements.txt
python3 -m pip install --user -r requirements-dev.txt

# Optional: only needed when local development uses DATABASE_BACKEND=postgres
python3 -m pip install --user -r requirements-postgres.txt

# Optional: Install BrowserAgent dependencies
python3 -m pip install --user playwright
python3 -m playwright install chromium

# Optional: Install Claude Code CLI
npm install -g @anthropic-ai/claude-code

# Configure
cp .env.docker.example .env.docker
# Edit .env.docker with your API keys

# Run
python3 run.py api
```

### Docker Compose (Server/Multi-device)

```bash
cd Nymeria
cp .env.docker.example .env.docker
# Edit .env.docker with your API keys and secrets

docker compose --env-file .env.docker up -d
```

Docker Compose builds two Nymeria application images:

- `nymeria-full:local` from `Dockerfile.full` for `api` and `worker`.
  The API owns the Docker stack's `NymeriaAgent` runtime and keeps the
  Kali/browser/CLI workstation dependencies used by shell-capable and
  browser-capable tools. The worker is a scheduler/trigger relay that POSTs
  turns to the API, but it currently shares the full image in Compose.
- `nymeria-slim:local` from `Dockerfile.slim` for `watchdog`, `discord-bot`,
  `telegram-bot`, `slack-bot`, `matrix-bot`, `mattermost-bot`, `zulip-bot`,
  `rocketchat-bot`, `signal-bot`, and `mcp`.
  These processes are HTTP thin clients over the API and do not construct their
  own `NymeriaAgent`.
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

The API container uses `GET /ready` for its Compose health check; `/ready`
validates database and Redis readiness. Caddy checks the proxied `/health`
route, and thin-client services use `python -m nymeria.core.service_health`.
Worker, watchdog, and profiled chat bot processes write runtime heartbeat files
under `/tmp/nymeria-health/`. Those checks fail when the heartbeat is stale,
the heartbeat PID is gone, the service reports an unhealthy client/ticker loop,
or a service-specific dependency check fails. The worker validates PostgreSQL
and Redis access; watchdog, Discord, Slack, Matrix, Telegram, and MCP validate
API `/health`; MCP also validates its local TCP listener.

Use `docker compose --env-file .env.docker ps` for the container health summary.
For a direct check inside a container, run a service-specific command such as:

```bash
docker compose --env-file .env.docker exec worker \
  python -m nymeria.core.service_health check worker
```

## Architecture

### Docker Deployment Architecture

```text
+-------------------------------------------------------------------+
|                         Docker networks                           |
|                                                                   |
|  backend:                                                         |
|  +--------------+  +--------------+                               |
|  | PostgreSQL   |  | Redis        |                               |
|  | LangGraph    |  | pub/sub and  |                               |
|  | checkpoints  |  | cache state  |                               |
|  +--------------+  +--------------+                               |
|          ^                 ^                                       |
|          |                 |                                       |
|  +-------------------------------+        +---------------------+  |
|  | API container                 | <----> | Caddy reverse proxy |  |
|  | single agent runtime          |        | public 80/443       |  |
|  +-------------------------------+        +---------------------+  |
|          ^                                                        |
|          | REST/SSE with service token                            |
|  +-------------------------------+                                |
|  | Worker, watchdog, MCP, and    |                                |
|  | profiled chat bot containers  |                                |
|  +-------------------------------+                                |
|                                                                   |
|  Volumes:                                                         |
|  - postgres_data: LangGraph checkpoint database                   |
|  - redis_data: Redis server persistence                           |
|  - nymeria_data: accounts.db, users, TODOs, triggers, config      |
|  - nymeria_workspace: workspace files                             |
+-------------------------------------------------------------------+
```

## Tool Availability by Deployment

| Capability | Local | Docker Compose |
|------------|-------|----------------|
| Core tools | Host system | Containerized |
| Optional and callable-thread tools | Available | Available |
| Browser automation | Install Playwright/Chromium locally | Included in the `nymeria-full` API image |
| `claude_code` integration | Install Claude Code CLI locally | Available only where configured in `nymeria-full` |
| Self-modification persistence | Local filesystem | Mounted code/data volumes |

## Configuration

### Environment Variables

See `.env.docker.example` for all available options. Key settings:

```bash
# Required
POSTGRES_PASSWORD=<secure-password>
REDIS_PASSWORD=<secure-url-safe-password>
NYMERIA_SERVICE_TOKEN=<admin-service-token>   # see docs/accounts.md

# LLM (at least one required)
ANTHROPIC_API_KEY=sk-ant-<token>
OPENROUTER_API_KEY=sk-or-<token>
OPENAI_API_KEY=sk-<token>

# LLM Settings
LLM_PROVIDER=anthropic  # or openrouter, openai
LLM_MODEL=claude-sonnet-4-6

# Optional Integrations
PERPLEXITY_API_KEY=pplx-<token>  # Web search
TELEGRAM_BOT_TOKEN=<token>       # Telegram notifications
DISCORD_WEBHOOK_URL=<url>        # Discord notifications
SLACK_WEBHOOK_URL=<url>          # Slack notifications
MATTERMOST_BASE_URL=<url>        # Mattermost bot/tools server URL
MATTERMOST_ACCESS_TOKEN=<token>  # Mattermost bot account token
ZULIP_BASE_URL=<url>             # Zulip realm URL
ZULIP_EMAIL=<email>              # Zulip bot email
ZULIP_API_KEY=<token>            # Zulip bot API key
ROCKETCHAT_BASE_URL=<url>        # Rocket.Chat server URL
ROCKETCHAT_USER_ID=<user-id>     # Rocket.Chat bot/user ID
ROCKETCHAT_AUTH_TOKEN=<token>    # Rocket.Chat bot/user token
SIGNAL_HTTP_URL=<url>            # signal-cli-rest-api base URL
SIGNAL_ACCOUNT=+15551234567      # Signal bot account phone number
WEBEX_ACCESS_TOKEN=<token>       # Webex webhook replies
WEBEX_WEBHOOK_SECRET=<secret>    # Webex webhook HMAC secret
TEAMS_BOT_APP_ID=<app-id>        # Microsoft Teams Bot Framework app ID
TEAMS_BOT_APP_PASSWORD=<secret>  # Microsoft Teams Bot Framework client secret
GOOGLE_CHAT_SERVICE_ACCOUNT_FILE=/run/secrets/google-chat-service-account.json # Google Chat replies
LINE_CHANNEL_ACCESS_TOKEN=<token> # LINE Messaging API webhook replies
LINE_CHANNEL_SECRET=<secret>     # LINE webhook HMAC secret
WHATSAPP_ACCESS_TOKEN=<token>    # WhatsApp Cloud API webhook replies
WHATSAPP_PHONE_NUMBER_ID=<id>    # WhatsApp Cloud API sender
WHATSAPP_WEBHOOK_VERIFY_TOKEN=<token> # Meta webhook challenge token
MESSENGER_PAGE_ACCESS_TOKEN=<token> # Messenger Send API webhook replies
MESSENGER_PAGE_ID=<page-id>      # Facebook Page ID
MESSENGER_WEBHOOK_VERIFY_TOKEN=<token> # Messenger webhook challenge token
INSTAGRAM_ACCESS_TOKEN=<token>   # Instagram Messaging webhook replies
INSTAGRAM_IG_USER_ID=<id>        # Instagram professional account ID
INSTAGRAM_WEBHOOK_VERIFY_TOKEN=<token> # Instagram webhook challenge token
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

Telegram, Discord, Slack, Matrix, Mattermost, Zulip, Rocket.Chat, and Signal
bots run as dedicated profiled containers. Configure their tokens in
`.env.docker` and enable their Docker Compose profiles; those clients do not
need external webhook URLs. See
`chat-apps/telegram-bot.md`, `chat-apps/discord-bot.md`,
`chat-apps/slack-bot.md`, `chat-apps/matrix-bot.md`, `chat-apps/mattermost-bot.md`,
`chat-apps/zulip-bot.md`, `chat-apps/rocketchat-bot.md`, and `chat-apps/signal-bot.md`.
Signal additionally requires a separately managed
`signal-cli-rest-api` daemon in JSON-RPC/SSE mode.

WhatsApp, Messenger, Instagram, Webex, Microsoft Teams, Google Chat, and LINE are API-hosted webhook integrations.
WhatsApp uses the official WhatsApp Business Cloud API at
`/integrations/whatsapp/webhook`; configure a public HTTPS callback URL in Meta
and set `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, and
`WHATSAPP_WEBHOOK_VERIFY_TOKEN`; see `chat-apps/whatsapp-bot.md`. Messenger uses
Meta Messenger Platform webhooks at `/integrations/messenger/webhook`;
configure a public HTTPS callback URL in Meta and set
`MESSENGER_PAGE_ACCESS_TOKEN`, `MESSENGER_PAGE_ID`, and
`MESSENGER_WEBHOOK_VERIFY_TOKEN`; see `chat-apps/messenger-bot.md`. Instagram uses
Meta Instagram Messaging webhooks at `/integrations/instagram/webhook`;
configure a public HTTPS callback URL in Meta and set
`INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_IG_USER_ID`, and
`INSTAGRAM_WEBHOOK_VERIFY_TOKEN`; see `chat-apps/instagram-bot.md`. Webex uses Webex
Messaging webhooks at `/integrations/webex/webhook`; configure a public HTTPS
callback URL in Webex and set `WEBEX_ACCESS_TOKEN` and optionally
`WEBEX_WEBHOOK_SECRET`; see `chat-apps/webex-bot.md`. Microsoft Teams uses Bot
Framework message activities at `/integrations/teams/webhook`; configure that
URL as the bot messaging endpoint and set `TEAMS_BOT_APP_ID` and
`TEAMS_BOT_APP_PASSWORD`; see `chat-apps/teams-bot.md`. Google Chat uses interaction
events at `/integrations/google-chat/webhook`; configure that URL as the Chat
app endpoint and set `GOOGLE_CHAT_SERVICE_ACCOUNT_FILE` or
`GOOGLE_CHAT_SERVICE_ACCOUNT_JSON`; see `chat-apps/google-chat-bot.md`. LINE uses
Messaging API webhooks at `/integrations/line/webhook`; configure that URL in
the LINE Developers Console and set `LINE_CHANNEL_ACCESS_TOKEN` and
`LINE_CHANNEL_SECRET`; see `chat-apps/line-bot.md`.

For event-driven automations from external services (IFTTT, Zapier, etc.),
use the trigger system: `POST /triggers/fire/{trigger_id}`. See `docs/triggers.md`.

## MCP Server (Agent-to-Agent Communication)

Nymeria can expose an MCP server for other AI agents to use her capabilities.
The MCP process is a thin client: it talks to the running REST/SSE API with
`NYMERIA_SERVICE_TOKEN` and uses `X-Nymeria-Act-As` for user-scoped tools.
`python3 run.py mcp` exits during startup if that token is missing, matching the
worker, bot, watchdog, and foreground service launch checks.

### Local MCP

```bash
# STDIO mode (for Claude Code, etc.)
python3 run.py mcp --api-url http://localhost:8000

# HTTP mode (for network access)
python3 run.py mcp --http --port 8001 --api-url http://localhost:8000
```

### Docker MCP

The `mcp` service starts by default in `docker compose --env-file .env.docker up -d`, waits for the API health check, and runs:

```bash
python3 run.py mcp --http --host 0.0.0.0 --port 8001 --api-url http://nymeria-api:8000
```

```yaml
# In docker-compose.yml
api:
  ports:
    - "127.0.0.1:${API_PORT:-8000}:8000"
mcp:
  ports:
    - "127.0.0.1:${MCP_PORT:-8001}:8001"
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

### API Runtime

The stock Compose stack is a single-API runtime. Do not scale the API service
with `docker compose --scale api=<count>` in the current architecture: Compose sets
fixed container names, and agent coordination structures such as
`ThreadLockManager` and `PendingPromptQueue` are process-local. Redis pub/sub
lets the worker publish autonomous task events, but it does not coordinate
multiple API agent runtimes. Horizontal API scaling would require a separate
design for shared locks, queued sub-turns, sticky request routing, and container
naming.

### Single Worker

The worker (`python3 run.py worker`) should only run one instance to avoid duplicate scheduled-task execution.

In Docker the worker is a **scheduler-only thin client**: it polls due TODOs
and poll-based trigger sources, then relays each turn to the API container
via `POST /chat` (mirroring the watchdog). It no longer constructs a
`NymeriaAgent`, so its memory footprint drops by roughly the size of the
tool registry, LLM client, and MCP runtime, roughly 50 to 150 MB. The API
container's footprint grows by a comparable amount because it now serves
autonomous turns in addition to user chat. If the host is close to its memory
limit, tune the `api` and `worker` resource limits in `docker-compose.yml` to
match the workload.

The worker waits up to 30s on `/health` at startup before claiming the
first scheduled TODO, so the API doesn't need to be ready before the
worker container boots. The scheduled-TODO floor is API uptime,
not worker uptime. Plan API restarts accordingly.

## Backup & Recovery

### Data Locations

| Data | Location | Backup Method |
|------|----------|---------------|
| LangGraph checkpoints | `postgres_data` PostgreSQL volume | `pg_dump` |
| Accounts, tokens, chat bindings, credential vault | `/data/accounts.db` in `nymeria_data` | Volume backup plus secret-key backup |
| Users, memories, TODOs, triggers, skills, MCP servers, custom tools | `/data/<runtime paths>` in `nymeria_data` | Volume backup |
| Workspace files | `nymeria_workspace` | Volume backup |
| Redis cache/pub-sub state | `redis_data` | Optional volume backup; not a conversation-history store |
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

The repo ships no secrets. Start from the tracked `Nymeria/.env.docker.example`
template, copy it to `.env.docker`, and fill in real values on each machine:

```bash
cd Nymeria
cp .env.docker.example .env.docker
# Edit .env.docker with your API keys, passwords, and service tokens
```

`.env.docker` is gitignored and should never be committed. Compose reads it with
`--env-file` and injects only the variables each service declares; the full env
file is not bind-mounted into containers.

- **Per-machine values:** Each machine has its own `.env.docker` (different API keys, proxy URLs, passwords, etc.). Keep it local; do not share or commit it.
- **Rotation:** When a credential is compromised or due for rotation, update the value in `.env.docker` and restart the affected services. This covers LLM API keys, CLIProxy OAuth, Postgres, Redis, service tokens, Fernet keys, and Firebase/Google credentials.
- **Docker images:** `.env.docker` is excluded from the Docker build context. Compose injects selected values with `--env-file` instead of copying or mounting the full secret file into containers.
- **CLIProxy OAuth tokens** are per-machine and gitignored at `CLIProxyAPI-main/temp/latest/auths/`. Never copy them between machines.

## Reverse Proxy (Caddy)

The compose stack ships with a Caddy reverse proxy that terminates TLS and
fronts the api/mcp containers. Production hosts should expose **only** 80
and 443 to the public internet; the api (8000) and mcp (8001) ports are
bound to `127.0.0.1` inside the host and only reachable via SSH tunnel.

### One-time setup

1. Point a DNS A record at the host, for example `nymeria.example.com` to the host IP.
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
4. `docker compose up -d caddy`. Caddy joins the `edge` network and is
   the only host-published service.

If you don't have a domain yet, leave `NYMERIA_HOSTNAME` unset. Caddy
listens on `:80` over plain HTTP and you can front it with Cloudflare
Tunnel, Tailscale Funnel, or an SSH tunnel for testing; none of those
need a public TLS cert.

### Local-dev tunnel

When developing against the live host, SSH-tunnel the API port:

```
ssh -L 8000:127.0.0.1:8000 nymeria@<host>
```

The desktop and mobile apps can then point at `http://localhost:8000`
just as they do for local Docker.

## Security Considerations

1. **Account tokens**: Per-user bearer tokens (`nym_<token>`) are minted via `python3 run.py users add` for new users or `python3 run.py users issue-token` for an existing user. The legacy shared `NYMERIA_API_KEY` was retired; see `docs/accounts.md`. Worker, watchdog, bots, MCP, and foreground service processes authenticate with the admin `NYMERIA_SERVICE_TOKEN` plus `X-Nymeria-Act-As: <user_id>` for per-user routing.
2. **Secrets at rest**: `Nymeria/.env.docker`, `.env`, `firebase-service-account.json`, and `google_credentials.json` hold secrets and are gitignored. Keep them out of version control, restrict file permissions, and back them up separately from the repo. See the Secrets Management section above.
3. **CORS**: Restrict origins in production. `CORS_ORIGINS` should list your `NYMERIA_HOSTNAME` and the local Tauri origins for desktop/mobile clients; no wildcards.
4. **Trigger secrets**: Per-trigger shared secrets for webhook fire endpoints (see `docs/triggers.md`)
5. **Network**: TLS is terminated at the Caddy reverse proxy (above). Only 80/443 should be open on the host firewall; 8000/8001 are loopback-only.
6. **Container privileges**: App containers run as the Dockerfile's non-root `nymeria` user (uid 999) with `cap_drop: ALL` and `no-new-privileges:true`. Full-runtime containers (`api`, `worker`) keep a writable rootfs for runtime caches and workspace operations, but do not get `SETUID`, `SETGID`, `DAC_OVERRIDE`, or other package-install capabilities. Thin-client containers (watchdog, mcp, chat bots, caddy) also use read-only rootfs plus tmpfs for `/tmp` and `/home/nymeria`.
7. **Network segmentation**: Two Docker networks: `edge` for Caddy, API, MCP, worker, watchdog, profiled chat bots, and voice services; `backend` for PostgreSQL, Redis, API, and worker. Chat bots and MCP cannot reach the database directly even if compromised.
8. **Resource limits**: Every service declares `mem_limit`, `cpus`, and `pids_limit` (see the `x-limits-*` anchors in `docker-compose.yml`). A runaway tool call cannot exhaust host memory or fork-bomb the kernel.
9. **Bind mounts**: `./nymeria` and `run.py` are mounted **read-only** into containers for live code sync. `.env.docker` is not mounted; services receive only the selected environment variables declared in Compose. `.env.docker` stays out of image layers and is ignored by the Docker build context.
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
python3 -m pip install --user playwright
python3 -m playwright install chromium
```

In Docker, Playwright and Chromium are installed in `nymeria-full` at build
time. If browser automation is broken inside the API container, rebuild the
full image rather than installing browsers interactively into the running
container.

### Self-modifications lost after restart

Ensure code/data mounts are intact in `docker-compose.yml`:
```yaml
volumes:
  - ./nymeria:/app/nymeria:ro
  - ./run.py:/app/run.py:ro
  - nymeria_data:/data
```

## Upgrading

```bash
git pull
docker compose --env-file .env.docker build
docker compose --env-file .env.docker up -d
```

Self-modifications and runtime data are preserved by the `nymeria_data` and `nymeria_workspace` volumes. Source bind mounts (`./nymeria`, `run.py`) are read-only inside containers; update host files through normal git workflows and rebuild/restart. Compose tags the shared app images as `nymeria-full:local` and `nymeria-slim:local`, so rebuilding updates the grouped services instead of creating separate duplicate images for each command.
