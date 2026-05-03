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

# Optional: only needed when local development uses DATABASE_BACKEND=postgres
pip install -r requirements-postgres.txt

# Optional: Install BrowserAgent dependencies
pip install playwright
playwright install chromium

# Optional: Install Claude Code CLI
npm install -g @anthropic-ai/claude-code

# Configure
cp .env.example .env
# Edit .env with your API keys

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
| Browser automation | ⚠️ Install Playwright/Chromium | ✅ Included in image |
| `claude_code` integration | ⚠️ Install Claude Code CLI | ⚠️ Requires CLI availability in container |
| Self-modification persistence | ✅ (local filesystem) | ✅ (via mounted volumes) |

## Configuration

### Environment Variables

See `.env.docker.example` for all available options. Key settings:

```bash
# Required
POSTGRES_PASSWORD=<secure-password>
NYMERIA_SERVICE_TOKEN=<admin-service-token>   # see docs/accounts.md

# LLM (at least one required)
ANTHROPIC_API_KEY=sk-ant-...
OPENROUTER_API_KEY=sk-or-...
OPENAI_API_KEY=sk-...

# LLM Settings
LLM_PROVIDER=anthropic  # or openrouter, openai
LLM_MODEL=claude-sonnet-4-20250514

# Optional Integrations
PERPLEXITY_API_KEY=pplx-...      # Web search
TELEGRAM_BOT_TOKEN=...           # Telegram notifications
DISCORD_WEBHOOK_URL=...          # Discord notifications
SLACK_WEBHOOK_URL=...            # Slack notifications
```

### CORS for Remote Access

To access from other devices:

```bash
# Allow specific origins
CORS_ORIGINS=http://192.168.1.100:1420,http://myphone.local:1420

# Or allow all (less secure)
CORS_ORIGINS=*
```

### Messaging Integrations

Telegram, Discord, and Twitch bots run as dedicated thin-client containers.
Configure their tokens in `.env.docker` and enable their Docker Compose
profiles — no external webhook URLs needed. See `docs/telegram-bot.md`,
`docs/discord-bot.md`, and `docs/twitch-bot.md`.

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

## Security Considerations

1. **Account tokens**: Per-user bearer tokens (`nym_…`) are minted via `python run.py users add`. The legacy shared `NYMERIA_API_KEY` was retired — see `docs/accounts.md`. Bots/ticker/watchdog authenticate with the admin `NYMERIA_SERVICE_TOKEN` plus `X-Nymeria-Act-As: <user_id>` for per-user routing.
2. **CORS**: Restrict origins in production
3. **Trigger secrets**: Per-trigger shared secrets for webhook fire endpoints (see `docs/triggers.md`)
4. **Network**: Use HTTPS in production (reverse proxy)
5. **Docker**: Current image runs as root by design (Kali tooling). Restrict host/container access and deploy only in trusted environments.
6. **Bind mounts**: `./nymeria`, `run.py`, and `.env.docker` are mounted into containers for live sync, so treat host repo access as production-sensitive.
7. **Kali Tools**: Use responsibly and only on authorized targets

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

Self-modifications and runtime config are preserved by bind-mounted code/config files (`./nymeria`, `run.py`, `.env.docker`) plus the `nymeria_data` volume. Re-apply carefully after upstream upgrades if merge conflicts occur.
