# Setup Wizard Knowledge (GUI / CLI Onboarding)

Status: design input, not an implemented spec. This doc captures everything
learned from standing up a single-container slim backend by hand (with CLIProxy
LLM routing and Cloudflare external access) so an onboarding wizard can automate
it. Read it as "here is the manual flow that works, here is what already exists,
here is the gap a wizard must close." It is written for both a CLI wizard
(`run.py init`) and a future GUI onboarding flow in the desktop app.

If you only read one section, read "The two axes" and "Gap analysis". They are
the conceptual core. The rest is recipe detail.

## The two axes (the conceptual core)

There are two independent choices, and the word "slim" appears on both, which is
the single biggest source of confusion. Keep them separate.

Axis 1, Runtime shape (process topology):
- Slim: one process running `run.py slim` (API plus in-process ticker plus
  watchdog plus embedded MCP) on SQLite. No Postgres, no Redis, no separate
  worker.
- Full: the Docker stack from `docker-compose.yml`. Separate containers for api,
  worker, mcp, watchdog, bots, plus Postgres and Redis.

Axis 2, Image toolset (which binaries are baked into the container):
- `Dockerfile.full`: Kali base, ~2 to 3 GB. All security tools, Playwright and
  Chromium, Node, Claude Code CLI. Used by the api and worker containers (the
  ones that run the agent).
- `Dockerfile.slim`: python:3.11-slim base, small. Thin-client image. Default
  command is `run.py watchdog`. Used by watchdog, bots, and mcp containers,
  which call the API over HTTP and never run an agent. It deliberately omits
  Kali tools, Playwright, Node, and the Claude CLI.
- `Dockerfile.single` (added during this work): python:3.11-slim base, ~976 MB.
  Hosts `run.py slim`. This is the containerized single-process launcher.

The trap: the name `Dockerfile.slim` reads like it pairs with `run.py slim`. It
does not. `Dockerfile.slim` is a thin client that runs no agent. `run.py slim`
runs the full NymeriaAgent in-process, so its host image must contain whatever
tools the agent will actually call. Compose builds `Dockerfile.slim` as the
image tag `nymeria-slim:local`, so do not repurpose that file or that tag for
the single-process launcher. Use a distinct name (`Dockerfile.single`).

Why this matters for the wizard: tool availability is a property of the image
(Axis 2), not the runtime shape (Axis 1). The roughly 1250 optional tools are
in-process Python functions and run on any base. Only a small subset shell out
to system binaries:
- Browser automation tools: need Chromium and Playwright.
- Kali security tools: need the Kali packages.
- The Claude Code CLI tool: needs Node and the CLI installed.
- Audio extraction (voice): needs ffmpeg.
- MCP servers spawned via `npx` or `uvx`: need Node or uv.

So a plain Debian image gives almost the entire tool surface. The wizard should
present base-image choice as "do you need browser, security, or CLI tools" and
not conflate it with the slim-versus-full process question.

## What already exists (build on this, do not rebuild)

`run.py init` invokes `nymeria/setup_wizard.py::run_init`, a mature, roughly
2300-line interactive wizard. Its data model lives in `nymeria/onboarding.py`.
Today it models two axes:

Hosting (`HostingOption` in onboarding.py): `bare_metal`, `venv`, `docker`.
These describe where the Python process runs.
- `bare_metal` and `venv` write `config.env` and expect native Python. This is
  effectively the slim or local install path.
- `docker` is a handoff only: `_print_docker_hosting_handoff` prints
  instructions. It does not build images, write `.env.docker`, mint secrets, or
  bring the stack up.

Auth method (`ProviderAuthMethod`): `api_key`, `cliproxy_claude_oauth`,
`cliproxy_codex_oauth`. The CLIProxy paths are already heavily automated:
- `_ensure_cliproxy_container_ready`: docker compose up if not reachable.
- `_ensure_cliproxy_claude_auth_files`: runs `--claude-login` if no active auth.
- `_ensure_tool_prefix_disabled`: sets `tool_prefix_disabled` in the auth JSON.
- `_resolve_cliproxy_gatekeeper_key`: generates `cpx-nymeria-<token>` keys and
  writes them into CLIProxy `config.yaml` `api-keys`.
- `_run_cliproxy_cloak_check`: runs `tools/check_cliproxy_cloak.py`.
- `_compose_host_cliproxy_base_url`: parses the CLIProxy compose `ports` to find
  the published host port, so it can detect 8318 rather than assuming 8317.

Other building blocks already present:
- Setup style (`recommended` vs `advanced`) and next action (start, print
  commands, cli).
- `AccountsRepo.ensure_bootstrap_admin` plus `_print_bootstrap_token_handoff`.
- `_test_llm_connection` for validating a provider before writing config.
- A clipboard-aware bootstrap token copy command for win32 and posix.

Constants that matter (setup_wizard.py):
- `CLIPROXY_CONTAINER_NAME = "cli-proxy-api-latest"`.
- `CLIPROXY_RELATIVE_ROOT = CLIProxyAPI-main/temp/latest`.
- `CLIPROXY_DEFAULT_HOST_BASE_URL = http://localhost:8317`.
- `CLIPROXY_DOCKER_BASE_URL = http://cli-proxy-api:8317`.

## Gap analysis (what the wizard does not yet do)

1. No single-container slim option. None of the hosting choices generate or run
   the `Dockerfile.single` plus `docker-compose.single.yml` plus `.env.docker`
   flow. This shape is especially valuable on Windows, where native install is
   painful (see "Host reality" below).
2. The full Docker path is a handoff, not an installer. There is no automated
   `.env.docker` generation from `.env.docker.example`, no secret minting
   (`POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `NYMERIA_SERVICE_TOKEN`), no
   `compose up`, no health wait, no bootstrap-token surfacing.
3. Headless or remote OAuth is not handled. The CLIProxy `--claude-login` flow
   assumes a local browser can reach the callback. On an SSH or headless host
   the callback never lands. There is a working manual workaround (see "CLIProxy
   OAuth, including the headless trick"). The wizard should automate it.
4. Default host base URL drift. `CLIPROXY_DEFAULT_HOST_BASE_URL` is 8317, but the
   current pinned CLIProxy publishes 8318 on the host. The compose-parsing
   helper handles this when a compose file exists, but the default fallback is
   stale.
5. Command name drift. This checkout exposes `users rotate-token`, not
   `users issue-token` (which appears in older VPS notes). The wizard should not
   hardcode `issue-token`.

## Host reality that shaped these decisions (Windows work instance)

- Python: `python` and `python3` on PATH are the Microsoft Store stubs. The real
  interpreter is the `py` launcher (Python 3.13). Crucially, no project
  dependencies are installed natively and there is no venv, so native
  `run.py slim` would require building the full dependency tree on Windows,
  including `sqlite-vec`. Containerizing sidesteps all of that. The wizard should
  treat "containerized slim" as the recommended default on Windows.
- Port 8000 was free (the main Nymeria stack was not running). Other local
  containers used 3000, 5000, 5433, 5678, 5679. The wizard should check 8000 (it
  already warns via `_port_in_use`).
- cloudflared runs as a Windows service using a token-managed tunnel. The public
  hostname to origin mapping lives in the Cloudflare Zero Trust dashboard, not in
  a local config file, so the wizard cannot read or write ingress locally. It can
  only test the public URL.

## Recipe A: single-container slim (the shape we built)

Three files, all under `Nymeria/`.

### Dockerfile.single

Key points the wizard generator must preserve:
- Base `python:3.11-slim-bookworm` (a real Debian distro, not Kali).
- apt installs ca-certificates, curl, git, plus gcc, g++, make as a build
  toolchain that is purged after pip install to keep the image lean.
- Non-root user pinned to uid and gid 999 so the `/data` volume ownership lines
  up with the other Nymeria images.
- `pip install -r requirements.txt`. This is sufficient because `requirements.txt`
  already includes `requirements-sqlite.txt` (langgraph-checkpoint-sqlite,
  aiosqlite, sqlite-vec). Do not install `requirements-docker.txt`: its Redis,
  Postgres, and Firebase deps are unnecessary because slim forces SQLite and
  disables Redis.
- `ENV NYMERIA_DATA_DIR=/data`, `NYMERIA_WORKSPACE_DIR=/workspace`.
- `USER nymeria`, `EXPOSE 8000`.
- `CMD ["python", "run.py", "slim", "--host", "0.0.0.0", "--port", "8000"]`.

### docker-compose.single.yml

- One service `nymeria-single`, image `nymeria-single:local`, built from
  `Dockerfile.single`.
- `env_file: .env.docker`. `run.py` loads it itself (see "How run.py slim
  behaves").
- `extra_hosts: ["host.docker.internal:host-gateway"]` so the in-process agent
  can reach CLIProxy and other host services from inside the container.
- `ports: ["127.0.0.1:8000:8000"]`. Loopback only. The Cloudflare tunnel runs on
  the host and reaches localhost:8000, so nothing is exposed to the LAN.
- Volumes: a dedicated `nymeria_single_data` volume at `/data` (kept separate
  from the main stack's `nymeria_data` so the test never touches production
  state), a workspace volume, and read-only bind mounts of `./nymeria` and
  `./run.py` for live code sync. An optional commented bind mount of
  `../nymeria-desktop/build` to `/app/frontend` for the web UI (not required;
  see "Frontend").
- `healthcheck`: `curl -f http://localhost:8000/health`.
- `init: true` to reap MCP subprocess grandchildren.

### .env.docker

- `run.py` loads `.env`, then `config.env`, then `.env.docker` via
  `load_dotenv(override=True)`. So a generated `.env.docker` is read directly by
  the slim launcher. Postgres and Redis values can be omitted because slim
  overrides them.
- For CLIProxy LLM routing, the values used were:
  `LLM_PROVIDER=anthropic`, `LLM_MODEL=claude-opus-4-6`,
  `LLM_BASE_URL=http://host.docker.internal:8318` (no `/v1` suffix for the
  Anthropic-style CLIProxy path; the OpenAI/Codex path needs `/v1`),
  `ANTHROPIC_API_KEY=<the cpx- gatekeeper key>`, plus optional tuning
  (`LLM_TEMPERATURE`, `LLM_EXTENDED_THINKING`, `LLM_REASONING_EFFORT`,
  `LLM_USE_MODEL_DEFAULTS`, `OPENAI_API_MODE=responses`).
- `NYMERIA_PUBLIC_URL=https://nymeria.example.com` for bot credential
  links and OAuth redirects.
- `CORS_ORIGINS` must include the public origin in addition to the local desktop
  and tauri origins, or the browser UI fails CORS when loaded through the tunnel.
- `NYMERIA_API_DOCS=true` is handy for a local test (enables Swagger).

### Build, run, verify

```
cd Nymeria
docker compose -f docker-compose.single.yml build
docker compose -f docker-compose.single.yml up -d
```

Healthy startup logs to look for (these confirm the slim shape):
- `Ticker started with 5s poll interval` (in-process ticker).
- `create_checkpointer backend=sqlite` and `SqliteSaver` (SQLite, no Postgres).
- `Slim MCP session manager started` (MCP embedded in-process).
- `Slim watchdog started (in-process task)`.
- `issued new slim-service token ... /data/SLIM_SERVICE_TOKEN.txt`.
- `GET /health 200 OK`.
- `Serving frontend from /app/nymeria/frontend`.

Verification commands:
- `curl http://127.0.0.1:8000/health` returns `{"status":"ok","version":"..."}`.
- The `/data` volume contains accounts.db, nymeria.db, todo_schedule.db,
  BOOTSTRAP_TOKEN.txt, SLIM_SERVICE_TOKEN.txt, and the subdirectories custom_tools,
  skills, todos, triggers, thread_configs, thread_metadata, users, logs, goals,
  mcp_servers.

## How run.py slim behaves (so the wizard can rely on it)

`_apply_slim_runtime_env` (run.py) sets, before settings are cached:
- `DATABASE_BACKEND=sqlite`
- `REDIS_ENABLED=false` and pops `REDIS_URL`
- `API_HOST`, `API_PORT` from the flags
- `NYMERIA_API_URL` to a loopback URL
- `NYMERIA_DATA_DIR` if `--data-dir` was given

Then it starts the API with `slim_mode=True`, which forces the in-process ticker
on, bootstraps the `bot-service` admin user, mints or reuses
`SLIM_SERVICE_TOKEN.txt`, mounts MCP at `/mcp` before the SPA fallback, and
registers the watchdog as a startup task (still respects
`WATCHDOG_ENABLED=false`).

Consequence: a generated `.env.docker` can safely carry Docker-oriented values;
slim ignores the Postgres and Redis ones. The wizard does not need to strip them.

## Frontend

The slim image serves a baked-in web UI from `/app/nymeria/frontend` (the package
ships a built frontend). `_frontend_static_dir` in `nymeria/triggers/api.py`
checks `<package>/frontend/index.html` first, then `/app/frontend`. So the
browser UI works at the API origin with no bind mount. If you want to serve a
freshly built desktop bundle instead, bind `../nymeria-desktop/build` to
`/app/frontend`. For a backend-only test, neither is required.

## Recipe B: CLIProxy LLM routing

CLIProxy is an optional sidecar that lets Nymeria use a Claude Max or Codex
subscription via OAuth instead of a metered API key. It is the "advanced" auth
path. On a fresh checkout it is unconfigured.

### Layout

The pinned deployment lives at `CLIProxyAPI-main/temp/latest/`. The root
`CLIProxyAPI-main/docker-compose.yml` is decommissioned (commented out); do not
use it. `temp/latest/` ships only `.example` files on a clean checkout
(`config.yaml.example`, `docker-compose.yml.example`).

### config.yaml

Generated from the example:
- `host: "0.0.0.0"`, `port: 8317`, `auth-dir: "/root/.cli-proxy-api"`.
- `api-keys`: a list of opaque `cpx-...` gatekeeper keys. These are local
  secrets that gate client access to the proxy. They are NOT upstream provider
  keys. Generate fresh per machine (the wizard already does
  `cpx-nymeria-<token_urlsafe(24)>`). The convention is one key for the
  Anthropic/Claude path (used as Nymeria `ANTHROPIC_API_KEY`) and one for the
  Codex/OpenAI path (used as Nymeria `OPENAI_API_KEY`).
- `debug: true`, `commercial-mode: false`, `request-retry: 1`.
- Do NOT set `claude-header-defaults`. On the pinned v6.9.36 image the cloak gate
  keys off the client's incoming User-Agent, not this config value.

### docker-compose.yml for the proxy

- Container `cli-proxy-api-latest`, image pinned by digest
  `eceasy/cli-proxy-api:v6.9.36@sha256:e0646c8f...`. Digest-pin matters because
  the cloak behavior changed between v6.9.0 and v6.9.36.
- `ports: ["8318:8317"]`. Host 8318, container 8317. Note 8318, not 8317, is the
  host port. Windows HyperV reserves a port range that collided with the proxy's
  OAuth ports historically, which is why the published ports are narrowed.
- Mounts: `./config.yaml` to `/CLIProxyAPI/config.yaml`, `./auths` to
  `/root/.cli-proxy-api`, `./logs` to `/CLIProxyAPI/logs`.
- The committed example joins an external network `nymeria_nymeria-network` so
  the full stack can reach the proxy at `http://cli-proxy-api:8317`. For a
  standalone slim test that network does not exist, so drop it and let the slim
  container reach the proxy via `http://host.docker.internal:8318` instead. The
  wizard should pick the addressing based on shape:
  - Full stack: put the proxy on the shared network, use `cli-proxy-api:8317`.
  - Standalone slim: use `host.docker.internal:8318`.

### CLIProxy OAuth, including the headless trick

The critical constraint: OAuth refresh tokens are single-use across machines.
Each host must run its own login. Do not copy `auths/*.json` between servers.

The login command is interactive and needs a TTY:
```
docker exec -it cli-proxy-api-latest ./CLIProxyAPI --claude-login --no-browser
```
It prints an authorization URL. The user opens it in a browser, signs in with
the Claude account, approves, and is redirected to
`http://localhost:54545/callback?code=...&state=...`.

On a host with a local browser, the redirect lands on the listener the login
process opens on port 54545 inside the container, and login completes.

On a headless or SSH host (no local browser), the user opens the URL on another
device, so the redirect to `http://localhost:54545` has nowhere to land. Working
workaround discovered here:
- The login process binds 54545 inside the container. The container has `/bin/sh`
  and `wget`.
- Deliver the captured callback to that listener from inside the container's own
  network namespace:
```
docker exec cli-proxy-api-latest wget -qO- \
  "http://localhost:54545/callback?code=THE_CODE&state=THE_STATE"
```
- A successful delivery returns the proxy's "Authentication Successful" HTML and
  the login process writes `auths/claude-<email>.json`.

The wizard should detect headless (no browser, or an SSH session) and offer to
accept the pasted callback URL, then deliver it via the in-container client. The
listener port (54545) and the in-container delivery pattern are the reusable
pieces.

### After login

1. Set `"tool_prefix_disabled": true` in the new `claude-<email>.json` auth file.
   This is required on v6.9.36. Do it without printing the token: load the JSON,
   set the key, write it back. The other keys present are access_token, email,
   expired, id_token, last_refresh, refresh_token, type.
2. Restart the proxy: `docker restart cli-proxy-api-latest`.
3. Verify the model list with the Claude gatekeeper key:
```
curl -H "Authorization: Bearer <cpx-claude-key>" http://localhost:8318/v1/models
```
   A healthy result lists multiple Claude models (observed 11, including
   claude-opus-4-6 and claude-opus-4-7). An empty list means the OAuth token is
   not active yet.
4. Recreate the slim container so it picks up the LLM env
   (`docker compose -f docker-compose.single.yml up -d`), then confirm its logs
   no longer show "Failed to fetch Anthropic models ... Connection refused".

### The User-Agent cloak

Nymeria's `providers.py` sends `User-Agent: claude-cli/2.1.113` from the
LangChain Anthropic client, which is what makes v6.9.36 skip cloaking. This is
already in the code the container runs, so no extra setup is needed. The wizard
should not try to re-add `claude-header-defaults` to the proxy config.

## Recipe C: external access (Cloudflare tunnel)

- cloudflared runs as a host service with a token-managed named tunnel. The
  public hostname to origin mapping lives in the Cloudflare Zero Trust dashboard.
  The wizard cannot read or write it locally; it can only test the public URL.
- Because the slim container publishes 127.0.0.1:8000 and the tunnel origin was
  already `http://localhost:8000` (from the prior Docker stack), external access
  worked with zero changes. `GET https://nymeria.example.com/health`
  returned 200 and the web UI root returned the Nymeria SPA.
- Set `NYMERIA_PUBLIC_URL` to the public origin and include it in `CORS_ORIGINS`.
- Other remote-access options (Tailscale, quick tunnel, Caddy) are documented in
  `docs/deployment/remote-access.md`. The wizard can offer these as alternatives
  but should treat "you already have a tunnel, just confirm ingress to
  localhost:8000" as the simplest path.

## Accounts and tokens

- Bootstrap token: written to `/data/BOOTSTRAP_TOKEN.txt` on first boot, intended
  for the desktop or mobile Setup Wizard, single-use, delete after use. Format
  `nym_...`.
- Slim service token: `/data/SLIM_SERVICE_TOKEN.txt`, persistent internal admin
  credential for in-process callers. Not for the Setup Wizard.
- Device tokens: minted with `python run.py users rotate-token <email> --label
  <name>`. Important details learned:
  - The subcommand is `rotate-token`, not `issue-token` (older VPS notes used
    `issue-token`). The wizard should detect the available subcommand or pin to
    `rotate-token`.
  - It resolves the user by email (`owner@localhost`), not by id (`default`).
  - It revokes prior tokens for that user and issues exactly one new token, shown
    once.
  - Available `users` actions in this build: add, list, disable, enable,
    rotate-token, link-platform, unlink-platform, platforms.
- A `NYMERIA_SERVICE_TOKEN not set` warning during `users` commands is expected
  and harmless in slim; in-process callers use the auto-minted slim service
  token.

## End-to-end smoke test

The truest check is a real chat turn through the API. `POST /chat` is SSE
streaming. Request body needs `message` (and optional `thread_id`). Auth is a
Bearer account token (the slim service token or a device token works).

```
curl -sN -X POST http://127.0.0.1:8000/chat \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  --data '{"message":"Reply with exactly the text: SLIM OK","thread_id":"smoketest"}'
```

A healthy stream ends with a `response` event carrying the text and a `done`
event carrying `context_stats` (model, token counts, context limit). Observed:
the model replied `SLIM OK`, model `claude-opus-4-6`, context limit 1000000.

## Proposed wizard flow (CLI and GUI)

Extend the existing wizard rather than writing a new one. The two existing axes
(hosting, auth) plus setup style and next action already form the skeleton.

Suggested steps:
1. Detect environment: OS, whether Docker is available and running, whether a
   local browser exists (headless detection), whether port 8000 is free, whether
   native Python dependencies are installed. On Windows with no native deps,
   recommend containerized slim.
2. Choose deployment target. Replace or augment the current hosting choices with:
   - Containerized slim (single container, recommended for one user).
   - Full Docker stack (multi-container, recommended for multi-user or uptime).
   - Native venv or bare metal (existing).
3. Choose image capability tier (only relevant for the container targets):
   - Minimal Debian (default): all in-process tools, no browser, security, or CLI
     tools.
   - Full Kali: adds browser, security tools, Node, Claude CLI. Heavier.
4. Choose LLM auth: direct API key (existing), or CLIProxy OAuth (Claude or
   Codex). For CLIProxy, run the automation, and on a headless host, prompt for
   the pasted callback URL and deliver it in-container.
5. Generate artifacts for the chosen target:
   - Containerized slim: `Dockerfile.single`, `docker-compose.single.yml`,
     `.env.docker`. Build and bring up.
   - Full Docker: generate `.env.docker` from the example, mint
     `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `NYMERIA_SERVICE_TOKEN`, then
     `docker compose up -d --build`.
6. Wait for health, then surface the bootstrap token (and optionally mint a
   device token via `users rotate-token`).
7. Offer external access guidance: detect cloudflared, test the public URL, set
   `NYMERIA_PUBLIC_URL` and `CORS_ORIGINS`.
8. Run a chat smoke test and report pass or fail.

GUI-specific notes:
- The desktop Setup Wizard already collects a backend URL and account token. The
  new GUI onboarding should be able to drive the same artifact generation through
  a backend setup endpoint or a local helper, then poll `/health` and accept the
  bootstrap token automatically.
- Headless OAuth is less of a concern in the GUI case (the desktop machine has a
  browser), but the callback-delivery helper is still useful when the backend
  runs on a remote host the desktop app is pointed at.

## Failure modes and gotchas (checklist for the wizard)

- Confusing `Dockerfile.slim` (thin client) with `run.py slim` (single process).
- Installing `requirements-docker.txt` into a slim image (unneeded Postgres,
  Redis, Firebase). `requirements.txt` already pulls SQLite plus sqlite-vec.
- Assuming the CLIProxy host port is 8317. It is 8318. Parse the compose ports.
- Copying CLIProxy `auths/*.json` between machines. Refresh tokens are single-use
  across hosts; each host runs its own login.
- Forgetting `tool_prefix_disabled: true` on the Claude auth file (v6.9.36).
- Headless OAuth: the callback to localhost:54545 cannot land without the
  in-container `wget` delivery trick.
- Missing the public origin in `CORS_ORIGINS`, which breaks the browser UI over
  the tunnel.
- Hardcoding `users issue-token`; this build uses `users rotate-token`, resolved
  by email.
- Native install on Windows: no project deps, no venv, and `sqlite-vec` to build.
  Prefer containerized slim.
- Port 8000 already in use (warn and offer an alternate port via the slim
  `--port` flag and the published port mapping).

## Reference paths

- `Nymeria/run.py` (`run_slim`, `_apply_slim_runtime_env`).
- `Nymeria/nymeria/setup_wizard.py`, `Nymeria/nymeria/onboarding.py`.
- `Nymeria/Dockerfile.single`, `Nymeria/docker-compose.single.yml`,
  `Nymeria/.env.docker` (generated artifacts from this work).
- `Nymeria/Dockerfile.slim`, `Nymeria/Dockerfile.full`,
  `Nymeria/docker-compose.yml` (existing images and full stack).
- `CLIProxyAPI-main/temp/latest/{config.yaml,docker-compose.yml}` (proxy
  artifacts), `Nymeria/tools/check_cliproxy_cloak.py` (cloak smoke test).
- `Nymeria/docs/deployment/slim.md`, `Nymeria/docs/deployment/shapes-explained.md`,
  `Nymeria/docs/deployment/remote-access.md`, `Nymeria/docs/cliproxy.md`,
  `Nymeria/docs/accounts.md`.
