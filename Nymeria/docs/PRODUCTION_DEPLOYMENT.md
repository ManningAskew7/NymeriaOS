# Nymeria Production Deployment Guide

This document outlines deployment options for Nymeria, from local development to full Docker-based production deployments.

## Deployment Options Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Nymeria Deployment Options                       │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  💻 LOCAL (Most Users)                                              │
│     python run.py api                                               │
│     - Full system access                                            │
│     - Install dependencies as needed                                │
│     - Best for personal use on your own machine                     │
│                                                                     │
│  📦 DOCKER STANDARD                                                 │
│     docker compose --profile standard up -d                         │
│     - Includes Chromium + Playwright (BrowserAgent)                 │
│     - Git, Node.js, dev tools                                       │
│     - ~800MB image                                                  │
│     - Good for servers, multi-device access                         │
│                                                                     │
│  🐉 DOCKER FULL (Kali Linux)                                        │
│     docker compose --profile full up -d                             │
│     - Everything in Standard, plus:                                 │
│     - Kali Linux base with security tools                          │
│     - Claude Code CLI pre-installed                                │
│     - Full development environment                                  │
│     - ~2-3GB image                                                  │
│     - For power users                                               │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

## Quick Start

### Local Development (Recommended for Most Users)

```bash
cd Nymeria
pip install -r requirements.txt

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

### Docker Standard (Server/Multi-device)

```bash
cd Nymeria
cp .env.docker.example .env.docker
# Edit .env.docker with your API keys and secrets

docker compose --profile standard up -d
```

### Docker Full (Kali Linux Workstation)

```bash
cd Nymeria
cp .env.docker.example .env.docker
# Edit .env.docker with your API keys and secrets

docker compose --profile full up -d
```

## Architecture

### Docker Deployment Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Docker Network                               │
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │  PostgreSQL  │  │    Redis     │  │   Nymeria Container   │  │
│  │              │  │              │  │                       │  │
│  │  Checkpoint  │  │  Event Bus   │  │  - API Server         │  │
│  │  Storage     │  │  (Pub/Sub)   │  │  - Worker (Ticker)    │  │
│  │              │  │              │  │  - All Tools          │  │
│  └──────────────┘  └──────────────┘  │  - Browser (Standard+)│  │
│                                      │  - Dev Tools (Full)   │  │
│                                      │  - Kali Tools (Full)  │  │
│                                      └───────────────────────┘  │
│                                                 │                │
│  Volumes:                                       │                │
│  - postgres_data: Conversation history          │                │
│  - redis_data: Event persistence                │                │
│  - nymeria_data: Memories, TODOs, profiles      │                │
│  - nymeria_workspace: Project files (Full)      │                │
│  - nymeria_code: Self-modifications (Full)      │                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
              Access from any device via API
              (Desktop app, mobile, other agents)
```

## Tool Availability by Deployment

| Tool | Local | Docker Standard | Docker Full |
|------|-------|-----------------|-------------|
| bash_execute | ✅ Host system | ✅ Container (Debian) | ✅ Container (Kali) |
| file_read/write/list | ✅ Full access | ✅ /data + /workspace | ✅ /data + /workspace |
| web_search | ✅ | ✅ | ✅ |
| memory tools | ✅ | ✅ | ✅ |
| todo tools | ✅ | ✅ | ✅ |
| self_modify | ✅ | ✅ (persisted) | ✅ (persisted) |
| BrowserAgent | ⚠️ Install Playwright | ✅ Pre-installed | ✅ Pre-installed |
| OutlookAgent | ✅ | ✅ | ✅ |
| claude_code | ⚠️ Install CLI | ❌ Not included | ✅ Pre-installed |
| notify tools | ✅ | ✅ | ✅ |
| Kali security tools | ❌ | ❌ | ✅ nmap, nikto, etc. |

## Configuration

### Environment Variables

See `.env.docker.example` for all available options. Key settings:

```bash
# Required
NYMERIA_API_KEY=<your-api-key>
POSTGRES_PASSWORD=<secure-password>

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

### Webhook Configuration

For receiving messages from Telegram/Discord/Slack:

```bash
WEBHOOK_SECRET=<generate-a-secret>
```

Then configure your platform to send webhooks to:
- Telegram: `https://your-domain/webhook/telegram`
- Discord: `https://your-domain/webhook/discord`
- Slack: `https://your-domain/webhook/slack`

## MCP Server (Agent-to-Agent Communication)

Nymeria can expose an MCP server for other AI agents to use her capabilities.

### Local MCP

```bash
# STDIO mode (for Claude Code, etc.)
python run.py mcp

# HTTP mode (for network access)
python run.py mcp --http --port 8001
```

### Docker MCP

The MCP server is exposed on port 8001 in Docker deployments:

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

The API server is stateless and can be scaled:

```bash
docker compose --profile standard up -d --scale api=3
```

Add a load balancer (nginx, traefik) in front.

### Single Worker

The worker (ticker) should only run ONE instance to avoid duplicate task execution.

## Backup & Recovery

### Data Locations

| Data | Location | Backup Method |
|------|----------|---------------|
| Conversations | PostgreSQL | `pg_dump` |
| Memories/Profiles | /data/users/ | Volume backup |
| TODOs | /data/todos/ | Volume backup |
| Custom Tools | /data/custom_tools/ | Volume backup |
| Self-modifications | /app/nymeria/ (Full) | Volume backup |

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

1. **API Key**: Always set a strong `NYMERIA_API_KEY`
2. **CORS**: Restrict origins in production
3. **Webhook Secret**: Validate incoming webhooks
4. **Network**: Use HTTPS in production (reverse proxy)
5. **Docker**: Run as non-root user (already configured)
6. **Kali Tools**: Use responsibly and only on authorized targets

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

Ensure the code volume is mounted:
```yaml
volumes:
  - nymeria_code:/app/nymeria
```

## Upgrading

```bash
git pull
docker compose --profile <your-profile> build
docker compose --profile <your-profile> up -d
```

Self-modifications are preserved in volumes and will need to be re-applied to new code if there are conflicts.
