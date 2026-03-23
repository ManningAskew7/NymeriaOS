# Twitch Bot

Nymeria's Twitch integration connects to a Twitch channel via TwitchIO v3, buffers chat messages, responds to `!commands`, and periodically evaluates chat ("pulse"). It runs as a separate Nymeria instance in its own Docker container, sharing PostgreSQL and Redis with the main stack.

## Architecture

```
Docker: nymeria-twitch-bot (profile: twitch)
  └─ NymeriaAgent (enable_ticker=False)
       └─ Thread: twitch_{channel} (configured via ThreadConfig)
            ├─ system_prompt: Bot personality (auto-set on first start, editable via API/UI)
            ├─ enabled_tools: twitch_send, twitch_timeout, twitch_ban, twitch_unban, twitch_announce
            └─ LLM config: inherits global or per-thread override
```

The bot communicates exclusively through the `twitch_send` tool — the agent's final text output is never sent to chat. This gives the agent full control over when and how many messages it sends.

## Setup

### 1. Create a Twitch Application

1. Go to https://dev.twitch.tv/console/apps
2. Register a new application (type: Confidential, category: Chat Bot)
3. Set OAuth redirect URL to `http://localhost:3000`
4. Copy the **Client ID** and **Client Secret**

### 2. Create a Bot Account

1. Create a new Twitch account for the bot
2. Get the bot's numeric user ID via the Twitch API:
   ```bash
   # Get an app token first
   curl -s -X POST 'https://id.twitch.tv/oauth2/token' \
     -d 'client_id=YOUR_CLIENT_ID&client_secret=YOUR_SECRET&grant_type=client_credentials' \
     | python3 -m json.tool

   # Look up user ID
   curl -s -H 'Client-ID: YOUR_CLIENT_ID' \
     -H 'Authorization: Bearer APP_TOKEN' \
     'https://api.twitch.tv/helix/users?login=botusername' \
     | python3 -m json.tool
   ```
3. Have the channel owner mod the bot: `/mod botusername`

### 3. Generate OAuth Tokens

Two tokens are needed: one for the **bot account** and one for the **broadcaster** (channel owner).

**Bot token** — log in as the bot account and visit:
```
https://id.twitch.tv/oauth2/authorize?response_type=code&client_id=YOUR_CLIENT_ID&redirect_uri=http://localhost:3000&scope=user:read:chat+user:write:chat+user:bot+moderator:manage:banned_users+moderator:manage:chat_messages+moderator:manage:announcements+channel:bot
```

**Broadcaster token** — log in as the channel owner and visit:
```
https://id.twitch.tv/oauth2/authorize?response_type=code&client_id=YOUR_CLIENT_ID&redirect_uri=http://localhost:3000&scope=channel:bot
```

For both: the page won't load (nothing listens on localhost:3000). Copy the `code` parameter from the URL bar and exchange it:
```bash
curl -s -X POST 'https://id.twitch.tv/oauth2/token' \
  -d 'client_id=YOUR_CLIENT_ID' \
  -d 'client_secret=YOUR_SECRET' \
  -d 'code=THE_CODE' \
  -d 'grant_type=authorization_code' \
  -d 'redirect_uri=http://localhost:3000'
```

TwitchIO auto-refreshes tokens after the initial setup.

### 4. Configure Environment

Add to `.env.docker`:
```bash
TWITCH_CLIENT_ID=your-client-id
TWITCH_CLIENT_SECRET=your-client-secret
TWITCH_BOT_ACCESS_TOKEN=bot-access-token
TWITCH_BOT_REFRESH_TOKEN=bot-refresh-token
TWITCH_BOT_USER_ID=bot-numeric-user-id
TWITCH_BROADCASTER_TOKEN=broadcaster-access-token
TWITCH_BROADCASTER_REFRESH_TOKEN=broadcaster-refresh-token
TWITCH_CHANNEL=channelname
```

### 5. Start

```bash
DISCORD_BOT_TOKEN=disabled docker compose --profile twitch --env-file .env.docker up -d --build

# Reconnect CLIProxyAPI if needed
docker network connect nymeria_nymeria-network cli-proxy-api

# Check logs
docker logs nymeria-twitch-bot --tail 20
```

## Chat Commands

| Command | Access | Cooldown | Description |
|---------|--------|----------|-------------|
| `!ask <question>` | Subs, VIPs, Mods, Broadcaster | 30s/user, 10s/global | Ask the AI a question with recent chat context |
| `!status` | Everyone | None | Show model, uptime, buffer count, pulse status |
| `!clear` | Mods, Broadcaster | None | Clear conversation history |

## Chat Pulse

The bot periodically evaluates recent chat and may comment if something interesting is happening.

- **Interval**: configurable via `TWITCH_PULSE_INTERVAL` (default 180s / 3 minutes)
- **Minimum activity**: requires 10+ new messages since last pulse to fire
- **Behavior**: the agent receives recent messages and decides whether to call `twitch_send` or stay silent

The pulse only fires when there's new activity, so it won't waste tokens when the stream is offline or chat is dead.

## Moderation Tools

These are registered as optional tools and auto-enabled on the Twitch thread:

| Tool | Description |
|------|-------------|
| `twitch_send` | Send a message to chat (max 500 chars) |
| `twitch_timeout` | Timeout a user (1-1800 seconds) |
| `twitch_ban` | Permanently ban a user |
| `twitch_unban` | Lift a ban or timeout |
| `twitch_announce` | Send a highlighted announcement |

All moderation tools accept usernames (not numeric IDs) and resolve them automatically via the Helix API.

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `TWITCH_CLIENT_ID` | — | Twitch application Client ID (required) |
| `TWITCH_CLIENT_SECRET` | — | Twitch application Client Secret |
| `TWITCH_BOT_ACCESS_TOKEN` | — | Bot's OAuth access token (required) |
| `TWITCH_BOT_REFRESH_TOKEN` | — | Bot's OAuth refresh token |
| `TWITCH_BOT_USER_ID` | — | Bot's numeric Twitch user ID |
| `TWITCH_BROADCASTER_TOKEN` | — | Broadcaster's OAuth token (channel:bot scope) |
| `TWITCH_BROADCASTER_REFRESH_TOKEN` | — | Broadcaster's refresh token |
| `TWITCH_CHANNEL` | `silk` | Channel to join |
| `TWITCH_BUFFER_SIZE` | `500` | Max messages in ring buffer |
| `TWITCH_PULSE_ENABLED` | `true` | Enable periodic chat pulse |
| `TWITCH_PULSE_INTERVAL` | `300` | Seconds between pulse checks |
| `TWITCH_PULSE_MESSAGE_COUNT` | `100` | Messages to include in pulse context |
| `TWITCH_COMMAND_CONTEXT_COUNT` | `50` | Messages to include with !ask context |
| `TWITCH_RESPOND_MODE` | `command` | Response mode (command = only !commands) |

## Thread Configuration

The bot auto-creates a `twitch_{channel}` thread with a default system prompt on first start. You can customize the personality, tools, and LLM settings via the API or desktop UI:

```bash
curl -X PUT "http://localhost:8000/threads/twitch_silk/config" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"system_prompt": "Your custom bot personality..."}'
```

## Files

| File | Purpose |
|------|---------|
| `nymeria/triggers/twitch_bot.py` | Main bot class, commands, pulse loop |
| `nymeria/tools/twitch.py` | Moderation tools (OPTIONAL_TOOLS) |
| `nymeria/config/settings.py` | Twitch settings fields |
| `run.py` | `twitch-bot` subcommand entry point |
| `docker-compose.yml` | `twitch-bot` service (profile: twitch) |

## Debugging

```bash
# Container logs
docker logs nymeria-twitch-bot --tail 50

# Follow logs live
docker logs nymeria-twitch-bot -f

# Check if EventSub subscription is active
docker logs nymeria-twitch-bot 2>&1 | grep -i subscri

# Check token status
docker logs nymeria-twitch-bot 2>&1 | grep -i token
```
