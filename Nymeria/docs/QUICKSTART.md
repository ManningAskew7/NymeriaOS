# Nymeria Quick Start Guide

Get Nymeria running in under 10 minutes.

## Prerequisites

- **Python 3.10+** - Check with `python --version`
- **LLM API Key** - From one of:
  - [Anthropic](https://console.anthropic.com/) (recommended)
  - [OpenAI](https://platform.openai.com/)
  - [OpenRouter](https://openrouter.ai/)

## Step 1: Install Dependencies

```bash
cd Nymeria
pip install -r requirements.txt
```

## Step 2: Configure Environment

Copy the minimal configuration template:

```bash
cp .env.minimal .env
```

Or for all options:

```bash
cp .env.example .env
```

## Step 3: Set Your API Keys

Edit `.env` and fill in the required values:

### Generate NYMERIA_API_KEY

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copy the output to `NYMERIA_API_KEY=` in your `.env` file.

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

## Step 4: Start the Backend

```bash
python run.py api
```

You should see:
```
Starting Nymeria API server on 0.0.0.0:8000...
  - Docs: http://0.0.0.0:8000/docs
  - ReDoc: http://0.0.0.0:8000/redoc
```

## Step 5: Connect the Desktop App

1. Open the Nymeria desktop app
2. The setup wizard will guide you through:
   - Entering the backend URL (default: `http://localhost:8000`)
   - Entering your `NYMERIA_API_KEY`
   - Testing the connection

## You're Done!

Start chatting with Nymeria. Here are some things to try:

- "Remember that my name is [your name]"
- "Create a TODO to remind me to check email in 2 hours"
- "What can you help me with?"

---

## Troubleshooting

### "NYMERIA_API_KEY not set"

Generate a key and add it to your `.env`:
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### "No API key for LLM provider"

Make sure you've set the API key for your chosen provider in `.env`:
- `ANTHROPIC_API_KEY` for Anthropic
- `OPENAI_API_KEY` for OpenAI
- `OPENROUTER_API_KEY` for OpenRouter

### "Cannot connect to server"

1. Check the backend is running: `python run.py api`
2. Check the URL in the desktop app matches the backend
3. Check firewall isn't blocking port 8000

### "Invalid API key"

The `NYMERIA_API_KEY` in your backend `.env` must match exactly what you enter in the desktop app.

### Desktop app shows blank screen

1. Open DevTools (Ctrl+Shift+I or Cmd+Option+I)
2. Check Console for errors
3. Try clearing localStorage and refreshing

---

## Next Steps

- Read the full documentation in `docs/`
- Customize Nymeria's personality in `nymeria/config/soul.md`
- Add custom tools in `nymeria/tools/`
- Install as a Windows service: `python run.py service install`

## Getting Help

- GitHub Issues: Report bugs or request features
- Check logs in `data/logs/` for debugging
