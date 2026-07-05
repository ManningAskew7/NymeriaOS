# triggers/

All entry points into Nymeria: API server, bots, and CLI.

## Start here

`api.py`  -  FastAPI app factory, auth middleware, CORS setup.

## Contents

- `api.py`  -  FastAPI application factory
- `sse_consumer.py`  -  shared SSE event dispatcher for all surfaces
- `discord_bot.py`, `telegram_bot.py`, `slack_bot.py`  -  standalone bot entry points
- `whatsapp_bot.py`  -  WhatsApp Cloud API webhook runtime used by the API router; not a standalone entry point
- `teams_bot.py`  -  Microsoft Teams Bot Framework webhook runtime used by the API router; not a standalone entry point
- `api_client.py`  -  httpx client used by thin-client bots and the Docker worker
- `bot_helpers.py`  -  shared bot utilities
- `slash_dispatcher.py`  -  slash command routing
- `cli/`  -  interactive CLI
- `sources/`  -  event trigger source implementations
- `discord_cogs/`  -  Discord slash command cogs

The former `watchdog_worker.py` daemon was folded into the ticker
(`core/watchdog_sweep.py`, backlog #78); there is no standalone watchdog
entry point anymore.
