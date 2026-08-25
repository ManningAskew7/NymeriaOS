# Telegram Bot

Nymeria's Telegram integration runs as a stateless gateway that translates Telegram bot commands and direct messages into Nymeria REST API calls. All state lives in the API container  -  the bot is a thin client with no local persistence (except a per-chat tool display toggle held in memory).

## Architecture

```
Docker: nymeria-telegram-bot (profile: telegram)
	  └─ NymeriaTelegramBot
	       ├─ NymeriaAPIClient (async httpx → Nymeria REST API)
	       ├─ python-telegram-bot v22+ (async polling)
	       ├─ 47 native bot commands + catch-all + plain message handler
	       └─ SSE listener (autonomous task stream → chat posts)
```

Like the Discord bot, the Telegram bot communicates exclusively via the REST API. Chat responses are streamed via SSE (`POST /chat`)  -  users see text appear progressively as the model generates it, with tool call boundaries shown as separate message bubbles.

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

Or use the `/thread` command  -  it shows the full thread ID including the chat ID.

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
| `/tasks [status]` | Retired spelling of `/todo_list`, still registered and still working |
| `/export [format]` | Export conversation history as a file. Format: markdown (default), json, txt |
| `/restart [bot\|api]` | Restart the Telegram bot (default) or API server. Bot restarts stop polling and close Nymeria-side resources before the container exits. |
| `/showtools` | Toggle whether tool calls are shown as separate messages |
| `/help` | List all available commands |
| `/start` | Telegram's default entry point  -  shows welcome message |
| `/threads [query]` | List desktop/user-owned threads this Telegram chat can switch to |
| `/switch <title\|number\|id>` | Move this Telegram chat to another existing thread. Title matching is case-insensitive; use `/threads` first to get numbered choices for duplicate titles. |
| `/new [title]` | Create a fresh desktop-style thread and switch this Telegram chat to it. If no title is given, the first chat message can still auto-title the thread. |

You can also send plain text in DMs without any command prefix.

Most global command families are thin adapters over the backend command
registry. Telegram's flat commands (`/tools_core`, `/memory_save`,
`/todo_add`, `/notepad_write`, `/settings`, etc.) are translated to
canonical backend paths such as `/tools list core`, `/memory save`,
`/todos add`, `/notepad write`, `/settings`, and executed through
`POST /commands/execute`. Telegram-only
commands such as `/bind`, `/switch`, `/new`, `/showtools`, and chat-control
commands remain local to the bot.

### Renamed on 2026-08-03 (backlog #131), and still typeable

The command vocabulary migration renamed a number of backend commands. Unlike
Discord, **nothing you already type stops working on Telegram**: every retired
spelling stays registered as a whole-path alias, so `/config_show`,
`/memory_forget`, `/thread_info`, `/artifacts_recent`, `/hook_log`,
`/mcp_remove`, `/skills_inspect`, `/tasks`, `/branch` and friends all still
resolve. What changed here is which name the bot itself SPEAKS and which name
the "/" menu advertises:

| Retired backend path | Canonical now | Telegram menu name |
|----------------------|---------------|--------------------|
| `config show` | `settings` | `/settings` |
| `config get` / `config set` | `settings get` / `settings set` | `/settings_get`, `/settings_set` |
| `models` | `model list` | `/models` (unchanged) |
| `tasks` | `todos list` | `/todo_list` |
| `tools core\|optional\|enabled\|category` | `tools list [filter]` | `/tools_list` |
| `memory forget` | `memory delete` | `/memory_forget` (unchanged) |
| `account current` | `account show` | `/account_current` (unchanged) |
| `artifacts recent` | `artifacts list` | `/artifacts_recent` (unchanged) |
| `hook log` | `hook history` | `/hook_log` (unchanged) |
| `mcp remove` | `mcp delete` | `/mcp_remove` (unchanged) |
| `thread info` / `thread new` | `thread show` / `thread create` | `/thread_info`, `/thread_new` (unchanged) |
| `skills inspect` | `skills show` | `/skills_show` |
| `skills off all` | `skills disable all` | `/skills_disable all` |

The "unchanged" rows are deliberate: the menu name is a command's first
single-token alias, and the migration kept the old alias first everywhere so no
menu entry was silently renamed. The three `/config_*` entries are the
exception, because their whole family folded into `settings`.

Disabling every active skill at once used to be its own command
(`/skills_off_all`). That flat name is gone; the replacement is
`/skills_disable all` (or `/skills off all`), which deactivates every skill
active on this chat's thread.

Every command WITHOUT a native handler is caught by a generic passthrough
(registered last, 2026-08-02) and forwarded verbatim to the backend command
service, so the full catalog works from Telegram: `/provider list`,
`/skills`, `/doctor`, `/thread list`, and every future registered command.
A typo answers with the backend's "Unknown command ... Did you mean ...?"
copy instead of Telegram's old silent drop, and commands that execute as
chat turns (`/skill`, `/quick`, ...) fall through into the normal chat
stream. In group chats the passthrough only claims commands explicitly
addressed to the bot (`/cmd@botname`) or sent as a reply to it, so other
bots' commands in a shared chat stay unanswered; DMs need no addressing.
Per-surface and admin gating apply (`surface="telegram"`); the two
secret-typing flows (`/provider setup`, `/provider cliproxy`) are refused
here with an explanation, since anything typed in chat persists in platform
history.

The Telegram command menu and `/help` output are generated from the merged
catalog of backend global commands plus Telegram-local commands. Backend grouped
paths are displayed as Telegram-safe flat command names, for example
`/tools_core` and `/todo_add`, without duplicating their canonical backend rows.
Telegram caps the menu at 100 entries, so the menu can omit some commands;
they still execute via the passthrough.

During streamed replies, Telegram surfaces compaction events instead of hiding them: `compacting` sends a short italic status, `compacted` sends a compact "Context compacted" HTML notice with a summary preview, and resumed assistant output continues in normal response bubbles after the notice.

### Model & Thinking

| Command | Description |
|---------|-------------|
| `/model [name] [scope]` | Show or change the LLM model. Scope: global (default) or thread |
| `/models` | List all available models from the current provider (relays `/model list`) |
| `/think [mode]` | Set thinking mode: off, on, low, medium, high. Admin only on Telegram |
| `/status` | System dashboard: model, context, tools, tasks, uptime |

### TODOs

| Command | Description |
|---------|-------------|
| `/todo_list [status] [--thread current\|<id>]` | List TODOs. Filter: active (default), pending, in_progress, done, all |
| `/todo_add <task> \| <schedule> \| <repeat> \| <notes>` | Create a TODO. Schedule: any relative duration such as "45s", "17m", "2h", "1w", an absolute/ISO datetime, or `none` for a plain checklist item. Repeat: any recurrence interval (daily, 90m, weekly, ...). The pipe form and the flag form (`--schedule 2h --repeat daily --notes ...`) both work (#143). |
| `/todo_edit <id> [new task] [--status ...] [--notes ...] [--schedule ...\|--clear-schedule] [--repeat ...\|--clear-repeat] [--thread ...]` | Edit any field of a TODO (unique id prefix works) |
| `/todo_schedule <id> <when\|clear>` | Set or clear a TODO's fire time |
| `/todo_repeat <id> <interval\|clear>` | Set or clear a TODO's recurrence |
| `/todo_complete <id>` | Mark a TODO as done (unique id prefix works) |
| `/todo_delete <id>` | Delete a TODO permanently (unique id prefix works) |

### Settings and environment

All five are admin-only on Telegram. The bot is deliberately stricter than the
backend for global controls (the backend lets any linked user READ a setting):
the pre-gate in `TELEGRAM_COMMAND_ACCESS` runs before the handler and covers
exactly the names below, plus `/think` and `/restart`. Typing an old alias such
as `/config_show` instead reaches the backend through the catch-all, where the
backend's own gate applies.

| Command | Description |
|---------|-------------|
| `/settings` | Show all server settings (was `/config_show`) |
| `/settings_get <key>` | Get a specific setting value (was `/config_get`) |
| `/settings_set <key> <value>` | Update a setting. Auto-parses booleans, numbers, none (was `/config_set`) |
| `/env_show` | List environment variables by category, secrets masked |
| `/env_set <key> <value>` | Write one environment variable |

Unmasked environment reads are not offered here: there is no `/env_get`
handler, because an unmasked secret typed back into a chat persists in
platform history.

### Aliases

| Command | Description |
|---------|-------------|
| `/aliases` | List your personal command aliases with author and health flags |
| `/alias_create <name> <command...>` | Create an alias that expands to a full command, values included (e.g. `/alias_create gpt5 model openai/gpt-5.5`) |
| `/alias_delete <name>` | Delete one of your aliases |

Aliases are per-user and expand server-side at dispatch, so a spelling
created anywhere works here through the passthrough: type `/gpt5` and the
backend runs `/model openai/gpt-5.5`. An alias always loses to a real
command spelling, and the agent may author aliases for you (the listing
names the author of each).

### Tools

| Command | Description |
|---------|-------------|
| `/tools_list [filter]` | List tools: `enabled` (default), `optional`, `core`, or one category name |
| `/tools_core` | List core tools (enabled by default) |
| `/tools_optional` | List optional tool categories with per-chat active counts |
| `/tools_enabled` | Show all tools active in this chat |
| `/tools_category <name>` | List tools in a category with enabled/disabled status |
| `/tools_search <query>` | Ranked tool search with enable hints (bot-local rendering) |
| `/tools_enable <name>` | Enable a tool or entire category for this chat |
| `/tools_disable <name>` | Disable a tool or entire category for this chat |

The four filter spellings folded into `tools list [filter]` in the backend, but
they stay registered here and each relays its filter VALUE (`/tools list core`).
Since backlog #133 the backend can do the same on its own: `tools core` and
`tools optional` are INJECTED aliases whose expansion carries the filter
token, so a spaced `/tools core` reaching the backend directly renders the
core view again on every surface. These flat relays are equivalent and
unaffected.

### Memory

| Command | Description |
|---------|-------------|
| `/memory_list` | List all saved memories |
| `/memory_save <key> <value>` | Save a persistent memory |
| `/memory_forget <key>` | Remove a saved memory (relays the canonical `/memory delete`) |
| `/memory_search <query>` | Search memories by keyword |

### Automation

Both are single-token passthroughs: the subcommand and its flags ride as
arguments and the backend's longest-prefix path match routes them, so the whole
flag grammar is authorable from a Telegram DM.

| Command | Description |
|---------|-------------|
| `/hook [list\|create\|show\|edit\|enable\|disable\|delete\|test\|history ...]` | Author and manage lifecycle hooks. `/hook history` was `/hook log` |
| `/fallback [status\|revert\|approvals\|approve\|deny ...]` | LLM fallback chain, consent prompts, and active swaps |

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
| `/start link_<code>` | Auto-handled when you tap a `t.me/<bot>?start=link_<code>` deep link from the desktop wizard's first step. Self-service alternative to `python3 run.py users link-platform`. |
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

## BYO bots  -  "Use my own bot" wizard

In addition to the shared bot, users can register their own Telegram bot from
a BotFather token. The Chat App tab has a second button, **Use my own bot**,
that walks through:

1. **Token paste**  -  the user pastes a token from `@BotFather`. The API
   validates it via Telegram's `getMe`, encrypts it with `NYMERIA_SECRETS_KEY`
   (Fernet symmetric encryption  -  see `nymeria/core/secrets.py`), and stores
   the ciphertext on `user_telegram_bots`.
2. **Starting**  -  the supervisor inside the `nymeria-telegram-bot` container
   refreshes its registered-bot list every ~15s. On the next tick it builds
   a fresh `python-telegram-bot` `Application` for the new bot, wires it
   into the same handlers as the shared bot, and starts polling. The wizard
   polls `GET /me/telegram-bots/{id}` until `last_seen_at` becomes non-null,
   then advances.
3. **Bind code**  -  same as the shared-bot flow except the bot is the
   user's own. The bot's `/bind <code>` handler calls
   `POST /admin/chatapp/bindings/claim-via-bot`, which authorizes by
   matching the bind code's issuing Nymeria user against the bot's
   `owner_user_id`. No `platform_identities` lookup needed  -  the bot
   itself is the credential.
4. **Done.**

Removing a registered bot (`DELETE /me/telegram-bots/{id}`) cascade-deletes
its bindings; the supervisor stops the polling loop on its next refresh.

### Requirements
- `NYMERIA_SECRETS_KEY` must be set in `.env.docker`. Without it the
  `POST /me/telegram-bots` endpoint returns 503 with instructions on how
  to mint one.
- The supervisor uses one asyncio task and ~25-50 MB RAM per registered
  bot. On a small VPS this is fine for ~10 bots; if you ever scale higher,
  watch RAM via `docker stats nymeria-telegram-bot`.

### Bot routing
- **Inbound**: each bot's polling sees only chats it's a member of. The
  per-bot binding cache (filtered by `user_telegram_bot_id`) resolves
  `chat_id → thread_id`. Unbound chats on a user-owned bot have no
  fallback (unlike the shared bot's `telegram_<chat_id>` default)  -  for
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

- **Hidden (default):** Pre-tool text and post-tool text are sent as separate message bubbles. Telegram's chat bubble layout provides natural visual separation  -  no separators needed. Toggle with `/showtools`.
- **Shown:** Tool calls and results appear as separate messages between text segments, formatted with tool name, arguments, and result in HTML.

A **Stop** button (inline keyboard) appears on the first message during streaming. Press it to abort the current operation.
Stop buttons are backed by short-lived opaque tokens and can only be used from
the same Telegram chat by the linked Nymeria user who started that stream.

If streaming fails, the bot falls back to the sync `POST /chat/sync` endpoint automatically.

## Message Handling

### In Private Chats (DMs)

The bot responds to all text messages  -  no command prefix needed. Just type naturally.

To route a single turn to another owned Nymeria thread without switching the
current Telegram binding, prefix the message with a thread mention:

```text
@Research summarize the latest notes
@"Thread With Spaces" draft the reply
@abc1234 continue this task
```

The backend resolves the prefix against the linked user's visible thread
titles, callable names, and ID prefixes. Telegram preserves the prefix and
streams the response back into the current chat with a `Response from <thread>`
reference line. The target thread owns the saved conversation history for that
turn.

### In Groups

Telegram's **privacy mode** (enabled by default for bots) means the bot only receives:
- Messages that are bot commands (`/command`)
- Replies to the bot's own messages

This is handled automatically by Telegram  -  no configuration needed. To respond to replies, simply reply to any of Nymeria's messages.

### Message Splitting

Telegram has a 4096-character message limit. The bot uses the shared trigger message splitter (`nymeria/triggers/message_splitter.py`) to split long responses intelligently:

1. Preserves code block boundaries (never splits inside ``` ``` ```)
2. Prefers paragraph breaks (`\n\n`)
3. Falls back to line breaks (`\n`), then sentence boundaries (`. `)
4. Hard-splits at 4096 chars only as last resort

### Attachments (images & documents)

The bot accepts photos and document uploads alongside (or instead of) text. Files are downloaded, base64-encoded, and forwarded to the API as the same `attachments` payload the desktop frontend uses, so the same multimodal models work.

- **Images:** `image/jpeg`, `image/png`, `image/gif`, `image/webp`  -  10 MB max.
- **Documents:** `application/pdf`, `text/plain`, `text/markdown`, `text/csv`  -  20 MB max.
- **Limit:** 4 files per message; extras are dropped with a warning.

Telegram converts photos to JPEG and serves them in multiple resolutions; the bot uses the highest. To preserve original encoding (PNG, etc.), send the image as a *file* / *document* instead of a photo.

The message text comes from `text` if present, otherwise the photo/document `caption`. If neither is set, a short `[attachment]` placeholder is substituted so the API's non-empty-message requirement is satisfied.

Unsupported MIME types and oversized files are rejected with a short reply in the chat  -  the rest of the message still goes through.

Because Telegram is text-only and can't surface the desktop's "model may not support these attachments" override modal, the bot auto-sets `force_unsupported_attachments=true` whenever attachments are present. If the underlying model can't actually process the file the LLM will say so itself, but the upfront capability check is bypassed (useful with CLIProxy, where multimodal capabilities aren't advertised the way OpenRouter advertises them).

### File Delivery (outbound attachments)

When Nymeria writes a file with `file_write(..., attach=True)`, the bot automatically downloads and sends it in the Telegram chat. This lets the agent deliver reports, CSVs, images, and other artifacts directly to the user's phone.

**How it works:**
1. `file_write(..., attach=True)` only marks files inside `NYMERIA_WORKSPACE_DIR` (default `/workspace`) as deliverable.
2. The API emits a `workspace_artifact` SSE event with the file path, filename, and MIME type. For mixed-version compatibility, the raw tool result still carries an `[attach:/workspace/file.csv]` tag.
3. The bot downloads the file from `GET /workspace/download?path=...` on the API.
4. Images (`image/*` under 10 MB) are sent as inline photos; everything else as downloadable documents.

**Limits:** Files over 50 MB (Telegram bot limit) are silently skipped. Only files within `/workspace/` can be downloaded  -  the API rejects paths outside the workspace directory.

### Voice messages (voice-in, voice-out)

Voice notes and audio files are transcribed through the backend's STT
(`POST /voice/stt`) and fed into the normal chat pipeline as the message text
(prepended with the caption when both exist), so thread history reads
naturally. The agent is told via a trigger note that the message was spoken
and that its reply will be spoken too.

After the streamed text reply finishes, the bot synthesizes the response
(`POST /voice/tts` with `voice_note: true`, markdown flattened, capped at
4000 chars) and sends it with `send_voice`; Ogg/Opus and MP3 render as round
voice bubbles. If the recipient blocks voice messages or the container is not
voice-compatible, it falls back to `send_audio`. Voice replies are
best-effort: a missing TTS provider (HTTP 503) or any send failure never
degrades the already-delivered text reply.

Requirements and limits: `STT_PROVIDER` must be configured on the backend (the
bot replies with a setup hint when it is not); `TTS_PROVIDER` is optional and
only gates the spoken reply. Audio over 20 MB (the Bot API download cap) is
rejected before download. Other platform bots do not handle voice yet.

### HTML Formatting

All bot output uses Telegram's HTML parse mode. The bot converts markdown from the AI model to Telegram HTML:

- `**bold**` → `<b>bold</b>`
- `` `code` `` → `<code>code</code>`
- ` ```code blocks``` ` → `<pre>code</pre>`
- `> blockquote` → `<blockquote>text</blockquote>`

If HTML parsing fails (malformed tags in AI output), the bot falls back to plain text automatically.

## Autonomous Task Delivery

The bot maintains a background SSE connection to `GET /autonomous/stream`. When a watchdog or scheduled TODO fires on a Telegram thread, the bot streams the events into the chat the same way it streams a regular conversation:

- Delivery is attach-preferred: on `task_started` the bot opens the per-thread turn stream (`GET /threads/{thread_id}/turn/stream`) and renders the turn from that canonical buffer, ignoring the firehose transcript copies while attached. Turns that are not attachable (for example non-buffered headless workflow runs) fall back to rendering the firehose events exactly as before.
- Fanout-mirrored events (`fanout: true`, a queued prompt's re-publication of a busy holder turn) are dropped wholesale so one turn is never delivered twice.
- Per-thread state is kept in memory keyed by the Nymeria thread ID, so concurrent autonomous runs in different chats don't interleave.
- `response` chunks accumulate in a buffer and flush as a new message bubble at every `tool_call` boundary.
- `tool_call` / `tool_result` markers are shown only when the chat has `/showtools` enabled.
- `tool_reload` events are produced by the backend same-turn tool reload loop; Telegram just flushes buffered text and posts a compact Tool Binding line before resumed tool calls/results.
- Compaction, attached-context, and iteration-limit events are surfaced as compact status messages.
- A small `Tool calls: N` italic footer is appended to the final bubble when tools were used.
- No wrapper header  -  bubbles look identical to a regular reply, with the chat itself providing the autonomous-vs-user provenance.
- `task_completed.content` is used only as a fallback when no live `response` chunks were received, so long streamed responses are not duplicated at completion.
- On `task_completed` with `error: true`, a single short `Autonomous task error: ...` line is posted instead.
- Explicit `notify` tool calls arrive as `notification` SSE events and are posted to the bound Telegram chat unless Telegram autonomous delivery is `off`.

TODOs created via `/todo_add` in a Telegram chat have their results delivered back to that chat automatically.

### Delivery honesty and failure accounting

Send failures are counted, not just warned about (backlog #247). The
completion log line says what actually happened: delivered, partially
delivered (some bubbles failed), or FAILED (nothing reached the chat), with
the first error. "Chat not found" and blocked-bot errors carry actionable
copy: the recipient has never started the bot (or blocked it), and Telegram
refuses bot-initiated first contact, so retrying cannot help until they act.

For TODO-driven turns the bot also reports the outcome to
`POST /todos/{todo_id}/delivery-report` (see `api.md`): consecutive
undelivered runs alert the owner and eventually auto-pause the schedule,
exactly like the execution-failure policy, so a scheduled message can no
longer fail silently forever. Turns that errored backend-side are excluded
(they already feed the execution-failure accounting).

### Dropped-stream recovery (interactive)

A mid-turn connection drop on the interactive chat stream no longer
re-sends the prompt (which ran the turn a second time). The shared recovery
consumer (`triggers/sse_consumer.py::consume_chat_stream_with_recovery`)
re-attaches to `GET /threads/{thread_id}/turn/stream` from the last seen
`seq` and delivers the tail exactly once; if the turn is genuinely gone it
posts an honest "lost connection" notice instead. The legacy sync fallback
now runs only when the failure happened before the turn started, and it
skips self-invoke (reaction) turns entirely: those carry no wire turn
identity, so re-sending would run the reaction twice; the bot logs and
stops instead.

Per-thread delivery is controlled from Thread Settings:

| Mode | Behavior |
|------|----------|
| `full` | Stream autonomous output into Telegram. Default. |
| `notify_only` | Suppress normal autonomous output; send only explicit `notify` events and task errors. |
| `off` | Suppress autonomous Telegram delivery for the thread. |

## Model-Swap Consent (LLM Fallback)

Telegram is a park-capable platform for the LLM fallback-consent layer:
because the bot streams turns over SSE (with keepalives), a Telegram-origin
turn in `ask` mode parks exactly like a GUI turn instead of auto-swapping.

- A `fallback_prompt` event posts a consent prompt with inline buttons:
  **Swap** labeled with the prompt's default hold (e.g. **Swap (2h)**; a 0
  default renders plain **Swap** and applies to this turn only), **Swap
  until reverted** (when permanent holds are allowed), and **Don't swap**.
  The message body is the
  shared text prompt, so `/fallback approve <id> [minutes|permanent]` and
  `/fallback deny <id>` always work even if the buttons fail. No answer by
  the deadline auto-swaps (a fallback is a resilience action).
- Buttons carry opaque tokens (Telegram callback data is client-visible and
  capped at 64 bytes); the backend authorizes the clicker (owner-or-admin),
  so a 404 answer means "not yours to resolve" and a 409 means "no longer
  pending".
- `fallback_prompt_resolved` is the single edit path for the prompt message
  from every resolve surface (buttons, commands, REST, desktop, timeout,
  abort). When the thread ends up ON the fallback (approved or timeout),
  the edited message keeps an inline **Revert** button that clears the hold
  via the standard thread-config PATCH as the clicker.
- Applied swaps (`provider_fallback`, any consent mode) post a short notice
  with the same Revert button; unlike the consent prompt, the notice
  respects the autonomous delivery-mode gate. The consent prompt pair
  bypasses `full`/`notify_only`/`off` (muting a thread must not silently
  cost the user their say).
- `/fallback` (status, revert, approvals, approve, deny) is forwarded to the
  backend command service like `/hook`.

## Emoji Reactions

Two-way emoji reactions, opt-in via `TELEGRAM_REACTION_TRIGGER_ENABLED=true`
(default off). Reaction updates already arrive on the bot's `Update.ALL_TYPES`
subscription; the toggle only gates handling, so flipping it needs just a bot
restart.

### Inbound: reactions fire the agent

In **private chats only**, when a user adds an emoji reaction to a message in
the chat (`MessageReactionHandler`), the bot fires an agent turn in the chat's
mapped thread with a synthetic prompt:

```
[Reaction] Alice reacted with 👍 to one of your recent messages in this chat.
```

The private-chat scope is a platform constraint turned into a guard: Telegram
reaction updates carry the chat, message id, reactor, and old/new reaction
lists, but NOT the reacted message's author, and bots cannot fetch messages by
id. In a DM with the bot, a human's reaction is overwhelmingly about a bot
message, so the own-message gate Discord enforces is approximated by scope.
Custom (paid) emoji reactions are described as "a custom emoji".

Guards, in order: toggle off → drop; non-private chat → drop; reactor missing,
a bot, or the bot itself → drop (loop guard); no newly **added** emoji in the
old-to-new reaction delta (i.e. a removal) → drop; repeat of the same
(message, reactor, emoji) within 45 seconds → drop (debounce, so toggling an
emoji off and on cannot fire repeated turns); reactor has no linked
Nymeria account → silent drop. The turn runs with `is_self_invoke=true`,
`trigger_override="reaction"`, `source="trigger"`, and a `source_label` like
`reaction 👍`, and streams into the chat like a normal reply.

### Outbound: the `react` tool

The synthetic prompt tells the agent how to react back. `react` is a catalog
tool (not bound by default); when it is unbound in the thread the prompt
includes its compact args schema plus a `tool_invoke` recipe, so the agent can
call it immediately without a graph rebuild:

- `react(emoji="👍")` posts the reaction onto the message that started the
  turn (the reacted-to message for reaction turns, the user's message for
  normal ones). `message_id` overrides the target within the same chat.
- `react(emoji="👍", suppress_reply=true)` additionally hides the turn's text
  reply, for emoji-only acknowledgements.

Delivery: the tool publishes a `reaction_request` event on the autonomous bus;
the bot's firehose listener matches `platform == "telegram"`, validates the
emoji against Telegram's standard reaction set
(`telegram.constants.ReactionEmoji`; bots cannot use arbitrary emojis), and
calls `set_message_reaction(...)`. A non-standard emoji or API failure is
logged and never breaks the turn.

Reply suppression is deterministic, not model-inferred: the react call sets
a per-turn backend flag and its result carries the marker
`[nymeria:reply_suppressed]`; the stream emits a `reply_suppressed` event
right after the `tool_result` only when the backend flag confirms the real
react call (marker text echoed by any other tool's output is inert), and the
bot then drops
buffered text, stops sending chunks, removes the Stop button, and skips the
sync-fallback, voice-reply, and `task_completed` content fallbacks for that
turn. Tool-call messages (when `/showtools` is on) still render.

## Key Files

| What | Where |
|------|-------|
| Bot implementation | `nymeria/triggers/telegram_bot.py` |
| API client (shared) | `nymeria/triggers/api_client.py` |
| Attachment helpers (shared) | `nymeria/triggers/attachment_helpers.py` |
| Entry point | `run.py` → `run_telegram_bot()` |
| Docker config | `docker-compose.yml` (profile: `telegram`) |
| Env vars | `.env.docker` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_DEFAULT_CHAT_ID`, `TELEGRAM_REACTION_TRIGGER_ENABLED`) |
| Settings model | `nymeria/config/settings.py` |
