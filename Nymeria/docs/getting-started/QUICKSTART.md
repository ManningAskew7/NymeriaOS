# NymeriaOS Quick Start Guide

Get NymeriaOS running in under 10 minutes.

## Prerequisites

- **Python 3.11+** - Check with `python3 --version`. If you install with `uv`
  (below), uv can fetch a matching Python for you, so you do not have to install
  one first.
- **An installer** - [`uv`](https://docs.astral.sh/uv/) (recommended) or `pipx`
  (`python3 -m pip install --user pipx`).
- **LLM API Key** - From one of:
  - [Anthropic](https://console.anthropic.com/) (recommended)
  - [OpenAI](https://platform.openai.com/)
  - [OpenRouter](https://openrouter.ai/)

## Step 1: Install NymeriaOS

### One-line installer (front door)

The installer lets you pick a track and runs the right commands for you:

```bash
curl -fsSL https://get.nymeriaos.com/install.sh | sh
```

It asks whether you want **Slim** (simpler, best for a few users; single process
on SQLite via `uv`, no Docker), **Full** (more robust, multi-user; runs in
Docker, and the script can install Docker for you on Linux), or **Source**
(hackable: a git checkout with an editable install, for working on Nymeria or
letting the agent modify its own source). Non-interactive use:
`... | sh -s -- --slim`, `--full`, or `--source`. Cautious users can download
and read it first
(`curl -fsSL https://get.nymeriaos.com/install.sh -o install.sh`).

Note: the one-line installer requires the hosted endpoint
(`get.nymeriaos.com`, plus the published container images for the Full track
and the public repo for the Source track) to be live. Until then, use the
manual commands below, which work today.

### Manual install

`uv` is the recommended installer (it is fast and can fetch a matching Python
for you); `pipx` also works.

With `uv` (recommended):

```bash
uv tool install nymeriaos
nymeria init
nymeria doctor
nymeria api
```

To try NymeriaOS without a persistent install, run it ephemerally with
`uvx --from nymeriaos nymeria slim`.

With `pipx`:

```bash
pipx install nymeriaos
nymeria init
nymeria doctor
nymeria api
```

From source (the hackable install; edits under the checkout, yours or the
agent's own, apply on the next restart, and git gives you diff/branch/revert
safety):

```bash
git clone https://github.com/ManningAskew7/NymeriaOS.git ~/NymeriaOS
uv tool install --editable ~/NymeriaOS/Nymeria
nymeria init
```

A source install also unlocks the Docker shapes that build from the checkout
(including the full Postgres + Redis stack); the wizard detects the checkout
automatically.

The default install is lean. Optional chat-platform bots and heavy integrations
install as extras, for example `uv tool install "nymeriaos[discord]"` (or
`pipx install "nymeriaos[discord]"`). Use `nymeriaos[bots]` for every chat platform,
or combine extras like `nymeriaos[postgres,redis,voice]`. Available extras:
`discord`, `telegram`, `slack`, `bots`, `postgres`, `redis`, `voice`,
`browser`, `firebase`, `all`.

`nymeria init` opens an interactive setup wizard. The first screen picks the
setup depth. Quickstart (the recommended default) asks only the essentials:
how to host the backend, your LLM provider and key (or subscription OAuth),
your timezone (detected, you just confirm it), and remote access. Everything
else gets free, keyless defaults you can change later in the app: local
semantic memory, keyless web search (the bundled SearXNG container on Docker,
in-process metasearch elsewhere), the built-in page fetcher, local voice on
bare-metal installs, and all six bundled skill kits. Full setup walks every step
instead (tool families, embeddings, image generation, voice, context tuning,
agent limits). A third option, finishing setup in the desktop app, is on the
way.

Hosting works the same in both tiers: run the backend directly, install a
background service, or run a single Docker container. The Docker option works
without a source checkout: the wizard writes the published-image compose file
and `.env.docker` into its config dir, pins the image tag to your installed
version, and can start the container for you (it needs the published images
to be live; the full Postgres + Redis stack still requires a source
checkout). Move with the arrow keys, Enter to advance, Esc to go back a step,
and Ctrl+Q to quit. A review screen confirms before anything is written.

After you confirm, NymeriaOS validates the provider key with a small LLM API
call (unless you pass `--skip-llm-test`), writes `~/.nymeria/config.env`, creates
`~/.nymeria/data/`, and mints the first bootstrap admin token. The token handoff
and a capability summary print to the terminal after the wizard closes. Optional
provider keys (embeddings, OpenAI tools, Gemini, Perplexity) can be supplied with
flags now and will get their own wizard steps later.

For unattended setup, `nymeria init --non-interactive` takes flags instead of
prompting (see "Scripted setup" below). `nymeria doctor` checks the installed
Python version, config files, data directory, LLM connectivity, local databases,
optional Redis/voice setup, bundled frontend, and API port before you start the
server.

Packaged installs store config and writable data under `~/.nymeria/` by
default. After `nymeria api` starts, open `http://localhost:8000`; the backend
serves the bundled web UI from the same origin.

### Docker From A Source Checkout

Use this path when you want the full local service stack:

```bash
cd nymeria-desktop
npm install
npm run build
cd ../Nymeria
cp .env.docker.example .env.docker
# Edit .env.docker and add your LLM provider/key plus REDIS_PASSWORD and POSTGRES_PASSWORD.
DISCORD_BOT_TOKEN=disabled docker compose --env-file .env.docker up -d --build
```

The API will be available at `http://localhost:8000`. The compose file mounts
`nymeria-desktop/build` into the API container, so rebuild the frontend after
frontend changes if you are using the backend-served web UI. On first boot,
read the bootstrap account token from the shared Docker data volume or from the
API logs. From the `Nymeria/` directory:

```bash
docker compose --env-file .env.docker exec api cat /data/BOOTSTRAP_TOKEN.txt
docker compose --env-file .env.docker logs api
```

Paste the token into the web UI setup wizard.

For the full source-checkout service stack above, use the compose flow directly
rather than `nymeria init`. Create `.env.docker` from the example and edit it in
the source checkout so provider credentials are written to the Docker runtime
config, not to a packaged `config.env`.

### Source-Checkout Development

Use this path when you are changing backend code locally:

```bash
cd Nymeria
python3 -m pip install --user -r requirements.txt
```

From a source checkout, you can also install the backend package in editable
mode. This uses `pyproject.toml` and keeps the `nymeria` package importable
while you work:

```bash
python3 -m pip install --user -e .
```

SQLite is the default backend and needs no extra packages. If you want local
development to use PostgreSQL instead, install the Postgres checkpoint extras
after the base dependencies:

```bash
python3 -m pip install --user -r requirements-postgres.txt
# or, when using the package metadata:
python3 -m pip install --user -e ".[postgres]"
```

## Step 2: Configure Environment

Packaged installs normally do this through `nymeria init`; source and Docker
launches usually use dotenv files.

For a full local/Docker config template:

```bash
cp .env.docker.example .env.docker
```

Or create `.env` manually for a lighter local setup. The runtime loads `.env`,
`config.env`, and `.env.docker` if present.

## Step 3: Set Your API Keys

Edit `.env`, `config.env`, or `.env.docker` and fill in the required values:

### Account token

The legacy shared `NYMERIA_API_KEY` was retired in the multi-user refactor. The
first time setup or API boot finds an empty accounts DB it auto-creates a
`default` admin user and writes the raw account token to
`<data_dir>/BOOTSTRAP_TOKEN.txt` (mode 0600). `nymeria init` prints the token
file path and a platform-specific copy command that reads the `nym_<token>` value
from the file without putting the token itself in shell history. Paste that
`nym_<token>` account token into the desktop/mobile Setup Wizard, not an
Anthropic/OpenAI/OpenRouter provider API key, then delete the file. See
`docs/accounts.md` for the full account model and the `python3 run.py users`
CLI for provisioning additional users.

### Set Your LLM Provider API Key

For Anthropic (default):
```ini
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-<key>
```

For OpenAI:
```ini
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-<key>
```

For OpenRouter:
```ini
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-<key>
```

Optional capability keys:

```ini
EMBEDDING_API_KEY=sk-<key>       # Semantic memory/RAG/skill search
OPENAI_API_KEY=sk-<key>          # OpenAI image generation/STT/OpenAI-backed tools
GEMINI_API_KEY=<key>             # Gemini image/document extraction/TTS tools
PERPLEXITY_API_KEY=pplx-<key>    # Web search
```

If `LLM_PROVIDER=openai`, the primary `OPENAI_API_KEY` also covers optional
OpenAI-backed features.

For scripted setup in CI or an offline support session, `nymeria init` accepts
`--non-interactive` plus `--provider`, `--model`, and `--api-key` (all three are
required for a fresh install), and `--root` / `--data-dir` to control where
config and data are written. Optional connection flags are `--base-url` and
`--api-mode responses|chat_completions` for OpenAI-compatible providers, plus
`--hosting local|service|docker` and `--port` (the API listen port, default
8000; the wizard asks the same question interactively and warns when the port
is already in use). `--next-action print_commands|cli|start_api_open_frontend`
selects the closing handoff. Optional capability keys can be supplied with
`--embedding-api-key`, `--openai-api-key`, `--gemini-api-key`, and
`--perplexity-api-key`. Add `--quick` to apply the Quickstart tier's keyless
defaults (free local RAG, keyless web search and fetch, local voice on bare
metal, all skill kits) to the scripted setup, for example:

```bash
nymeria init --non-interactive --quick --provider anthropic \
  --model claude-opus-4-8 --api-key sk-ant-... --start
```

Re-running `nymeria init --non-interactive` against an existing install
reconfigures it: the current values load from disk and only the flags you pass
change, so omitted flags (including `--api-key`) keep their current values.
Add `--skip-llm-test` to write config without the live checks (the provider
key test and the post-start chat smoke test), and `--force` to discard the
existing `config.env` and rebuild it from flags alone. Add `--run-doctor` for
the quick post-init doctor check, or `--full-doctor` when you also want doctor
to make its own live LLM check.

To diagnose an existing install without changing files, run:

```bash
nymeria doctor
```

If you are offline or intentionally testing without provider access, use
`nymeria doctor --skip-llm-test` to keep the rest of the checks useful.

## Step 4: Start the Backend

For solo/local use the simplest path is `python3 run.py slim`, which runs
the API, ticker (including the watchdog sweep), and MCP in a single process backed by SQLite;
no Docker, no Redis, no Postgres:

```bash
python3 run.py slim
```

You should see:
```
Starting NymeriaOS SLIM (single-process) on 127.0.0.1:8000
  - Mode: SQLite + in-process ticker + embedded MCP
  - Internal API URL: http://127.0.0.1:8000
  - Data directory: <data_dir>
  - MCP endpoint: http://127.0.0.1:8000/mcp
```

`python3 run.py slim` writes an internal `data/SLIM_SERVICE_TOKEN.txt`
(mode 0600) on first boot so embedded MCP and trigger fires
can authenticate against the API in the same process. That is NOT the
human bootstrap token; paste `data/BOOTSTRAP_TOKEN.txt` into the desktop
Setup Wizard, not `SLIM_SERVICE_TOKEN.txt`. See
[deployment-slim.md](deployment-slim.md) for the full launcher reference.

For source-checkout API-only development, use:

```bash
python3 run.py api
```

When installed as a package, use:

```bash
nymeria slim
# or
nymeria api
```

`nymeria api` prints:
```
Starting NymeriaOS API server on 0.0.0.0:8000
  - API docs: disabled (set NYMERIA_API_DOCS=true to enable)
```

For full multi-process Docker deployments with separate API, worker, MCP,
PostgreSQL, and Redis services, use the Docker Compose path above.

### Backend Validation

Install the development requirements when you want to run backend linting,
tests, or coverage. Docker production images intentionally omit `pytest`,
`pytest-cov`, `ruff`, and other dev-only packages:

```bash
python3 -m pip install --user -r requirements-dev.txt
# or, when using the package metadata:
python3 -m pip install --user -e ".[dev]"
python3 -m ruff check nymeria tests run.py
python3 -m pytest tests --cov=nymeria --cov=run --cov-report=term --cov-fail-under=38
```

## Step 5: Open the Web UI or Desktop App

For package installs and source checkouts with a bundled frontend, open:

```text
http://localhost:8000
```

The backend-served web UI auto-detects the current origin as the API URL after
`/health` succeeds.

For the Windows desktop app:

1. Start or choose a separately installed backend
2. Open the NymeriaOS desktop app
3. The setup wizard will guide you through:
   - Entering the backend URL (default: `http://localhost:8000`)
   - Pasting a `nym_<token>` account token, such as the bootstrap token from
     `<data_dir>/BOOTSTRAP_TOKEN.txt` on a first local backend boot
   - Testing the connection

After an admin token is connected, provider configuration is separate from the
client connection wizard. Open Settings > Provider > Open Wizard to test and
save a direct provider key or point the backend at an already-running CLIProxy
OAuth endpoint. These settings are deployment-wide and affect all users who
inherit the global provider. Installed desktop builds are client-only: they do
not start a backend, start CLIProxy, run OAuth login, or write backend config
files directly.

## You're Done!

Start chatting with NymeriaOS. Here are some things to try:

- "Remember that my name is [your name]"
- "Create a TODO to remind me to check email in 2 hours"
- "What can you help me with?"

---

## Troubleshooting

### "Invalid API key" / 401 from the desktop app

The legacy `NYMERIA_API_KEY` shared key was retired. Authentication now uses
per-user account tokens. Read `<data_dir>/BOOTSTRAP_TOKEN.txt` (written
automatically on the first API boot) for the bootstrap admin token. To create a
new user and first token:
`python3 run.py users add <email> --role user --id <slug>`. To issue another
token for an existing user without revoking old tokens:
`python3 run.py users issue-token <id-or-email> --label <device>`. Both flows
are documented in `docs/accounts.md`.

### "No API key for LLM provider"

Make sure you've set the API key for your chosen provider in your environment file:
- `ANTHROPIC_API_KEY` for Anthropic
- `OPENAI_API_KEY` for OpenAI
- `OPENROUTER_API_KEY` for OpenRouter

### "Cannot connect to server"

1. Check the backend is running: `python3 run.py api`
2. Check the URL in the desktop app matches the backend
3. Check firewall isn't blocking the API port (8000 by default; `API_PORT`
   in your config if you chose another at setup)

### Desktop app shows blank screen

1. Open DevTools (Ctrl+Shift+I or Cmd+Option+I)
2. Check Console for errors
3. Try clearing localStorage and refreshing

---

## Next Steps

- Read the full documentation in `docs/`
- Customize Nymeria's personality in `nymeria/config/soul.md`
- Explore other entry points like `python3 run.py cli`, `python3 run.py worker`, or `python3 run.py mcp`
- Run as foreground gateway: `python3 run.py service run`
- Install the slim backend as a background service (starts at boot/login, restarts on crash): `python3 run.py service install`; manage it with `python3 run.py service status|restart|uninstall` (or the `nymeria service ...` console script)
- Enable tab completion: `python3 run.py completion bash >> ~/.bashrc && source ~/.bashrc` (also supports `zsh` and `fish`)

## Getting Help

- GitHub Issues: Report bugs or request features
- Check logs in `data/logs/` for debugging
