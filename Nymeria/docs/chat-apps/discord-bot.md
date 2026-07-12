# Discord Bot

Nymeria's Discord integration runs as a stateless gateway that translates Discord slash commands and mentions into Nymeria REST API calls. All state lives in the API container  -  the bot is a thin client with no local persistence (except a per-channel context toggle held in memory).

## Architecture

```
Docker: nymeria-discord-bot (profile: discord)
	 └─ NymeriaDiscordBot(discord.Client)
	      ├─ NymeriaAPIClient (async httpx → Nymeria REST API)
	      ├─ CommandTree (30 slash commands across 6 groups)
	      └─ SSE listener (autonomous task stream → channel posts)
```

The Discord bot communicates exclusively via the REST API. Chat responses are streamed via SSE (`POST /chat`)  -  users see text appear progressively as the model generates it, with tool call boundaries shown as visual separators. This means:

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

Discord slash commands are synced **per-guild** in the `on_ready` hook  -  the bot clears any stale global commands, copies the local command tree to each guild, and calls `tree.sync(guild=guild)`. This means:

- **After adding/removing/renaming slash commands**, you must restart the bot container so `on_ready` fires and pushes the updated tree to Discord.
- Sync is near-instant for guild commands (unlike global commands which can take up to an hour).
- The Discord client may take a few seconds to refresh its autocomplete cache  -  if new commands don't appear immediately, close and reopen the slash command menu.

```bash
# Restart to sync new/changed commands
docker compose --env-file .env.docker restart discord-bot

# Verify sync succeeded in logs
docker logs nymeria-discord-bot --tail 15
# Look for: "Slash commands synced to guild: <name>"
```

## Commands Reference

Discord keeps the slash-command declarations local because Discord needs a
static command tree, but duplicated global command bodies are thin wrappers over
the backend command registry (`POST /commands/execute`). `/help` builds a merged
catalog from backend global commands plus Discord-local commands such as
`/ask`, `/stop`, `/clear`, `/compact`, `/export`, `/show-tools`, and
`/channel-context`.

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
| `/restart [target]` | Restart the Discord bot (default) or API server (`/restart target:api`). Bot restarts close the Discord gateway and API client, then use Docker's restart policy; API restart uses the existing `POST /restart` endpoint. |
| `/help` | List all available commands. |

`/ask` also fetches the last ~10 channel messages as context (configurable, see `/channel-context`).

During streamed replies, Discord now surfaces compaction events instead of hiding them: `compacting` posts a short status line, `compacted` posts a "Context compacted" embed with a summary preview, and any resumed assistant output continues streaming normally after that embed.

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
| `/todos add <task> [schedule] [repeat] [notes]` | Create a TODO. Schedule: any relative duration such as `"45s"`, `"17m"`, `"2h"`, `"1w"`, or an absolute/ISO datetime such as `"2024-12-25 14:00"`. Repeat: `5min` through `monthly`. Associates with the current channel. |
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
| `/tools search <query>` | Discord-local tool search with ranked suggestions and enable hints. |
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
| `/show-tools` | Toggle whether tool calls are shown as embeds in chat. Defaults to hidden  -  only response text is shown, with horizontal rule separators at tool boundaries. When enabled, tool calls appear as blue embeds with arguments and results. |
| `/channel-context` | Toggle whether Nymeria includes recent channel messages as context in `/ask` and @mentions. Defaults to enabled. |

## Streaming Responses

Chat responses (`/ask` and @mentions) are streamed via SSE rather than waiting for the full response. Users see text appear progressively as the model generates it, with edits every ~1.5 seconds.

**Tool call display modes:**

- **Hidden (default):** Response text streams into a single progressively-edited message. Tool call boundaries are shown as horizontal rule separators (─────). Toggle with `/show-tools`.
- **Shown:** Text segments are sent as separate messages with tool call embeds (blue → green on completion) between them. Each embed shows the tool name, arguments, and result.

If streaming fails, the bot falls back to the sync `POST /chat/sync` endpoint automatically.

Both interactive and autonomous SSE flows use the shared `SSEEventHandler` protocol from `triggers/sse_consumer.py`, so new SSE event types only need to be added in one place.

## Autonomous Task Delivery

The bot maintains a background SSE connection to `GET /autonomous/stream`. When a scheduled TODO, watchdog nudge, or trigger runs on a Discord thread, the bot streams the same event types it uses for regular chat into the originating channel:

- `response` chunks are edited into live messages and flushed near Discord's 2000-character limit.
- `tool_call` / `tool_result` markers follow the channel's `/show-tools` setting.
- `tool_reload`, compaction, context summary, iteration-limit, and error events are surfaced inline.
- `workspace_artifact` events are uploaded as Discord attachments, with legacy `[attach:/path]` tags still supported as a fallback.
- `task_completed` ends the stream and only falls back to the aggregate `content` field if no live response chunks were received.

This means TODOs created via `/todos add` in a Discord channel will have their results delivered back to that channel automatically.

## API Client

`NymeriaAPIClient` (`nymeria/triggers/api_client.py`) is a standalone async HTTP client wrapping the Nymeria REST API. It uses `httpx.AsyncClient` with:

- **Chat timeout**: 300s read (accommodates long LLM calls)
- **Default timeout**: 30s read
- **Bearer token auth** via the admin `NYMERIA_SERVICE_TOKEN`, with `X-Nymeria-Act-As: <resolved_user_id>` set per request so each call runs under the linked Nymeria account (see `../accounts.md`).

The client covers all API endpoints: chat, thread management, settings, tools, memories, TODOs, and models. It could be reused by other async integrations.

## Message Handling

### Response Splitting

Discord has a 2000-character message limit. The bot uses the shared trigger message splitter (`nymeria/triggers/message_splitter.py`) to split long responses intelligently:

1. Preserves code block boundaries (never splits inside ` ``` `)
2. Prefers paragraph breaks (`\n\n`)
3. Falls back to line breaks (`\n`), then sentence boundaries (`. `)
4. Hard-splits at 2000 chars only as last resort

### Attachments (images & documents)

The bot accepts message attachments alongside (or instead of) text. Files are downloaded, base64-encoded, and forwarded to the API as the same `attachments` payload the desktop frontend uses.

- **Images:** `image/jpeg`, `image/png`, `image/gif`, `image/webp`  -  10 MB max.
- **Documents:** `application/pdf`, `text/plain`, `text/markdown`, `text/csv`  -  20 MB max.
- **Limit:** 4 files per message; extras are dropped with a warning.

If a message has attachments but no text, a short `[attachment]` placeholder is substituted so the API's non-empty-message requirement is satisfied. Unsupported MIME types and oversized files are rejected with a short reply in the channel  -  the rest of the message still goes through.

**Limitation:** `/ask` does NOT yet accept attachments  -  Discord slash commands need a separate `attachment` option type. Use a regular @mention (with the file attached to the same Discord message) or a DM with the file attached.

Because chat clients can't surface the desktop's "model may not support these attachments" override modal, the bot auto-sets `force_unsupported_attachments=true` whenever attachments are present. If the underlying model can't process the file the LLM will say so itself, but the upfront capability check is bypassed.

### File Delivery (outbound attachments)

When Nymeria writes a workspace file with `file_write(..., attach=True)`, the Discord bot uploads that file back into the channel as a normal Discord attachment. This works for both interactive chats and autonomous task runs.

**How it works:**
1. `file_write(..., attach=True)` only marks files inside `NYMERIA_WORKSPACE_DIR` (default `/workspace`) as deliverable.
2. The API emits a `workspace_artifact` SSE event containing the file path, filename, MIME type, and originating tool call ID.
3. The Discord bot downloads the file from `GET /workspace/download?path=...` and re-uploads it with `discord.File`.
4. For mixed-version safety, the bot still understands legacy `[attach:/path]` tags if it sees an older backend result.

**Limits:** Discord's upload size limits still apply. If the generated file is too large or no longer exists, the bot logs the failure and the rest of the response continues normally.

### Channel Context

When enabled (default), the bot fetches the last ~10 non-bot messages from the channel and prepends them as context:

```
--- Recent channel messages (for context) ---
[14:30] Alice: has anyone seen the deploy logs?
[14:32] Bob: checking now
--- End of channel context ---
```

This helps Nymeria understand the ongoing conversation even when invoked via `/ask` rather than a direct @mention.

## Emoji Reactions

Two-way emoji reactions, opt-in via `DISCORD_REACTION_TRIGGER_ENABLED=true`
(default off). The bot always enables the `reactions` gateway intent, so
flipping the toggle needs only a bot restart, not a Discord-side change.

### Inbound: reactions fire the agent

When a user adds any emoji reaction to a message **the bot itself authored**
(`on_raw_reaction_add`), the bot fires an agent turn in the channel's mapped
thread with a synthetic prompt:

```
[Reaction] Alice reacted with 👍 to your message: "Sure, I have rescheduled the ..."
```

Custom Discord emojis are rendered as `:name:`. The quoted excerpt is the
reacted message's text, whitespace-collapsed and capped at 200 characters.

Guards, in order: toggle off → drop; reactor is the bot itself or any bot →
drop (loop guard); reacted message not authored by the bot → drop (checked
first against the raw payload's `message_author_id`, so no message fetch is
spent on other people's messages, then re-checked on the fetched message);
repeat of the same (message, reactor, emoji) within 45 seconds → drop
(debounce, so emoji toggling cannot fire repeated turns); reactor has no
linked Nymeria account → silent drop (same access model as messages).
Reaction **removals** never fire. The turn runs with `is_self_invoke=true`,
`trigger_override="reaction"`, `source="trigger"`, and a `source_label` like
`reaction 👍`, and streams into the channel exactly like an @mention response.

### Outbound: the `react` tool

The synthetic prompt tells the agent how to react back. `react` is a catalog
tool (not bound by default); when it is unbound in the thread the prompt
includes its compact args schema plus a `tool_invoke` recipe, so the agent can
call it immediately without a graph rebuild:

- `react(emoji="👍")` posts the emoji onto the message that started the turn
  (the reacted-to message for reaction turns, the user's message for normal
  ones). `message_id` overrides the target within the same channel.
- `react(emoji="👍", suppress_reply=true)` additionally hides the turn's text
  reply in Discord, for emoji-only acknowledgements.

Delivery: the tool publishes a `reaction_request` event on the autonomous bus;
the bot's firehose listener matches `platform == "discord"`, fetches the
channel and message, and calls `message.add_reaction(...)`. Failures (deleted
message, missing permission, unknown emoji) are logged and never break the
turn.

Reply suppression is deterministic, not model-inferred: the react call sets
a per-turn backend flag and its result carries the marker
`[nymeria:reply_suppressed]`; the stream emits a `reply_suppressed` event
right after the `tool_result` only when the backend flag confirms the real
react call (marker text echoed by any other tool's output is inert), and the
bot then drops
buffered text, stops sending chunks, and skips the sync-fallback and
`task_completed` content fallbacks for that turn. Tool-call embeds (when
`/show-tools` is on) still render.

## Future Improvements

- **`/activation` command**  -  Toggle between `mention` and `all` respond modes from Discord instead of requiring an env var change + restart. Deferred because the interaction between per-guild settings, env var defaults, and runtime state is more complex than it appears.

## Key Files

| What | Where |
|------|-------|
| Bot implementation | `nymeria/triggers/discord_bot.py` |
| Slash command Cogs | `nymeria/triggers/discord_cogs/` (chat, todos, config, tools, memory, info) |
| API client | `nymeria/triggers/api_client.py` |
| Attachment helpers (shared) | `nymeria/triggers/attachment_helpers.py` |
| Entry point | `run.py` → `run_discord_bot()` |
| Docker config | `docker-compose.yml` (profile: `discord`) |
| Env vars | `.env.docker` (`DISCORD_BOT_TOKEN`, `DISCORD_RESPOND_MODE`, `DISCORD_REACTION_TRIGGER_ENABLED`) |
