# Migration Guide: Local to Docker Deployment

This guide walks you through migrating your Nymeria installation from local development to a Docker-based production deployment.

## Overview

The Docker deployment provides:
- **PostgreSQL** for production-grade conversation persistence
- **Redis** for cross-container event communication
- **Horizontal scaling** - run multiple API containers behind a load balancer
- **Separate worker** - isolated ticker for autonomous task execution
- **Platform integrations** - Telegram, Discord, Slack, Twitch, and MCP services

## Prerequisites

- Docker and Docker Compose installed
- Your existing environment file(s) with LLM API keys
- (Optional) Messaging platform credentials for integrations

## Migration Steps

### Step 1: Prepare Environment File

1. Copy the Docker environment template:
   ```bash
   cp .env.docker.example .env.docker
   ```

2. Fill in required values:
   ```bash
   # Generate API key if you don't have one
   python -c "import secrets; print(secrets.token_urlsafe(32))"

   # Generate PostgreSQL password
   python -c "import secrets; print(secrets.token_urlsafe(24))"
   ```

3. Edit `.env.docker` with your values:
   ```env
   NYMERIA_API_KEY=<your-generated-key>
   POSTGRES_PASSWORD=<your-generated-password>
   ANTHROPIC_API_KEY=<your-api-key>
   ```

### Step 2: Export Existing Data (Optional)

If you want to preserve your conversation history and user memories:

1. **Backup SQLite databases:**
   ```bash
   cp data/nymeria.db data/nymeria.db.backup
   cp data/todo_schedule.db data/todo_schedule.db.backup
   # Legacy only (if present from older versions):
   [ -f data/tasks.db ] && cp data/tasks.db data/tasks.db.backup
   ```

2. **Backup user data:**
   ```bash
   [ -d data/users ] && cp -r data/users data/users.backup
   [ -d data/todos ] && cp -r data/todos data/todos.backup
   [ -d data/custom_tools ] && cp -r data/custom_tools data/custom_tools.backup
   ```

Note: SQLite to PostgreSQL migration requires a separate script. The Docker deployment starts with a fresh database by default.

### Step 3: Build and Start Services

1. **Build the Docker images:**
   ```bash
   docker compose build
   ```

2. **Start all services:**
   ```bash
   docker compose --env-file .env.docker up -d
   ```

3. **Check service health:**
   ```bash
   docker compose ps
   ```

   All services should show "healthy" status after a few seconds.

### Step 4: Verify Deployment

1. **Test the health endpoint:**
   ```bash
   curl http://localhost:8000/health
   ```

   Expected response: `{"status":"ok","version":"1.0.0"}`

2. **Test the chat endpoint:**
   ```bash
   curl -X POST http://localhost:8000/chat/sync \
     -H "Authorization: Bearer <your-api-key>" \
     -H "Content-Type: application/json" \
     -d '{"message": "Hello!", "user_id": "default"}'
   ```

3. **Check logs for any errors:**
   ```bash
   docker compose logs -f api
   docker compose logs -f worker
   ```

### Step 5: Configure Frontend

Update your desktop app to point to the Docker API:

1. In the app settings, change the API URL from `http://localhost:8000` to your Docker host's address
2. If accessing from a different machine, use the server's IP or hostname

For remote access, ensure:
- Port 8000 is accessible (firewall rules)
- CORS is configured in `.env.docker`: `CORS_ORIGINS=http://your-frontend-url`

### Step 6: Set Up Messaging Integrations (Optional)

#### Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather)
2. Get your chat ID from [@userinfobot](https://t.me/userinfobot)
3. Add to `.env.docker`:
   ```env
   TELEGRAM_BOT_TOKEN=your-bot-token
   TELEGRAM_DEFAULT_CHAT_ID=your-chat-id
   ```
4. Set up webhook (replace with your domain):
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://your-domain/webhook/telegram"
   ```

#### Discord

1. Create a webhook in your Discord server (Server Settings > Integrations)
2. Add to `.env.docker`:
   ```env
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
   ```

#### Slack

1. Create a Slack App and enable Incoming Webhooks
2. Add to `.env.docker`:
   ```env
   SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
   ```

### Step 7: Restart with New Configuration

After updating `.env.docker`:

```bash
docker compose --env-file .env.docker down
docker compose --env-file .env.docker up -d
```

The compose stack also bind-mounts `./nymeria`, `run.py`, and `.env.docker` into containers for live code/config sync, so host-side edits are immediately reflected after restart.

## Architecture Reference

```
┌─────────────────────────────────────────────────────┐
│  Docker Compose Network                             │
│                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐          │
│  │ API      │  │ Worker   │  │ Redis    │          │
│  │ :8000    │←→│ (Ticker) │←→│ :6379    │          │
│  └────┬─────┘  └────┬─────┘  └──────────┘          │
│       │             │                               │
│  ┌────┴─────────────┴───────────────────────┐      │
│  │ PostgreSQL :5432                          │      │
│  │ (conversations and checkpoints)           │      │
│  └───────────────────────────────────────────┘      │
│                                                     │
│  Volumes:                                           │
│  - postgres_data: Database files                    │
│  - redis_data: Event persistence                    │
│  - nymeria_data: User profiles, memories            │
└─────────────────────────────────────────────────────┘
```

## Common Operations

### View logs
```bash
docker compose logs -f          # All services
docker compose logs -f api      # API only
docker compose logs -f worker   # Worker only
```

### Restart services
```bash
docker compose restart api
docker compose restart worker
```

### Stop all services
```bash
docker compose down
```

### Update to latest code
```bash
git pull
docker compose build
docker compose --env-file .env.docker up -d
```

### Scale API containers (for load balancing)
```bash
docker compose --env-file .env.docker up -d --scale api=3
```
Note: You'll need a load balancer (nginx, traefik) in front of the API containers.

## Troubleshooting

### API container keeps restarting

Check logs:
```bash
docker compose logs api
```

Common issues:
- Missing environment variables (check `.env.docker`)
- PostgreSQL not ready (wait a few seconds, check postgres logs)
- Invalid API key format

### Worker not processing scheduled tasks

Check logs:
```bash
docker compose logs worker
```

Verify Redis connection:
```bash
docker compose exec redis redis-cli ping
```

Remember that only one worker instance should run, while API containers can scale horizontally.

### Database connection errors

Check PostgreSQL logs:
```bash
docker compose logs postgres
```

Verify connection:
```bash
docker compose exec postgres psql -U nymeria -c "SELECT 1"
```

### Webhooks not working

1. Check webhook health:
   ```bash
   curl http://localhost:8000/webhook/health
   ```

2. Verify webhook secret matches platform configuration

3. Check logs for incoming webhook requests:
   ```bash
   docker compose logs -f api | grep webhook
   ```

## Rollback

To revert to local development:

1. Stop Docker services:
   ```bash
   docker compose down
   ```

2. Restore your original `.env`:
   ```bash
   # If you backed it up
   cp .env.backup .env
   ```

3. Run locally:
   ```bash
   python run.py api
   ```

Your SQLite databases and user data in `data/` are preserved.
