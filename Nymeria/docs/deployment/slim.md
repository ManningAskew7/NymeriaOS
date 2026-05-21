# Slim Single-Process Launcher

`python3 run.py slim` runs everything Nymeria needs for local/personal use
in a **single Python process**: the REST API, the in-process ticker, the
watchdog task, and the Streamable HTTP MCP endpoint, all backed by SQLite
files in `data/`. There is no Docker, no Redis, and no Postgres.

This is the right launcher for:

- Trying Nymeria on a laptop in under a minute
- Self-hosting for one person on a VPS without docker compose
- Regression testing the sub-turn prompt queue locally (the queue + thread
  locks are process-local, so the single-process shape gives you the real
  semantics without a multi-container deployment)

For multi-user or production deployments use the Docker stack instead. See
[README.md](README.md) for the side-by-side comparison.

## Quick start

```bash
cd Nymeria
python3 run.py slim
```

That binds to `127.0.0.1:8000`. Override with flags:

```bash
python3 run.py slim --host 0.0.0.0 --port 8001
```

On first boot:

- `data/accounts.db` is created.
- `data/BOOTSTRAP_TOKEN.txt` is written (mode `0600`). Paste this `nym_<token>`
  value into the desktop/mobile Setup Wizard once.
- `data/SLIM_SERVICE_TOKEN.txt` is written (mode `0600`). This is an
  *internal* admin token that the in-process MCP, watchdog, trigger-fire
  and command-service callers use to authenticate against the API in the
  same process. **Do not** paste this into the Setup Wizard. It is a
  service credential, not a user credential.

## URLs

| Service | URL |
|---|---|
| REST API | `http://127.0.0.1:8000` |
| Health  | `http://127.0.0.1:8000/health` |
| MCP (Streamable HTTP) | `http://127.0.0.1:8000/mcp` |

## What the launcher does for you

`python3 run.py slim` applies the following runtime env overrides **before**
settings are cached, so the running process always uses SQLite even if
your shell or `.env.docker` exports Docker-oriented values:

- `DATABASE_BACKEND=sqlite`
- `REDIS_ENABLED=false` (and `REDIS_URL` is unset)
- `API_HOST` / `API_PORT` set to the launcher flags
- `NYMERIA_API_URL` set to a loopback URL the same process can reach

Then it constructs the FastAPI app with `slim_mode=True`, which:

- Forces the in-process ticker on (no separate worker container).
- Bootstraps the `bot-service` admin user and reuses or mints
  `SLIM_SERVICE_TOKEN.txt`.
- Mounts the MCP ASGI app at `/mcp` (before the SPA fallback route).
- Registers the watchdog as a FastAPI startup task (and stops it cleanly
  on shutdown). The watchdog still respects `WATCHDOG_ENABLED=false`.

## Flags

| Flag | Default | Purpose |
|---|---|---|
| `--host`, `-H` | `127.0.0.1` | Bind host |
| `--port`, `-p` | `8000` | Bind port |
| `--data-dir` | unset | Runtime data directory (writes `NYMERIA_DATA_DIR`) |
| `--no-mcp` | off | Skip mounting `/mcp` (debug only) |
| `--no-watchdog` | off | Skip the in-process watchdog task (debug only) |

## Verifying the launch

```bash
curl http://127.0.0.1:8000/health
python3 run.py users list      # 'default' (admin), 'bot-service' (admin)
ls data/SLIM_SERVICE_TOKEN.txt # exists, mode 0600
ls data/BOOTSTRAP_TOKEN.txt    # only on first boot
```

A Streamable HTTP MCP client (e.g. Claude Code's `mcp` config) can connect
to `http://127.0.0.1:8000/mcp` using `SLIM_SERVICE_TOKEN.txt` as the bearer.

## Token files: a quick map

| File | Audience | Purpose | Persistence |
|---|---|---|---|
| `data/BOOTSTRAP_TOKEN.txt` | The human operator | First-run admin login for the Setup Wizard | Auto-deleted when first used |
| `data/SLIM_SERVICE_TOKEN.txt` | Slim's same-process callers (MCP/watchdog/triggers) | Internal admin service credential | Persists; reused on every boot |

## Docker users: what does NOT work

Setting `REDIS_ENABLED=false` in `.env.docker` does **not** collapse the
Docker stack to single-process semantics: `docker-compose.yml` hard-codes
Redis settings on the API container and runs a separate `worker` service.
For sub-turn-queue testing, use `python3 run.py slim` instead, or write a
temporary compose override that scales `worker` to zero and overrides API
env to `DATABASE_BACKEND=sqlite` + `REDIS_ENABLED=false` with no
`REDIS_URL`. That override is a test harness, not a Docker-stack fix.

## Known limitations

- This launcher is the right shape for one user, give or take. Heavy
  concurrent writes start queueing past ~10 active users.
- Cross-process work (e.g. the Docker stack's pending-prompt queue and
  thread locks) is **not** addressed by this launcher; it is a separate
  workstream tracked in the slim-single-process audit doc.
