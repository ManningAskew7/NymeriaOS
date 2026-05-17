# Deployment

NymeriaOS supports two deployment shapes from the same codebase. Pick the one that matches your use case — you can switch later.

## Decision tree

| If you... | Use |
|---|---|
| Want to try it on your laptop in five minutes | **Slim** |
| Self-host for personal use on a VPS or home server | **Slim** + [remote access](remote-access.md) |
| Run a small team or business deployment | **Docker stack** + reverse proxy |
| Need multi-machine scaling, network segmentation, or capability-dropped sandboxing | **Docker stack** |

## Shapes at a glance

### Slim
- One Python process
- SQLite for all data
- In-memory event bus
- No external services required (no Postgres, no Redis)
- Agent runs as the OS user that started it — has whatever filesystem access you do
- Install: `git clone … && uv venv && uv pip install -e . && python run.py api`

The slim shape is the **default** the codebase has always supported — it's how the project is developed. It is suitable for individuals and small teams (rule of thumb: comfortable up to ~10 active users; heavy concurrent writes start queueing past that).

### Docker stack
- Multi-container: `api`, `worker`, `mcp`, `postgres`, `redis`, `caddy`, plus optional chat bots
- Postgres + Redis for durability and cross-process events
- Caddy reverse proxy with automatic Let's Encrypt TLS
- Hardened: non-root/capability-dropped app containers, minimal thin-client env, network segmentation, read-only source bind mounts, resource limits
- Install: `docker compose --env-file .env.docker up -d`

The Docker stack is what you reach for in production. It has stronger isolation, can host more concurrent users, and survives container failures cleanly. See [PRODUCTION_DEPLOYMENT.md](../PRODUCTION_DEPLOYMENT.md) for the operator's guide.

## What's the same in both shapes

Everything user-facing:
- All agent features — tools, skills, dynamic tool binding, scheduled TODOs, thread branches, memory index, credential vault
- All LLM provider integrations
- All chat-app bot integrations (Telegram, Discord, Slack, Matrix, Signal, Mattermost, Zulip, Rocketchat)
- The web UI, the desktop app, the mobile app
- Multi-user accounts
- Multi-agent thread teams

The slim shape is not a stripped-down product. It runs the same code in a smaller envelope.

## What differs

| | Slim | Docker stack |
|---|---|---|
| Storage backend | SQLite | Postgres |
| Event bus | In-memory | Redis pub/sub (or Postgres LISTEN/NOTIFY) |
| Concurrency ceiling | ~10 active users | 100+ active users |
| Process isolation | One process | Per-service containers |
| Network segmentation | None | `edge` + `backend` networks |
| TLS / public URL | Bring your own (see [remote-access.md](remote-access.md)) | Bundled Caddy |
| Container security boundary | N/A (runs on host) | Cap-dropped, non-root, read-only rootfs where possible |
| Bot containers | Run as separate processes (systemd units) | Run as containers |

## Remote access

Both shapes can be accessed from anywhere — your laptop, your phone, a coworker's machine. The patterns are the same regardless of shape. See [remote-access.md](remote-access.md).

Short version:
- **Chat bots** (Telegram/Discord/etc.) — talk to your assistant from anywhere with zero network setup
- **Tailscale** — private network between your devices, no public exposure
- **Cloudflare Tunnel** — public URL on a free tier, no VPS or domain needed
- **Domain + Caddy** — classic production setup with your own domain
