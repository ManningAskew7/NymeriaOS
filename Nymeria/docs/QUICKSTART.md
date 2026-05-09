# Nymeria Quick Start Guide

Get Nymeria running in under 10 minutes.

## Prerequisites

- **Python 3.11+** - Check with `python --version`
- **pipx** - Needed for beta package installs (`python -m pip install --user pipx`)
- **LLM API Key** - From one of:
  - [Anthropic](https://console.anthropic.com/) (recommended)
  - [OpenAI](https://platform.openai.com/)
  - [OpenRouter](https://openrouter.ai/)

## Step 1: Install Nymeria

### Beta Package Install

Use this path when you have private beta package access:

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

See [BETA_PRIVATE_INDEX.md](./BETA_PRIVATE_INDEX.md) for private index setup,
upgrade commands, and the GitHub Release wheel fallback.

`nymeria init` prompts for the hosting/security profile, provider, model, API
key, and optional capability keys. The model step offers a provider-specific
default and lets you press Enter to accept it. For package installs, choose the Python
virtual environment / pipx hosting option: it isolates Python dependencies, but
it is not an OS security sandbox. Nymeria can still access files your user can
access when tools are enabled. The Docker option prints source-checkout Docker
steps and exits without writing `config.env` or `.env.docker`. The venv/pipx
path validates the provider key with a small LLM API call, writes
`~/.nymeria/config.env`, creates `~/.nymeria/data/`, and creates the first
bootstrap admin token. `nymeria doctor` checks the installed Python version,
config files, data directory, LLM connectivity, local databases, optional
Redis/voice setup, bundled frontend, and API port before you start the server.

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
pip install -r requirements.txt
```

From a source checkout, you can also install the backend package in editable
mode. This uses `pyproject.toml` and keeps the `nymeria` package importable
while you work:

```bash
pip install -e .
```

SQLite is the default backend and needs no extra packages. If you want local
development to use PostgreSQL instead, install the Postgres checkpoint extras
after the base dependencies:

```bash
pip install -r requirements-postgres.txt
# or, when using the package metadata:
pip install -e ".[postgres]"
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

The legacy shared `NYMERIA_API_KEY` was retired in the multi-user refactor. The first time the API boots with an empty accounts DB it auto-creates a `default` admin user, prints the raw token to the API log at WARNING level, and writes it to `<data_dir>/BOOTSTRAP_TOKEN.txt` (mode 0600). Paste that token into the desktop/mobile Setup Wizard, then delete the file. See `docs/accounts.md` for the full account model and the `python run.py users …` CLI for provisioning additional users.

### Set Your LLM Provider API Key

For Anthropic (default):
```ini
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...your-key-here...
```

For OpenAI:
```ini
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...your-key-here...
```

For OpenRouter:
```ini
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...your-key-here...
```

Optional capability keys:

```ini
EMBEDDING_API_KEY=sk-...      # Semantic memory/RAG/skill search
OPENAI_API_KEY=sk-...         # OpenAI image generation/STT/OpenAI-backed tools
GEMINI_API_KEY=...            # Gemini image/document extraction/TTS tools
PERPLEXITY_API_KEY=pplx-...   # Web search
```

If `LLM_PROVIDER=openai`, the primary `OPENAI_API_KEY` also covers optional
OpenAI-backed features.

For scripted setup in CI or an offline support session, `nymeria init` accepts
`--non-interactive` plus flags such as `--provider`, `--model`, `--api-key`,
and `--root`. The current scripted direct setup also accepts
`--hosting venv|bare_metal`, `--auth-method api_key`,
`--setup-style advanced|recommended`, and `--next-action print_commands|cli|start_api_open_frontend`;
when these are omitted, it keeps the old direct API-key setup and prints the
commands to run next. `--hosting docker` prints the Docker source-checkout
handoff and exits without writing `config.env` or `.env.docker`. CLIProxy OAuth
auth methods print a guarded planning handoff with the documented proxy
commands and also exit without writing Nymeria config. Add `--skip-llm-test`
only when you intentionally want to write the config without validating
provider access.

To diagnose an existing install without changing files, run:

```bash
nymeria doctor
```

If you are offline or intentionally testing without provider access, use
`nymeria doctor --skip-llm-test` to keep the rest of the checks useful.

## Step 4: Start the Backend

For a source checkout:

```bash
python run.py api
```

When installed as a package, use:

```bash
nymeria api
```

You should see:
```
Starting Nymeria API server on 0.0.0.0:8000...
  - API docs: disabled (set NYMERIA_API_DOCS=true to enable)
```

### Backend Validation

Install the development requirements when you want to run backend linting,
tests, or coverage. Docker production images intentionally omit `pytest`,
`pytest-cov`, `ruff`, and other dev-only packages:

```bash
pip install -r requirements-dev.txt
# or, when using the package metadata:
pip install -e ".[dev]"
python -m ruff check nymeria tests run.py
python -m pytest tests --cov=nymeria --cov=run --cov-report=term --cov-fail-under=38
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
   - Pasting a `nym_...` account token, such as the bootstrap token from
     `<data_dir>/BOOTSTRAP_TOKEN.txt` on a first local backend boot
   - Testing the connection

## You're Done!

Start chatting with Nymeria. Here are some things to try:

- "Remember that my name is [your name]"
- "Create a TODO to remind me to check email in 2 hours"
- "What can you help me with?"

---

## Troubleshooting

For beta install diagnostics, provider/key failures, database locks, and
`nymeria doctor` output, see [BETA_TROUBLESHOOTING.md](./BETA_TROUBLESHOOTING.md).

### "Invalid API key" / 401 from the desktop app

The legacy `NYMERIA_API_KEY` shared key was retired. Authentication now uses per-user account tokens. Read `<data_dir>/BOOTSTRAP_TOKEN.txt` (written automatically on the first API boot) for the bootstrap admin token. To mint another user's token: `python run.py users add <email> --role user --id <slug>`. Both flows are documented in `docs/accounts.md`.

### "No API key for LLM provider"

Make sure you've set the API key for your chosen provider in your environment file:
- `ANTHROPIC_API_KEY` for Anthropic
- `OPENAI_API_KEY` for OpenAI
- `OPENROUTER_API_KEY` for OpenRouter

### "Cannot connect to server"

1. Check the backend is running: `python run.py api`
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
- Explore other entry points like `python run.py cli`, `python run.py worker`, or `python run.py mcp`
- Run as foreground gateway: `python run.py service`

## Getting Help

- GitHub Issues: Report bugs or request features
- Check logs in `data/logs/` for debugging
