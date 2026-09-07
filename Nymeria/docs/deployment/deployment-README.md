# Deployment

Nymeria supports two deployment shapes from the same codebase. Pick the one that matches your use case. You can switch later.

Both shapes come from the same two install channels: the published `nymeriaos` package (`uv tool install nymeriaos`) or a source checkout (`git clone` plus an editable install). The Docker stack needs the source checkout, because it builds its images locally; the beta publishes no container images. See [QUICKSTART.md](../getting-started/QUICKSTART.md) for the install steps.

New here? Read [shapes-explained.md](deployment-shapes-explained.md) first for a beginner-friendly tour of SSE, Redis, SQLite vs Postgres, and why two shapes exist. This page is the chooser; that one is the explainer.

## Decision tree

| Goal | Use |
|---|---|
| Want to try it on your laptop in five minutes | **Slim** |
| Self-host for personal use on a VPS or home server | **Slim** + [remote access](deployment-remote-access.md) |
| Run a small team or business deployment | **Docker stack** + reverse proxy |
| Need multi-machine scaling, network segmentation, or capability-dropped sandboxing | **Docker stack** |

## Shapes at a glance

### Slim
- One Python process
- SQLite for all data
- In-memory event bus
- Embedded MCP endpoint at `/mcp`; the watchdog sweep rides the in-process ticker
- No external services required (no Postgres, no Redis)
- Agent runs as the OS user that started it and has whatever filesystem access you do
- Install: `uv tool install nymeriaos && nymeria init && nymeria slim` (or, from a source checkout, `cd Nymeria && python3 -m pip install --user -r requirements.txt && python3 run.py slim`)

The slim shape is the **default** the codebase has always supported. It is how the project is developed. It is suitable for individuals and small teams (rule of thumb: comfortable up to ~10 active users; heavy concurrent writes start queueing past that).

See [slim.md](deployment-slim.md) for the full launcher reference, token-file map, and Docker-vs-slim caveats.

### Docker stack
- Multi-container: `api`, `worker`, `mcp`, `postgres`, `redis`, `caddy`, plus optional chat bots and voice services
- Postgres for durable app data and Redis pub/sub for cross-process events
- Caddy reverse proxy with automatic Let's Encrypt TLS
- Hardened: non-root/capability-dropped app containers, minimal thin-client env, network segmentation, read-only source bind mounts, resource limits
- Install: from a source checkout, `docker compose --env-file .env.docker up -d --build` (the compose file builds the images; there are no images to pull)

The Docker stack is what you reach for in production. It has stronger isolation, can host more concurrent users, and survives container failures cleanly. See [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md) for the operator's guide.

## What's the same in both shapes

Everything user-facing:
- All agent features: tools, skills, dynamic tool binding, scheduled TODOs, thread branches, memory index, credential vault
- All LLM provider integrations
- All chat-app bot integrations: native protocols through thin-client bot processes, plus API-hosted webhook runtimes for WhatsApp and Microsoft Teams
- The web UI, the desktop app, the mobile app
- Multi-user accounts
- Multi-agent thread teams

The slim shape is not a stripped-down product. It runs the same code in a smaller envelope.

## What differs

| | Slim | Docker stack |
|---|---|---|
| Storage backend | SQLite | Postgres |
| Event bus | In-memory | Redis pub/sub |
| Concurrency ceiling | ~10 active users | 100+ active users |
| Process isolation | One process | Per-service containers |
| Agent runtime | In-process (the one process) | API container only; `worker` schedules and relays turns to the API |
| Network segmentation | None | `edge` + `backend` networks |
| TLS / public URL | Bring your own (see [remote-access.md](deployment-remote-access.md)) | Bundled Caddy |
| Container security boundary | N/A (runs on host) | Cap-dropped, non-root, read-only rootfs where possible |
| Chat bots | Run as separate thin-client processes if launched | Profiled thin-client containers for Discord, Telegram, and Slack; API-hosted webhooks run inside `api` |

The Docker `worker` container is a scheduler + event relay: it polls
scheduled TODOs and poll-based triggers, then POSTs to `/chat` on the
API container with `publish_autonomous_events=False` so the worker
stays the sole publisher of autonomous SSE events (with stable
`todo.id` / `trigger-<id>` task ids). The API runs the agent. This
keeps the in-memory `ThreadLockManager` and `PendingPromptQueue`
authoritative for cross-source contention without any distributed
locking. See [architecture.md](../getting-started/architecture.md) §4 and
`nymeria/core/turn_executor.py` for the abstraction.

## Remote access

Both shapes can be accessed from anywhere: your laptop, your phone, or a coworker's machine. The patterns are the same regardless of shape. See [remote-access.md](deployment-remote-access.md).

Short version:
- **Chat bots** (Telegram/Discord/etc.): talk to your assistant from anywhere with zero network setup
- **Tailscale**: private network between your devices, no public exposure
- **Cloudflare Tunnel**: public URL on a free tier, no VPS or domain needed
- **Domain + Caddy**: classic production setup with your own domain
