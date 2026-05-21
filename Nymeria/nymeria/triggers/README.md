# triggers/

All entry points into Nymeria: API server, bots, CLI, and watchdog.

## Start here

`api.py`  -  FastAPI app factory, auth middleware, CORS setup.

## Contents

- `api.py`  -  FastAPI application factory
- `sse_consumer.py`  -  shared SSE event dispatcher for all surfaces
- `discord_bot.py`, `telegram_bot.py`, `slack_bot.py`, `matrix_bot.py`, `mattermost_bot.py`, `zulip_bot.py`, `rocketchat_bot.py`, `signal_bot.py`, `twitch_bot.py`  -  bot entry points
- `whatsapp_bot.py`  -  WhatsApp Cloud API webhook runtime used by the API router; not a standalone entry point
- `messenger_bot.py`  -  Meta Messenger webhook runtime used by the API router; not a standalone entry point
- `instagram_bot.py`  -  Meta Instagram Messaging webhook runtime used by the API router; not a standalone entry point
- `webex_bot.py`  -  Webex Messaging webhook runtime used by the API router; not a standalone entry point
- `teams_bot.py`  -  Microsoft Teams Bot Framework webhook runtime used by the API router; not a standalone entry point
- `google_chat_bot.py`  -  Google Chat HTTPS webhook runtime used by the API router; not a standalone entry point
- `line_bot.py`  -  LINE Messaging API webhook runtime used by the API router; not a standalone entry point
- `api_client.py`  -  httpx client used by thin-client bots
- `bot_helpers.py`  -  shared bot utilities
- `watchdog_worker.py`  -  TODO watchdog daemon
- `slash_dispatcher.py`  -  slash command routing
- `cli/`  -  interactive CLI
- `sources/`  -  event trigger source implementations
- `discord_cogs/`  -  Discord slash command cogs
