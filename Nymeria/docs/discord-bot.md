# Discord Bot

Nymeria's Discord integration runs as a stateless gateway that translates Discord slash commands and mentions into Nymeria REST API calls. All state lives in the API container — the bot is a thin client with no local persistence (except a per-channel context toggle held in memory).

## Architecture

```
Docker: nymeria-discord-bot (profile: discord)
  └─ NymeriaDiscordBot(discord.Client)
       ├─ NymeriaAPIClient (async httpx → Nymeria REST API)
       ├─ CommandTree (30 slash commands across 6 groups)
       └─ SSE listener (autonomous task completion → channel posts)
```

Unlike the Twitch bot (which calls `agent.chat()` directly), the Discord bot communicates exclusively via the REST API. Chat responses are streamed via SSE (`POST /chat`) — users see text appear progressively as the model generates it, with tool call boundaries shown as visual separators. This means:

- The frontend always reflects the same state as Discord
- Context stats, compaction, and tool changes are visible in the UI
- No agent instance runs inside the bot container

### Thread & User ID Scheme

| Context | Thread ID | User ID |
|---------|-----------|---------|
| Guild channel | `discord_<guild_id>_<channel_id>` | `discord_<user_id>` |
| DM | `discord_dm_<channel_id>` | `discord_<user_id>` |

Each Discord channel maps to one Nymeria thread. Per-thread config (model, tools, instructions) applies per-channel.

## Setup

### 1. Create a Discord Application

1. Go to https://discord.com/developers/applications
2. Create a new application
3. Under **Bot**, click "Reset Token" and copy the token
4. Enable **Message Content Intent** under Privileged Gateway Intents
5. Under **OAuth2 → URL Generator**, select scopes `bot` + `applications.commands`
6. Select permissions: Send Messages, Read Message History, Use Slash Commands
7. Use the generated URL to invite the bot to your server

### 2. Configure Environment

Add to `.env.docker`:

```bash
DISCORD_BOT_TOKEN=your-bot-token
DISCORD_RESPOND_MODE=mention    # "mention" (default) or "all"
```

- **mention**: Bot only responds to @mentions in guilds; always responds in DMs
- **all**: Bot responds to every message in every channel (legacy/testing mode)

### 3. Start the Bot

```bash
# Start with other containers
docker compose --profile discord --env-file .env.docker up -d --build

# Restart after code changes (bind-mounted, no rebuild needed)
docker compose --env-file .env.docker restart discord-bot

# Logs
docker logs nymeria-discord-bot --tail 50
```

When not using the Discord bot, set `DISCORD_BOT_TOKEN=disabled` to prevent docker-compose from complaining about the missing env var.

### Slash Command Sync

Discord slash commands are synced **per-guild** in the `on_ready` hook — the bot clears any stale global commands, copies the local command tree to each guild, and calls `tree.sync(guild=guild)`. This means:

- **After adding/removing/renaming slash commands**, you must restart the bot container so `on_ready` fires and pushes the updated tree to Discord.
- Sync is near-instant for guild commands (unlike global commands which can take up to an hour).
- The Discord client may take a few seconds to refresh its autocomplete cache — if new commands don't appear immediately, close and reopen the slash command menu.

```bash
# Restart to sync new/changed commands
docker compose --env-file .env.docker restart discord-bot

# Verify sync succeeded in logs
docker logs nymeria-discord-bot --tail 15
# Look for: "Slash commands synced to guild: <name>"
```

## Commands Reference

### Chat

| Command | Description |
|---------|-------------|
| `/ask <message>` | Send a message to Nymeria. Includes recent channel context if enabled. |
| `/stop` | Abort the current running operation. |
| `/clear` | Clear conversation history for this channel (preserves notepad and tool config). |
| `/compact` | Compress conversation context to reclaim token space. |
| `/thread` | Show thread ID, context usage (%), token count, compaction count, and context mode. |
| `/context` | Detailed context breakdown: effective model, token usage with progress bar, tools by category, per-thread overrides (instructions, enabled/disabled tools, callable status). |
| `/tasks [status]` | Quick view of scheduled and autonomous tasks. Filter: `active` (default), `pending`, `in_progress`, `done`, `all`. Shows schedule time, recurrence, and bound thread. |
| `/export [format]` | Export conversation history as a file attachment. Format: `markdown` (default), `json`, `txt`. |
| `/restart [target]` | Restart the Discord bot (default) or API server (`/restart target:api`). Bot restarts use Docker's restart policy; API restart uses the existing `POST /restart` endpoint. |
| `/help` | List all available commands. |

`/ask` also fetches the last ~10 channel messages as context (configurable, see `/channel-context`).

### Model & Thinking

| Command | Description |
|---------|-------------|
| `/model [name] [scope]` | Show or change the LLM model. `scope` is `"global"` (server default) or `"thread"` (channel override). |
| `/models` | List all available models from the current provider with context window sizes. |
| `/think [mode]` | Set extended thinking mode: `off`, `on`, `low`, `medium`, `high`. Without argument, shows current state. |
| `/status` | Comprehensive dashboard: model, provider, context bar, tools count, uptime, watchdog, task counts, Discord respond mode and channel context state. |

### TODOs (`/todos`)

| Command | Description |
|---------|-------------|
| `/todos list [filter]` | List TODOs. Filter: `active` (default), `pending`, `in_progress`, `done`, `all`. |
| `/todos add <task> [schedule] [repeat] [notes]` | Create a TODO. Schedule: `"30m"`, `"2h"`, `"1d"`, or `"2024-12-25 14:00"`. Repeat: `5min` through `monthly`. Associates with the current channel. |
| `/todos complete <todo_id>` | Mark a TODO as done (first 8 chars of ID). Recurring TODOs auto-reschedule. |
| `/todos delete <todo_id>` | Permanently delete a TODO (first 8 chars of ID). |

### Config (`/config`)

| Command | Description |
|---------|-------------|
| `/config show` | Show all server settings: LLM config, context management, system flags. |
| `/config get <key>` | Get a specific setting value (e.g., `llm_model`, `context_management`). |
| `/config set <key> <value>` | Update a server setting. Auto-parses booleans, numbers, and `none`. |

### Tools (`/tools`)

| Command | Description |
|---------|-------------|
| `/tools core` | List core tools (always loaded by default). |
| `/tools optional` | List optional tool categories with per-channel active counts. |
| `/tools enabled` | Show all tools active in this channel: core (with any disabled), optional enabled. |
| `/tools category <name>` | List tools in a category with enabled/disabled status for this channel. |
| `/tools enable <name>` | Enable a tool or entire category for this channel. Accepts a tool name (e.g., `bash_execute`) or category name (e.g., `email`). Autocomplete suggests both. |
| `/tools disable <name>` | Disable a tool or entire category for this channel. Works for core tools (disabling a default) and optional tools. |

Tool overrides are per-thread (per-channel). Changes made with `/tools enable` and `/tools disable` are visible in `/tools enabled` and the frontend.

### Memory (`/memory`)

| Command | Description |
|---------|-------------|
| `/memory list` | List all saved memories for this user. |
| `/memory save <key> <value>` | Save a persistent memory (survives across conversations). |
| `/memory forget <key>` | Remove a saved memory. |
| `/memory search <query>` | Search memories by keyword (matches key and value). |

### Notepad (`/notepad`)

Per-channel persistent notes that survive conversation compaction.

| Command | Description |
|---------|-------------|
| `/notepad read` | Read this channel's notepad contents. |
| `/notepad write <content> [mode]` | Write to notepad. Mode: `append` (default) or `replace`. |
| `/notepad clear` | Clear this channel's notepad. |

### Channel Settings

| Command | Description |
|---------|-------------|
| `/show-tools` | Toggle whether tool calls are shown as embeds in chat. Defaults to hidden — only response text is shown, with horizontal rule separators at tool boundaries. When enabled, tool calls appear as blue embeds with arguments and results. |
| `/channel-context` | Toggle whether Nymeria includes recent channel messages as context in `/ask` and @mentions. Defaults to enabled. |

## Streaming Responses

Chat responses (`/ask` and @mentions) are streamed via SSE rather than waiting for the full response. Users see text appear progressively as the model generates it, with edits every ~1.5 seconds.

**Tool call display modes:**

- **Hidden (default):** Response text streams into a single progressively-edited message. Tool call boundaries are shown as horizontal rule separators (─────). Toggle with `/show-tools`.
- **Shown:** Text segments are sent as separate messages with tool call embeds (blue → green on completion) between them. Each embed shows the tool name, arguments, and result.

If streaming fails, the bot falls back to the sync `POST /chat/sync` endpoint automatically.

## Autonomous Task Delivery

The bot maintains a background SSE connection to `GET /autonomous/stream`. When a scheduled TODO completes on a Discord thread, the bot receives a `task_completed` event and posts an embed to the originating channel with the task description and result.

This means TODOs created via `/todos add` in a Discord channel will have their results delivered back to that channel automatically.

## API Client

`NymeriaAPIClient` (`nymeria/triggers/discord_api_client.py`) is a standalone async HTTP client wrapping the Nymeria REST API. It uses `httpx.AsyncClient` with:

- **Chat timeout**: 300s read (accommodates long LLM calls)
- **Default timeout**: 30s read
- **Bearer token auth** via `NYMERIA_API_KEY`

The client covers all API endpoints: chat, thread management, settings, tools, memories, TODOs, and models. It could be reused by other async integrations.

## Message Handling

### Response Splitting

Discord has a 2000-character message limit. The bot's `split_message()` function splits long responses intelligently:

1. Preserves code block boundaries (never splits inside ` ``` `)
2. Prefers paragraph breaks (`\n\n`)
3. Falls back to line breaks (`\n`), then sentence boundaries (`. `)
4. Hard-splits at 2000 chars only as last resort

### Channel Context

When enabled (default), the bot fetches the last ~10 non-bot messages from the channel and prepends them as context:

```
--- Recent channel messages (for context) ---
[14:30] Alice: has anyone seen the deploy logs?
[14:32] Bob: checking now
--- End of channel context ---
```

This helps Nymeria understand the ongoing conversation even when invoked via `/ask` rather than a direct @mention.

## Future Improvements

- **`/activation` command** — Toggle between `mention` and `all` respond modes from Discord instead of requiring an env var change + restart. OpenClaw implements this as `/activation mention|always`. Deferred because the interaction between per-guild settings, env var defaults, and runtime state is more complex than it appears.

## Key Files

| What | Where |
|------|-------|
| Bot implementation | `nymeria/triggers/discord_bot.py` |
| API client | `nymeria/triggers/discord_api_client.py` |
| Entry point | `run.py` → `run_discord_bot()` |
| Docker config | `docker-compose.yml` (profile: `discord`) |
| Env vars | `.env.docker` (`DISCORD_BOT_TOKEN`, `DISCORD_RESPOND_MODE`) |
