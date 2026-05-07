# Nymeria Quick Start Guide

Get Nymeria running in under 10 minutes.

## Prerequisites

- **Python 3.10+** - Check with `python --version`
- **LLM API Key** - From one of:
  - [Anthropic](https://console.anthropic.com/) (recommended)
  - [OpenAI](https://platform.openai.com/)
  - [OpenRouter](https://openrouter.ai/)

## Step 1: Install Dependencies

For beta package installs:

```bash
pipx install nymeria --index-url <private-index-url>
nymeria init
nymeria api
```

`nymeria init` prompts for the provider, model, API key, and optional
capability keys. It writes `~/.nymeria/config.env`, creates
`~/.nymeria/data/`, and creates the first bootstrap admin token.

For source-checkout development:

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

For a full local config template:

```bash
cp .env.docker.example .env.docker
```

Or create `.env` manually for a lighter local setup. The runtime loads `.env`,
`config.env`, and `.env.docker` if present.

## Step 3: Set Your API Keys

Edit `.env` or `.env.docker` and fill in the required values:

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

## Step 4: Start the Backend

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
  - Docs: http://0.0.0.0:8000/docs
  - ReDoc: http://0.0.0.0:8000/redoc
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

## Step 5: Connect the Desktop App

1. Open the Nymeria desktop app
2. The setup wizard will guide you through:
   - Entering the backend URL (default: `http://localhost:8000`; auto-filled
     from the current page origin when the web UI is served by the backend)
   - Pasting the bootstrap account token from `<data_dir>/BOOTSTRAP_TOKEN.txt`
     (also logged at WARNING level on first API boot)
   - Testing the connection

## You're Done!

Start chatting with Nymeria. Here are some things to try:

- "Remember that my name is [your name]"
- "Create a TODO to remind me to check email in 2 hours"
- "What can you help me with?"

---

## Troubleshooting

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
