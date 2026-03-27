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
https://id.twitch.tv/oauth2/authorize?response_type=code&client_id=YOUR_CLIENT_ID&redirect_uri=http://localhost:3000&scope=channel:bot+channel:manage:polls+channel:manage:predictions+channel:manage:broadcast+channel:read:subscriptions+channel:moderate
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
| `!pulse on/off/<seconds>/min <count>` | Mods, Broadcaster | None | Control pulse (enable/disable/interval/min messages) |
| `!context` | Mods, Broadcaster | None | Show context window token usage and compaction count |
| `!stop` / `!start` | Mods, Broadcaster | None | Kill switch — disable/re-enable all agent responses |
| `!help` | Everyone | None | List available commands (shows mod commands to mods) |

## Chat Pulse

The bot periodically evaluates recent chat and may comment if something interesting is happening.

- **Interval**: configurable via `TWITCH_PULSE_INTERVAL` (default 300s, live via `!pulse <seconds>`)
- **Minimum activity**: configurable via `TWITCH_PULSE_MIN_MESSAGES` (default 10, live via `!pulse min <count>`)
- **Behavior**: the agent receives only **unseen** messages and decides whether to call `twitch_send` or stay silent

The pulse and `!ask` share a delivery cursor — messages are only sent to the agent once across both paths. If a pulse is skipped (too few messages), those messages carry over to the next delivery. This eliminates duplicate token spend from repeated context.

The pulse only fires when there's new activity, so it won't waste tokens when the stream is offline or chat is dead.

## Moderation Event Awareness

The bot subscribes to Twitch EventSub moderation events so it can see mod actions in its chat context:

- **Bans and timeouts** (`channel.ban`) — requires `channel:moderate` scope on broadcaster token
- **Unbans** (`channel.unban`) — requires `channel:moderate` scope on broadcaster token
- **Message deletions** (`channel.chat.message_delete`) — requires `user:read:chat` scope on bot token

Mod events appear in the chat buffer as system messages formatted as `[MOD] moderator_name banned/timed out/unbanned user_name`. This gives the agent awareness of ongoing moderation so it doesn't duplicate mod actions or miss context.

The bot also attempts the unified `channel.moderate` v2 subscription (covers all mod actions including warns) but this requires many `moderator:read:*` scopes. See the OAuth Scopes section for details.

## Tools (21 total)

All tools are registered as OPTIONAL_TOOLS and enabled per-thread via thread config. Tools marked with **[auto]** are auto-enabled on first start; others need manual enabling.

### Chat Tools

| Tool | Token | Auto | Description |
|------|-------|------|-------------|
| `twitch_send` | Bot | **[auto]** | Send a message to chat (max 500 chars) |
| `twitch_announce` | Bot | **[auto]** | Send a highlighted announcement (color options) |
| `twitch_delete_message` | Bot | | Delete a specific message by ID, or clear all chat |

### Moderation Tools

| Tool | Token | Auto | Description |
|------|-------|------|-------------|
| `twitch_timeout` | Bot | **[auto]** | Timeout a user (1-1800 seconds) |
| `twitch_ban` | Bot | **[auto]** | Permanently ban a user |
| `twitch_unban` | Bot | **[auto]** | Lift a ban or timeout |
| `twitch_warn` | Bot | **[auto]** | Issue an official warning popup to a user |
| `twitch_automod_review` | Bot | | Approve or deny an AutoMod-held message |
| `twitch_shoutout` | Bot | | Shoutout another channel (2-min cooldown per target) |

### Channel & Stream Info

| Tool | Token | Auto | Description |
|------|-------|------|-------------|
| `twitch_get_stream` | Bot | **[auto]** | Live status, viewer count, game, title, uptime |
| `twitch_get_channel` | Bot | **[auto]** | Channel title, game, tags, language |
| `twitch_get_chatters` | Bot | **[auto]** | List users currently in chat + count |
| `twitch_get_banned` | Bot | | List banned users with reasons |
| `twitch_get_schedule` | Bot | **[auto]** | Upcoming stream schedule |
| `twitch_clip` | Bot | | Clip last ~30 seconds of live stream |

### Broadcaster Actions (require broadcaster token)

| Tool | Token | Auto | Description |
|------|-------|------|-------------|
| `twitch_create_poll` | Broadcaster | | Create a chat poll (2-5 choices) |
| `twitch_end_poll` | Broadcaster | | End or archive an active poll |
| `twitch_create_prediction` | Broadcaster | | Create a channel points prediction |
| `twitch_resolve_prediction` | Broadcaster | | Resolve, cancel, or lock a prediction |
| `twitch_set_channel_info` | Broadcaster | | Change stream title, game, or tags |
| `twitch_get_subs` | Broadcaster | | Check sub count or if a user is subscribed |

All tools that accept usernames resolve them automatically via the Helix API.

### OAuth Scopes Required

**Bot account** (13 scopes):
```
user:read:chat user:write:chat user:bot channel:bot
moderator:manage:banned_users moderator:manage:chat_messages
moderator:manage:announcements moderator:manage:shoutouts
moderator:manage:warnings moderator:manage:automod
moderator:read:chatters moderator:read:banned_users clips:edit
```

**Broadcaster account** (6 scopes):
```
channel:bot channel:manage:polls channel:manage:predictions
channel:manage:broadcast channel:read:subscriptions channel:moderate
```

Use the auth helper to generate URLs with all scopes: `python tools/twitch_auth.py url`

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
| `TWITCH_PULSE_MIN_MESSAGES` | `10` | Minimum new messages before pulse fires |
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
| `nymeria/tools/twitch.py` | All 21 Twitch tools (OPTIONAL_TOOLS) |
| `nymeria/config/settings.py` | Twitch settings fields |
| `run.py` | `twitch-bot` subcommand entry point |
| `docker-compose.yml` | `twitch-bot` service (profile: twitch) |
| `tools/twitch_auth.py` | OAuth helper — URL generation, code exchange, token validation |

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
