# Nymeria Quick Start Guide

Get Nymeria running in under 10 minutes.

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

## Step 1: Install Nymeria

### One-line installer (front door)

The installer lets you pick a track and runs the right commands for you:

```bash
curl -fsSL https://get.nymeriaos.com/install.sh | sh
```

It asks whether you want **Slim** (simpler, best for a few users; single process
on SQLite via `uv`, no Docker) or **Full** (more robust, multi-user; runs in
Docker, and the script can install Docker for you on Linux). Non-interactive use:
`... | sh -s -- --slim` or `--full`. Cautious users can download and read it
first (`curl -fsSL https://get.nymeriaos.com/install.sh -o install.sh`).

Note: the one-line installer requires the hosted endpoint
(`get.nymeriaos.com`, plus the published container images for the Full track) to
be live. Until then, use the manual commands below, which work today.

### Manual install

### Beta Package Install

Use this path when you have private beta package access. `uv` is the
recommended installer (it is fast and can fetch a matching Python for you);
`pipx` also works.

With `uv` (recommended). `--index` adds the private index alongside public PyPI,
so Nymeria comes from the private index and its dependencies from PyPI:

```bash
export NYMERIA_PYPI_SIMPLE_INDEX_URL="https://<user>:<token>@<registry-host>/<repo>/simple/"
uv tool install nymeria --index "$NYMERIA_PYPI_SIMPLE_INDEX_URL"
nymeria init
nymeria doctor
nymeria api
```

To try Nymeria without a persistent install, run it ephemerally with
`uvx --index "$NYMERIA_PYPI_SIMPLE_INDEX_URL" nymeria slim`.

With `pipx`:

```bash
export NYMERIA_PYPI_SIMPLE_INDEX_URL="https://<user>:<token>@<registry-host>/<repo>/simple/"
pipx install nymeria --index-url "$NYMERIA_PYPI_SIMPLE_INDEX_URL"
nymeria init
nymeria doctor
nymeria api
```

If the private index does not proxy public PyPI dependencies, keep the private
index as the primary source for Nymeria and add public PyPI for dependencies:

```bash
pipx install nymeria \
  --index-url "$NYMERIA_PYPI_SIMPLE_INDEX_URL" \
  --pip-args="--extra-index-url https://pypi.org/simple"
```

The default install is lean. Optional chat-platform bots and heavy integrations
install as extras, for example `uv tool install "nymeria[discord]"` (or
`pipx install "nymeria[discord]"`). Use `nymeria[bots]` for every chat platform,
or combine extras like `nymeria[postgres,redis,voice]`. Available extras:
`discord`, `telegram`, `slack`, `mattermost`, `rocketchat`, `matrix`, `zulip`,
`signal`, `bots`, `postgres`, `redis`, `voice`, `browser`, `firebase`, `all`.

`nymeria init` prompts for the hosting/security profile, provider auth method,
provider, model, API key, setup style, data directory, and what to do next
after config is written. The model step offers a provider-specific default and
lets you press Enter to accept it.

For package installs, choose the Python virtual environment / pipx hosting
option. It isolates Python dependencies, but it is not an OS security sandbox:
Nymeria can still access files your user can access when tools are enabled.
The Docker option is a source-checkout handoff for direct API-key setup; it
prints compose steps and exits without writing `config.env` or `.env.docker`.

The normal first-run path is direct API-key authentication. It validates the
provider key with a small LLM API call, writes `~/.nymeria/config.env`, creates
`~/.nymeria/data/`, and creates the first bootstrap admin token. Recommended
setup writes only the primary provider credential and defers optional
capability keys. Advanced setup can write optional provider keys and a separate
`NYMERIA_DATA_DIR`.

CLIProxy Claude OAuth and CLIProxy Codex/OpenAI OAuth are advanced
source-checkout paths. They use the existing pinned
`CLIProxyAPI-main/temp/latest/` deployment, may start that Docker compose
service, require active local OAuth auth files, and write Nymeria config only
after the relevant proxy verification passes. Use direct API keys unless you
specifically need this subscription-routing path.

The final validation prompt can run `nymeria doctor --skip-llm-test`; because
provider auth was already tested, the full doctor LLM call runs only when you
ask for it. The final handoff prompt can print backend commands, print the
`nymeria cli` handoff, or show the backend/web UI start command.
`nymeria doctor` checks the installed Python version, config files, data
directory, LLM connectivity, local databases, optional Redis/voice setup,
bundled frontend, and API port before you start the server.

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

`nymeria init --hosting docker` can be used as a command reminder, but it does
not generate `.env.docker` yet. Create `.env.docker` from the example and edit
it in the source checkout so provider credentials are written to the Docker
runtime config, not to a packaged `config.env`.

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
`--non-interactive` plus flags such as `--provider`, `--model`, `--api-key`,
and `--root`. Use `--data-dir` with `--setup-style advanced` when the writable
data directory should be separate from the runtime root. The current scripted
direct setup also accepts
`--hosting venv|bare_metal`, `--auth-method api_key`,
`--setup-style advanced|recommended`, and `--next-action print_commands|cli|start_api_open_frontend`;
when these are omitted, it keeps the old direct API-key setup and prints the
commands to run next. `--hosting docker` prints the Docker source-checkout
handoff and exits without writing `config.env` or `.env.docker` for direct
API-key setup.

Scripted CLIProxy setup uses `--auth-method cliproxy_claude_oauth` or
`--auth-method cliproxy_codex_oauth`. Pass `--cliproxy-root <path>` if the
pinned `CLIProxyAPI-main/temp/latest/` directory is not in the default source
checkout location, and pass `--cliproxy-base-url <url>` if the host-reachable
proxy URL is not the compose-published default. In non-interactive mode, setup
fails with exact manual OAuth steps if no active local auth JSON is present.

Add `--skip-llm-test` only when you intentionally want to write direct API-key
config without validating provider access. Non-interactive
`--setup-style recommended` rejects optional capability keys and `--data-dir`;
use advanced setup for those values. Add `--run-doctor` for the quick
post-init doctor check in scripted setup, or `--full-doctor` when you also want
doctor to make its own live LLM check.

To diagnose an existing install without changing files, run:

```bash
nymeria doctor
```

If you are offline or intentionally testing without provider access, use
`nymeria doctor --skip-llm-test` to keep the rest of the checks useful.

## Step 4: Start the Backend

For solo/local use the simplest path is `python3 run.py slim`, which runs
the API, ticker, MCP, and watchdog in a single process backed by SQLite;
no Docker, no Redis, no Postgres:

```bash
python3 run.py slim
```

You should see:
```
Starting Nymeria SLIM (single-process) on 127.0.0.1:8000
  - Mode: SQLite + in-process ticker + embedded MCP
  - Internal API URL: http://127.0.0.1:8000
  - Data directory: <data_dir>
  - MCP endpoint: http://127.0.0.1:8000/mcp
```

`python3 run.py slim` writes an internal `data/SLIM_SERVICE_TOKEN.txt`
(mode 0600) on first boot so embedded MCP, the watchdog, and trigger fires
can authenticate against the API in the same process. That is NOT the
human bootstrap token; paste `data/BOOTSTRAP_TOKEN.txt` into the desktop
Setup Wizard, not `SLIM_SERVICE_TOKEN.txt`. See
[deployment/slim.md](deployment/slim.md) for the full launcher reference.

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
Starting Nymeria API server on 0.0.0.0:8000
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

For beta package installs and source checkouts with a bundled frontend, open:

```text
http://localhost:8000
```

The backend-served web UI auto-detects the current origin as the API URL after
`/health` succeeds.

For the Windows desktop app:

1. Start or choose a separately installed backend
2. Open the Nymeria desktop app
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

Start chatting with Nymeria. Here are some things to try:

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
3. Check firewall isn't blocking port 8000

### Desktop app shows blank screen

1. Open DevTools (Ctrl+Shift+I or Cmd+Option+I)
2. Check Console for errors
3. Try clearing localStorage and refreshing

---

## Next Steps

- Read the full documentation in `docs/`
- Customize Nymeria's personality in `nymeria/config/soul.md`
- Explore other entry points like `python3 run.py cli`, `python3 run.py worker`, or `python3 run.py mcp`
- Run as foreground gateway: `python3 run.py service`
- Enable tab completion: `python3 run.py completion bash >> ~/.bashrc && source ~/.bashrc` (also supports `zsh` and `fish`)

## Getting Help

- GitHub Issues: Report bugs or request features
- Check logs in `data/logs/` for debugging
