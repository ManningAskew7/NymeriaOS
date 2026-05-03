# Telegram Bot

Nymeria's Telegram integration runs as a stateless gateway that translates Telegram bot commands and direct messages into Nymeria REST API calls. All state lives in the API container — the bot is a thin client with no local persistence (except a per-chat tool display toggle held in memory).

## Architecture

```
Docker: nymeria-telegram-bot (profile: telegram)
	  └─ NymeriaTelegramBot
	       ├─ NymeriaAPIClient (async httpx → Nymeria REST API)
	       ├─ python-telegram-bot v22+ (async polling)
	       ├─ 43 bot commands + plain message handler
	       └─ SSE listener (autonomous task stream → chat posts)
```

Like the Discord bot, the Telegram bot communicates exclusively via the REST API. Chat responses are streamed via SSE (`POST /chat`) — users see text appear progressively as the model generates it, with tool call boundaries shown as separate message bubbles.

### Thread & User ID Scheme

| Context | Thread ID | User ID |
|---------|-----------|---------|
| Any chat | `telegram_{chat_id}` | `telegram_{user_id}` |

By default, each Telegram chat (private or group) maps to one
`telegram_{chat_id}` Nymeria thread. A chat can also be explicitly bound to a
desktop-created UUID thread through the desktop wizard, `/bind`, `/switch`, or
`/new`.

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
| `/threads [query]` | List desktop/user-owned threads this Telegram chat can switch to |
| `/switch <title\|number\|id>` | Move this Telegram chat to another existing thread. Title matching is case-insensitive; use `/threads` first to get numbered choices for duplicate titles. |
| `/new [title]` | Create a fresh desktop-style thread and switch this Telegram chat to it. If no title is given, the first chat message can still auto-title the thread. |

You can also send plain text in DMs without any command prefix.

During streamed replies, Telegram surfaces compaction events instead of hiding them: `compacting` sends a short italic status, `compacted` sends a compact "Context compacted" HTML notice with a summary preview, and resumed assistant output continues in normal response bubbles after the notice.

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

### Per-thread chat binding

| Command | Description |
|---------|-------------|
| `/bind <code>` | Attach this Telegram chat to the desktop thread that issued the code. Codes are minted by the desktop "Connect Telegram" wizard (Thread Settings → Chat App → Connect Telegram). Single-use, 10-min TTL. |
| `/threads [query]` | List existing switchable threads. Results are numbered for `/switch 1`, `/switch 2`, etc. Native platform threads like `telegram_<chat_id>` are excluded; use `/unbind` to return to the default Telegram thread. |
| `/switch <title\|number\|id>` | Move this chat's binding to another owned thread by exact title, unique title substring, numbered `/threads` result, or thread ID. Ambiguous title matches return a numbered picker. |
| `/new [title]` | Create a fresh UUID thread owned by the Telegram user's Nymeria account, bind this chat to it, and optionally set the title. |
| `/unbind` | Remove this chat's thread binding. Future messages here revert to the default `telegram_<chat_id>` thread. |
| `/start link_<code>` | Auto-handled when you tap a `t.me/<bot>?start=link_<code>` deep link from the desktop wizard's first step. Self-service alternative to `python run.py users link-platform`. |
| `/start bind_<code>` | Auto-handled when you tap a `t.me/<bot>?start=bind_<code>` deep link. Equivalent to `/bind <code>` in the chat the deep link opens. |

The desktop and mobile **Chat App** tabs (Thread Settings → Chat App) are the
canonical entry point for non-admin users. The flow is fully self-service: the
wizard issues a self-link code if the user hasn't linked their Telegram
identity yet, then issues a per-thread bind code, then polls until the bot has
consumed both; if the user is already linked, it skips directly to the
bind-code polling path. The
bot caches the chat↔thread map in process and refreshes it from the API every
60 seconds, so bindings created from the wizard take effect within that window.
The bot-facing admin binding endpoints are rate-limited per admin/service user
and endpoint; a bad loop gets `429` plus `Retry-After`, while the normal
60-second binding refresh and ~15-second BYO supervisor poll cadence remain
well below the default guard.
The API also reports bound desktop-created threads as `platform: "telegram"` in
`GET /threads` and emits `thread_updated` on bind/unbind, so frontend sidebars
show or clear the Telegram icon without requiring a `telegram_<chat_id>` thread
ID.

The bot-side switch commands use the same binding table as the desktop wizard:
`/switch` moves the current Telegram chat's row to another existing owned
thread, and `/new` first claims a fresh UUID thread for the resolved Nymeria
user before moving the row. The current user is resolved from the Telegram
sender's linked platform identity, matching normal Telegram chat authorization.

Setting `TELEGRAM_BOT_USERNAME=<bot>` (no `@`) in `.env.docker` lets the wizard
produce one-tap `t.me/<bot>?start=...` deep links. With it unset, the wizard
shows the raw `/bind` and `/start` commands the user types manually.

## BYO bots — "Use my own bot" wizard

In addition to the shared bot, users can register their own Telegram bot from
a BotFather token. The Chat App tab has a second button, **Use my own bot**,
that walks through:

1. **Token paste** — the user pastes a token from `@BotFather`. The API
   validates it via Telegram's `getMe`, encrypts it with `NYMERIA_SECRETS_KEY`
   (Fernet symmetric encryption — see `nymeria/core/secrets.py`), and stores
   the ciphertext on `user_telegram_bots`.
2. **Starting** — the supervisor inside the `nymeria-telegram-bot` container
   refreshes its registered-bot list every ~15s. On the next tick it builds
   a fresh `python-telegram-bot` `Application` for the new bot, wires it
   into the same handlers as the shared bot, and starts polling. The wizard
   polls `GET /me/telegram-bots/{id}` until `last_seen_at` becomes non-null,
   then advances.
3. **Bind code** — same as the shared-bot flow except the bot is the
   user's own. The bot's `/bind <code>` handler calls
   `POST /admin/chatapp/bindings/claim-via-bot`, which authorizes by
   matching the bind code's issuing Nymeria user against the bot's
   `owner_user_id`. No `platform_identities` lookup needed — the bot
   itself is the credential.
4. **Done.**

Removing a registered bot (`DELETE /me/telegram-bots/{id}`) cascade-deletes
its bindings; the supervisor stops the polling loop on its next refresh.

### Requirements
- `NYMERIA_SECRETS_KEY` must be set in `.env.docker`. Without it the
  `POST /me/telegram-bots` endpoint returns 503 with instructions on how
  to mint one.
- The supervisor uses one asyncio task and ~25-50 MB RAM per registered
  bot. On the 2 GB VPS this is fine for ~10 bots; if you ever scale higher,
  watch RAM via `docker stats nymeria-telegram-bot`.

### Bot routing
- **Inbound**: each bot's polling sees only chats it's a member of. The
  per-bot binding cache (filtered by `user_telegram_bot_id`) resolves
  `chat_id → thread_id`. Unbound chats on a user-owned bot have no
  fallback (unlike the shared bot's `telegram_<chat_id>` default) — for
  v1 the bot replies "this chat isn't bound to a thread; use `/bind <code>`".
- **Outbound**: the shared bot's SSE listener is the single subscriber.
  Each event's `thread_id` is matched against every bot's
  `_reverse_bindings`; the matching bot's `_handle_sse_event` handles
  delivery, so messages flow through the bot whose token owns the chat.

## Streaming Responses

Chat responses (`/ask` and plain text DMs) are streamed via SSE. Users see text appear progressively as the model generates it, with edits every ~1.5 seconds.

Regular chat delivery splits every finalized response segment before sending it
to Telegram. This covers both normal token-by-token streaming and providers
that emit a whole final answer in one large SSE `response` event; no single
Telegram message should exceed the platform's 4096-character text limit.

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

Telegram has a 4096-character message limit. The bot uses the shared trigger message splitter (`nymeria/triggers/message_splitter.py`) to split long responses intelligently:

1. Preserves code block boundaries (never splits inside ``` ``` ```)
2. Prefers paragraph breaks (`\n\n`)
3. Falls back to line breaks (`\n`), then sentence boundaries (`. `)
4. Hard-splits at 4096 chars only as last resort

### Attachments (images & documents)

The bot accepts photos and document uploads alongside (or instead of) text. Files are downloaded, base64-encoded, and forwarded to the API as the same `attachments` payload the desktop frontend uses, so the same multimodal models work.

- **Images:** `image/jpeg`, `image/png`, `image/gif`, `image/webp` — 10 MB max.
- **Documents:** `application/pdf`, `text/plain`, `text/markdown`, `text/csv` — 20 MB max.
- **Limit:** 4 files per message; extras are dropped with a warning.

Telegram converts photos to JPEG and serves them in multiple resolutions; the bot uses the highest. To preserve original encoding (PNG, etc.), send the image as a *file* / *document* instead of a photo.

The message text comes from `text` if present, otherwise the photo/document `caption`. If neither is set, a short `[attachment]` placeholder is substituted so the API's non-empty-message requirement is satisfied.

Unsupported MIME types and oversized files are rejected with a short reply in the chat — the rest of the message still goes through.

Because Telegram is text-only and can't surface the desktop's "model may not support these attachments" override modal, the bot auto-sets `force_unsupported_attachments=true` whenever attachments are present. If the underlying model can't actually process the file the LLM will say so itself, but the upfront capability check is bypassed (useful with CLIProxy, where multimodal capabilities aren't advertised the way OpenRouter advertises them).

### File Delivery (outbound attachments)

When Nymeria writes a file with `file_write(..., attach=True)`, the bot automatically downloads and sends it in the Telegram chat. This lets the agent deliver reports, CSVs, images, and other artifacts directly to the user's phone.

**How it works:**
1. `file_write(..., attach=True)` only marks files inside `NYMERIA_WORKSPACE_DIR` (default `/workspace`) as deliverable.
2. The API emits a `workspace_artifact` SSE event with the file path, filename, and MIME type. For mixed-version compatibility, the raw tool result still carries an `[attach:/workspace/file.csv]` tag.
3. The bot downloads the file from `GET /workspace/download?path=...` on the API.
4. Images (`image/*` under 10 MB) are sent as inline photos; everything else as downloadable documents.

**Limits:** Files over 50 MB (Telegram bot limit) are silently skipped. Only files within `/workspace/` can be downloaded — the API rejects paths outside the workspace directory.

### HTML Formatting

All bot output uses Telegram's HTML parse mode. The bot converts markdown from the AI model to Telegram HTML:

- `**bold**` → `<b>bold</b>`
- `` `code` `` → `<code>code</code>`
- ` ```code blocks``` ` → `<pre>code</pre>`
- `> blockquote` → `<blockquote>text</blockquote>`

If HTML parsing fails (malformed tags in AI output), the bot falls back to plain text automatically.

## Autonomous Task Delivery

The bot maintains a background SSE connection to `GET /autonomous/stream`. When a watchdog or scheduled TODO fires on a Telegram thread, the bot streams the events into the chat the same way it streams a regular conversation:

- Per-thread state is kept in memory keyed by the Nymeria thread ID, so concurrent autonomous runs in different chats don't interleave.
- `response` chunks accumulate in a buffer and flush as a new message bubble at every `tool_call` boundary.
- `tool_call` / `tool_result` markers are shown only when the chat has `/showtools` enabled.
- `tool_reload` events are produced by the backend same-turn tool reload loop; Telegram just flushes buffered text and posts a compact Tool Binding line before resumed tool calls/results.
- Compaction, attached-context, and iteration-limit events are surfaced as compact status messages.
- A small `Tool calls: N` italic footer is appended to the final bubble when tools were used.
- No wrapper header — bubbles look identical to a regular reply, with the chat itself providing the autonomous-vs-user provenance.
- `task_completed.content` is used only as a fallback when no live `response` chunks were received, so long streamed responses are not duplicated at completion.
- On `task_completed` with `error: true`, a single short `Autonomous task error: ...` line is posted instead.
- Explicit `notify` tool calls arrive as `notification` SSE events and are posted to the bound Telegram chat unless Telegram autonomous delivery is `off`.

TODOs created via `/todo_add` in a Telegram chat have their results delivered back to that chat automatically.

Per-thread delivery is controlled from Thread Settings:

| Mode | Behavior |
|------|----------|
| `full` | Stream autonomous output into Telegram. Default. |
| `notify_only` | Suppress normal autonomous output; send only explicit `notify` events and task errors. |
| `off` | Suppress autonomous Telegram delivery for the thread. |

## Key Files

| What | Where |
|------|-------|
| Bot implementation | `nymeria/triggers/telegram_bot.py` |
| API client (shared) | `nymeria/triggers/api_client.py` |
| Attachment helpers (shared) | `nymeria/triggers/attachment_helpers.py` |
| Entry point | `run.py` → `run_telegram_bot()` |
| Docker config | `docker-compose.yml` (profile: `telegram`) |
| Env vars | `.env.docker` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_DEFAULT_CHAT_ID`) |
| Settings model | `nymeria/config/settings.py` (lines 89-96) |
