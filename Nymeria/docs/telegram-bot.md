# Telegram Bot

Nymeria's Telegram integration runs as a stateless gateway that translates Telegram bot commands and direct messages into Nymeria REST API calls. All state lives in the API container — the bot is a thin client with no local persistence (except a per-chat tool display toggle held in memory).

## Architecture

```
Docker: nymeria-telegram-bot (profile: telegram)
  └─ NymeriaTelegramBot
       ├─ NymeriaAPIClient (async httpx → Nymeria REST API)
       ├─ python-telegram-bot v22+ (async polling)
       ├─ 35 bot commands + plain message handler
       └─ SSE listener (autonomous task completion → chat posts)
```

Like the Discord bot, the Telegram bot communicates exclusively via the REST API. Chat responses are streamed via SSE (`POST /chat`) — users see text appear progressively as the model generates it, with tool call boundaries shown as separate message bubbles.

### Thread & User ID Scheme

| Context | Thread ID | User ID |
|---------|-----------|---------|
| Any chat | `telegram_{chat_id}` | `telegram_{user_id}` |

Each Telegram chat (private or group) maps to one Nymeria thread.

### Telegram vs Discord Differences

| Aspect | Discord | Telegram |
|--------|---------|----------|
| Message limit | 2000 chars | 4096 chars |
| Structured output | Rich embeds | HTML formatting |
| Commands | Slash with subgroups | Flat `/command_name` |
| Typing indicator | `trigger_typing()` (10s) | `send_chat_action` (5s, repeat) |
| First response | `defer()` → `followup.send()` | Send immediately → edit |
| File sending | `discord.File` | `send_document(BytesIO)` |
| Tool boundaries | Separators in single message | Separate chat bubbles |
| Interactive buttons | No | Inline keyboards (stop button) |
| Group handling | @mention or "all" mode | Commands + replies only |

## Setup

### 1. Create a Telegram Bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts to create a bot
3. Copy the bot token (looks like `123456789:ABCdefGHI...`)
4. Optionally set a description and profile picture via BotFather

### 2. Configure Environment

Add to `.env.docker`:

```bash
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_DEFAULT_CHAT_ID=       # Optional: default chat for notifications
```

### 3. Start the Bot

```bash
# Start with other containers
docker compose --profile telegram --env-file .env.docker up -d --build

# Restart after code changes (bind-mounted, no rebuild needed)
docker compose --env-file .env.docker restart telegram-bot

# Logs
docker logs nymeria-telegram-bot --tail 50
```

### 4. Find Your Chat ID

Send a message to the bot, then check the logs:

```bash
docker logs nymeria-telegram-bot --tail 10
# Look for: "telegram_{chat_id}" in the thread ID
```

Or use the `/thread` command — it shows the full thread ID including the chat ID.

## Commands Reference

### Chat

| Command | Description |
|---------|-------------|
| `/ask <message>` | Send a message to Nymeria |
| `/stop` | Abort the current running operation |
| `/clear` | Clear conversation history (preserves notepad and tool config) |
| `/compact` | Compress conversation context to reclaim token space |
| `/thread` | Show thread ID, context usage, token count, compaction count |
| `/context` | Detailed context breakdown: model, tokens, tools, thread overrides |
| `/tasks [status]` | Quick view of scheduled tasks. Filter: active (default), pending, in_progress, done, all |
| `/export [format]` | Export conversation history as a file. Format: markdown (default), json, txt |
| `/restart [bot\|api]` | Restart the Telegram bot (default) or API server |
| `/showtools` | Toggle whether tool calls are shown as separate messages |
| `/help` | List all available commands |
| `/start` | Telegram's default entry point — shows welcome message |

You can also send plain text in DMs without any command prefix.

### Model & Thinking

| Command | Description |
|---------|-------------|
| `/model [name] [scope]` | Show or change the LLM model. Scope: global (default) or thread |
| `/models` | List all available models from the current provider |
| `/think [mode]` | Set thinking mode: off, on, low, medium, high. No argument shows current |
| `/status` | System dashboard: model, context, tools, tasks, uptime |

### TODOs

| Command | Description |
|---------|-------------|
| `/todo_list [status]` | List TODOs. Filter: active (default), pending, in_progress, done, all |
| `/todo_add <task> \| <schedule> \| <repeat> \| <notes>` | Create a TODO. Schedule: "30m", "2h", "1d". Repeat: 5min, hourly, daily, etc. Pipe-delimited. |
| `/todo_complete <id>` | Mark a TODO as done (first 8 chars of ID) |
| `/todo_delete <id>` | Delete a TODO permanently |

### Config

| Command | Description |
|---------|-------------|
| `/config_show` | Show all server settings |
| `/config_get <key>` | Get a specific setting value |
| `/config_set <key> <value>` | Update a setting. Auto-parses booleans, numbers, none |

### Tools

| Command | Description |
|---------|-------------|
| `/tools_core` | List core tools (enabled by default) |
| `/tools_optional` | List optional tool categories with per-chat active counts |
| `/tools_enabled` | Show all tools active in this chat |
| `/tools_category <name>` | List tools in a category with enabled/disabled status |
| `/tools_enable <name>` | Enable a tool or entire category for this chat |
| `/tools_disable <name>` | Disable a tool or entire category for this chat |

### Memory

| Command | Description |
|---------|-------------|
| `/memory_list` | List all saved memories |
| `/memory_save <key> <value>` | Save a persistent memory |
| `/memory_forget <key>` | Remove a saved memory |
| `/memory_search <query>` | Search memories by keyword |

### Notepad

| Command | Description |
|---------|-------------|
| `/notepad_read` | Read this chat's notepad |
| `/notepad_write <content>` | Append to notepad. Use `replace:<content>` to overwrite |
| `/notepad_clear` | Clear this chat's notepad |

## Streaming Responses

Chat responses (`/ask` and plain text DMs) are streamed via SSE. Users see text appear progressively as the model generates it, with edits every ~1.5 seconds.

**Tool call display modes:**

- **Hidden (default):** Pre-tool text and post-tool text are sent as separate message bubbles. Telegram's chat bubble layout provides natural visual separation — no separators needed. Toggle with `/showtools`.
- **Shown:** Tool calls and results appear as separate messages between text segments, formatted with tool name, arguments, and result in HTML.

A **Stop** button (inline keyboard) appears on the first message during streaming. Press it to abort the current operation.

If streaming fails, the bot falls back to the sync `POST /chat/sync` endpoint automatically.

## Message Handling

### In Private Chats (DMs)

The bot responds to all text messages — no command prefix needed. Just type naturally.

### In Groups

Telegram's **privacy mode** (enabled by default for bots) means the bot only receives:
- Messages that are bot commands (`/command`)
- Replies to the bot's own messages

This is handled automatically by Telegram — no configuration needed. To respond to replies, simply reply to any of Nymeria's messages.

### Message Splitting

Telegram has a 4096-character message limit. The bot splits long responses intelligently:

1. Preserves code block boundaries (never splits inside ``` ``` ```)
2. Prefers paragraph breaks (`\n\n`)
3. Falls back to line breaks (`\n`), then sentence boundaries (`. `)
4. Hard-splits at 4096 chars only as last resort

### HTML Formatting

All bot output uses Telegram's HTML parse mode. The bot converts markdown from the AI model to Telegram HTML:

- `**bold**` → `<b>bold</b>`
- `` `code` `` → `<code>code</code>`
- ` ```code blocks``` ` → `<pre>code</pre>`
- `> blockquote` → `<blockquote>text</blockquote>`

If HTML parsing fails (malformed tags in AI output), the bot falls back to plain text automatically.

## Autonomous Task Delivery

The bot maintains a background SSE connection to `GET /autonomous/stream`. When a scheduled TODO completes on a Telegram thread, the bot receives a `task_completed` event and posts the result to the originating chat.

TODOs created via `/todo_add` in a Telegram chat will have their results delivered back to that chat automatically.

## Key Files

| What | Where |
|------|-------|
| Bot implementation | `nymeria/triggers/telegram_bot.py` |
| API client (shared) | `nymeria/triggers/discord_api_client.py` |
| Entry point | `run.py` → `run_telegram_bot()` |
| Docker config | `docker-compose.yml` (profile: `telegram`) |
| Env vars | `.env.docker` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_DEFAULT_CHAT_ID`) |
| Settings model | `nymeria/config/settings.py` (lines 89-96) |
