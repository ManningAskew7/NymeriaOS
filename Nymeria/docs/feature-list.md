# Nymeria — Complete Feature List

Comprehensive feature inventory for comparison with other AI agent platforms. Last updated: 2026-04-23.

---

## 1. Callable Threads (Agent-to-Agent Orchestration)

The core differentiating feature. Any conversation thread can be made "callable", turning it into a tool that other threads can invoke. This enables stateful, multi-agent orchestration where each agent maintains its own persistent conversation history, knowledge, tools, and personality.

### How It Works
- **Any thread becomes a tool** — Set `callable=True` on any thread, and it appears as `thread_name(task: str) → str` in other threads' tool lists
- **Fully stateful** — Each callable thread has its own persistent conversation history (SQLite/PostgreSQL checkpoints). When invoked again, it remembers everything from previous calls
- **Independent configuration** — Each thread has its own system prompt, tools, LLM model, skills, and iteration limits
- **Hierarchical invocation** — Threads can call other threads, forming multi-level agent hierarchies (max depth 3)
- **Circular call prevention** — BFS ancestry check prevents deadlocks (thread A → B → A blocked)

### Per-Thread Configuration
- **System prompt**: Append instructions to base prompt (5000 chars) OR replace entirely (50,000 chars)
- **Tool set**: Enable/disable any tools per thread; each agent has only what it needs
- **LLM model**: Different threads can use different models and providers (e.g., Opus for reasoning, Haiku for fast tasks)
- **Extended thinking**: Per-thread reasoning effort (off, on, low, medium, high)
- **Skills**: Per-thread skill enablement/disablement
- **Turn safety**: 500 main-agent tool calls, 300 default callable tool calls, configurable callables up to 1000, plus repeated tool/result loop detection
- **Profile injection**: Toggle whether user memories and TODOs appear in this thread's prompt

### Thread Spawning
- Threads can dynamically create new callable threads via `spawn_thread` tool
- Created with full config: title, instructions, tools, LLM model, skills, initial message
- Rate-limited: 10 spawns/hour per parent, max depth 3
- Initial message support: spawner blocks until child responds
- Only the spawning parent can delete spawned threads

### Learning & Improvement
Each thread improves as it's used through two knowledge systems:
- **Notepad** (thread-local) — Persistent markdown scratchpad (50KB) that survives context compaction. The agent writes findings, decisions, project state, and strategy. Re-injected into the system prompt after every compaction. Acts as the thread's isolated, evolving knowledge base.
- **Profile** (shared) — Key-value store shared across ALL threads (100 entries, 1000 chars each). Contains universal facts about the user: name, preferences, API keys, communication style. Every thread sees this context.

### Concurrency & Safety
- **Per-thread locking** — `ThreadLockManager` prevents concurrent access to the same thread
- **Cascading abort** — Aborting a parent recursively aborts all active child threads
- **Callback isolation** — Child thread LLM tokens don't leak into parent's event stream
- **Parent-child tracking** — Active invocations registered for abort cascade and hierarchy queries

---

## 2. Persistent Memory & Learning

Nymeria threads don't just respond — they learn. Three interconnected systems ensure nothing is forgotten and every thread improves with use.

### Profile (Shared Knowledge Base)
- **Scope**: Global — shared across ALL threads for a user
- **Purpose**: Universal facts (name, role, preferences, API keys, communication style)
- **Auto-injected**: Into every thread's system prompt (configurable per-thread)
- **Tools**: `memory_add(scope="global", ...)`, `memory_edit(scope="global", ...)`, `memory_read(scope="global", ...)`, `personality_set`
- **Limits**: 100 memories, 1000 chars each

### Notepad (Per-Thread Knowledge Base)
- **Scope**: Thread-local — isolated to one conversation
- **Purpose**: Thread-specific state: project context, decisions, findings, strategy, file paths
- **Survives compaction**: Automatically re-injected after context summarization
- **Tools**: `memory_add(scope="thread", ...)`, `memory_edit(scope="thread", ...)`, `memory_read(scope="thread", ...)` (same unified verbs as profile, just `scope="thread"`)
- **Limit**: 50KB per thread
- **Used by autonomous tasks**: Ticker reads notepad for context continuity

### RAG (Nothing Gets Forgotten)
- **Every conversation turn is indexed** — User message + AI response pairs embedded into per-user vector store after each turn
- **Pre-compaction flush** — Before context is trimmed, all messages are defensively written to RAG
- **Hybrid search** — sqlite-vec vector similarity (70%) + FTS5 BM25 (30%)
- **Three chunk types**: `conversation`, `memory`, `todo` — each toggleable
- **Embedding**: OpenAI-compatible `EMBEDDING_MODEL` (default `text-embedding-3-small`, 1536 dims), BM25-only fallback if unavailable
- **Sentence-aware chunking**: 400-token chunks with 80-token overlap
- **Per-user isolation**: Separate vector stores per user
- **Search tool**: `rag_search` lets the agent query past context on demand

### The Learning Loop
```
User input → Agent responds (with profile + notepad context)
    → Conversation turn indexed in RAG
    → Agent saves universal facts to profile (shared)
    → Agent saves thread-specific context to notepad (local)
    → [Context limit reached] → Summary generated → RAG flush → Notepad re-injected
    → Agent continues with full knowledge
```

---

## 3. Dynamic Self-Customization

Nymeria adapts its capabilities at runtime without code changes. The agent discovers, installs, and enables tools and skills on the fly.

### User-Defined Default Tools
- Users configure their personal default tool set via API (`GET/PUT/DELETE /tools/defaults`) or desktop UI
- New threads inherit this personalized baseline automatically
- Core tools always available; optional tools curated per user preference

### Runtime Capability Expansion (`self-improve`)
- **Search** — `tool_search` finds tools by keyword/category after `self-improve` loads
- **Enable** — `tool_enable` activates optional tools per-thread with flexible TTL (`Nm`, `Nh`, `Nd`, `Nw`, or `never`)
- **MCP** — `manage_mcp` searches, previews, installs, inspects, and same-turn enables discovered MCP tools
- **Skills** — `skill_manage` lists, searches, installs, enables, and disables Agent Skills
- **Skill Kits** — `skill_kit_create` packages existing tools or creates HTTP tools before publishing a durable Skill Kit

### In-Turn Hot-Loading
- **Mid-stream graph rebuild** — Enable a tool and use it in the same turn (up to 3 reloads per turn)
- Agent calls `tool_enable(action="enable")` → graph ends → fresh graph compiled with new tool → agent continues in same SSE stream
- Skill Kit activation uses the same path when `metadata.nymeria.required_tools` declares required tool schemas
- `skill_config(action="publish")`, `skill_kit_create`, `skill_manage`, and `manage_mcp(action="install")` use reload metadata to refresh the right schemas/indexes in the same turn
- **Sliding renewal** — Re-enabling refreshes expiry; promoting to permanent upgrades classification
- **Lazy eviction** — Expired TTL tools filtered at graph-build time, no background scheduler

### MCP Server Discovery & Installation
- **`manage_mcp`** — Search official MCP registry + Smithery, preview install plans, install from Claude Desktop JSON/bare command/HTTP URL/registry ID, and inspect installed servers
- Auto-discovers tools, namespaces as `mcp__<server>__<tool>`, and enables them in the same turn when requested

### Skill Discovery & Installation
- **`skill_manage`** — List/search installed + Anthropic marketplace skills, install bundles, and enable/disable them per-thread
- **Security scanning** — Detects curl|bash pipes, rm -rf, eval base64, fork bombs before install
- **Progressive disclosure** — Only name + description loaded; full body on activation
- **Skill Kits** — Skills may declare exact Nymeria `required_tools`; activation strictly binds them with TTL
- **Self-improve Skill Kit** — Bundled workflow seeded into user global skills by default; users can untick "Enable globally" while generated Skill Kits activate only on the current thread unless explicitly made global
- **`skill_config`** — Validated agent-facing writer for generated `SKILL.md` files; strict dependency checks, user scope by default, global scope admin-only
- **Per-thread control** — Enable/disable skills per thread; four scopes (thread > user > global > bundled)

---

## 4. Core Agent Architecture

### LangGraph ReAct Agent
- **Reasoning + Acting loop** — LangGraph-based ReAct pattern with higher tool-call budgets (default 500 main, 300 callable, configurable callables up to 1000) and exact repeated tool/result loop detection
- **Per-user graph compilation** — Graphs compiled per user/thread based on memory hash, tool set, and thread config; LRU-cached (max 50 entries)
- **Dynamic tool binding** — Tools resolved at graph-build time from core + optional + callable + MCP + skill sources
- **Multi-provider LLM support** — Anthropic (native) plus OpenAI, OpenRouter, xAI, Gemini, Groq, DeepSeek, Mistral, local/self-hosted runtimes, and other OpenAI-compatible providers via a registry-backed adapter and live model-list endpoints
- **Extended thinking** — Configurable reasoning effort (off, on, low, medium, high) with thinking block visualization
- **Model hot-switching** — Change model per-thread or globally at runtime without restart
- **Cross-thread @mentions** — Prefix a chat message with `@ThreadName`,
  `@CallableName`, or `@"Thread With Spaces"` to route that turn to another
  owned thread while streaming the response in the current client view with a
  visible reference line like `Response from <thread>`. Routed turns preserve
  the normal thinking, preamble, tool-call, tool-result, artifact, and final
  response rendering pipeline. The target thread owns the persisted checkpoint
  history.

### Streaming (SSE)
- **Real-time SSE** for chat, autonomous tasks, and trigger executions
- **Chat events**: `thinking`, `tool_call`, `tool_result`, `workspace_artifact`, `dispatched`, `response`, `context_attached`, `compacted`, `tool_reload`, `queued`, `iteration_limit`, `error`, `done`
- **Ordered assistant steps**: visible pre-tool commentary streams and rehydrates as `response` steps before the matching `tool_call`, separate from hidden/expanded thinking blocks
- **Autonomous events**: `task_started`, `thinking`, `tool_call`, `tool_result`, `workspace_artifact`, `response`, `task_completed`, `webhook_message`

### Context Management
- **Auto-compaction** (default) — Automatic summarization at a configurable model-window threshold (default 80%)
- **In-context summarization** — Agent generates its own summary, no separate LLM call
- **Pre-compaction RAG flush** — All messages indexed before trimming (nothing lost)
- **Checkpoint pruning** — Old checkpoint rows cleaned up after compaction
- **Notepad re-injection** — Thread knowledge base re-attached after compaction
- **Token tracking** — Context stats exposed via API: token count, percentage, compaction count

### Concurrency
- **Per-thread locking** — Non-blocking acquisition with timeout and lock holder metadata
- **Queued execution** — Busy threads return "queued" events with wait duration
- **Explicit cancellation** — `/stop` and `POST /threads/{id}/stop` cancel active work on a thread

---

## 5. Tool System (90+ Tools)

### Core Tools (Always Loaded)

| Category | Tools |
|----------|-------|
| **Files** | `file_read`, `file_write` (writes confined to `NYMERIA_WORKSPACE_DIR`) |
| **Web** | `web_search` (Perplexity, 3 depth levels) |
| **Multi-Model** | `consult` (Gemini second opinion) |
| **Memory** | `memory_add`, `memory_edit`, `memory_read` (each takes `scope="global"` for profile or `scope="thread"` for notepad), `personality_set`, `rag_search` |
| **TODOs** | `nym_todo` (create/update with scheduling + recurrence), `nym_todo_delete`, `nym_todo_list` |
| **Notifications** | `notify` (Telegram/Discord/Slack/Teams, auto mode) |
| **Capability Expansion** | Default path is `Skill(name="self-improve")`; it binds `tool_search`, `tool_enable`, `manage_mcp`, `skill_manage`, `api_discover`, `http_request`, and `skill_kit_create` only when needed |

`bash_execute` is available only as an admin-enabled optional tool, not in the default core loadout.

### Optional Tool Categories

| Category | Tools | Description |
|----------|-------|-------------|
| **Browser** | 9 | Playwright with BeautifulSoup fallback; navigate, click, type, screenshot, scroll |
| **Outlook Email** | 18 | OAuth, auth reset, search (KQL), send, reply, forward, drafts, categories, attachments (PDF/DOCX/Excel) |
| **Google Calendar** | 15 | OAuth, auth reset, events CRUD, RSVP, free/busy, colors |
| **Twitch** | 23 | Chat, moderation, stream info, polls, predictions, clips, channel management |
| **Triggers** | 6 | Create, list, update, delete, inspect, source catalog |
| **Image Generation** | 1 | OpenAI GPT Image and Gemini Nano Banana prompt-to-image generation with workspace artifacts |
| **_PRV_A/Sheets** | 9 | Google Sheets CRUD, supplier lookup, product search, lifecycle status, pricing |
| **Watchdog** | 4 | Activity feed, cross-thread dispatch, cross-thread notepad, TODO overview |
| **Thread Spawning** | 1 | Dynamic callable thread creation with full config |
| **Self-Modification** | 8 | (Legacy) Direct code editing with backups and validation |

### Dynamic Tools
- **Callable thread tools** — Any thread with `callable=True` becomes a tool
- **Custom HTTP tools** — REST API calls with templates and JSONPath extraction
- **MCP server tools** — Auto-discovered from installed MCP servers
- **Skill meta-tool** — Synthesized `Skill(name)` tool per thread; Skill Kits hot-bind required tools on activation

---

## 6. Autonomous Operation

### TODO-Based Scheduling
- **Relative scheduling**: arbitrary positive durations like "45s", "17m", "2h", "1w" from now
- **Absolute scheduling**: ISO 8601 datetime
- **Recurrence patterns**: 5min, 10min, 15min, 30min, hourly, daily, weekly, monthly
- **Status tracking**: pending → in_progress → done
- **Auto-purge**: Completed TODOs are removed from the active TODO list after `TODO_AUTO_ARCHIVE_DAYS` (default 7)

### Ticker (Background Scheduler)
- **Daemon thread polling** — 5-second intervals (configurable 1–60s)
- **Parallel execution** — ThreadPoolExecutor (default 5 concurrent tasks)
- **Retry logic** — 3 retries before permanent failure, 10-minute backoff on iteration limits
- **Missed schedule recovery** — Detects and re-executes overdue TODOs on startup
- **Event publishing** — Real-time SSE for watching autonomous work

### Watchdog Monitoring
- **Staleness detection** — Finds TODOs unchanged beyond configurable threshold
- **Nudge mechanism** — Groups stale TODOs by thread, sends prompts via `/chat`
- **External notifications** — Alerts via Telegram/Discord/Slack with elapsed time
- **Kill switches** — Environment variable or file flag to disable

---

## 7. Event-Driven Triggers (Automation-Style)

### Trigger Sources (6 types, plugin auto-registration)
| Source | Type | Description |
|--------|------|-------------|
| **Webhook** | Push | HTTP POST from external services, workflow tools, or local scripts with optional secret |
| **Outlook Email** | Poll | Microsoft 365 inbox monitoring with triple deduplication, folder/sender/subject/importance filters, attachment pass-through |
| **RSS/Atom** | Poll | Feed monitoring (blogs, YouTube, GitHub, Reddit) with HTTP egress policy enforcement |
| **HTTP Poll** | Poll | URL monitoring with fire modes: change, status_code, contains, always |
| **Slack** | Poll | Channel/DM monitoring with keyword filtering and bot exclusion |
| **Microsoft Teams** | Poll | Channel monitoring via Graph API |

### Trigger Actions
- **agent_prompt** — Send prompt to bound thread via `/chat` with full SSE streaming
- **notify** — Publish notification via configured platforms
- **create_todo** — Create TODO item with template variable support

### Trigger Features
- **Condition filtering** — equals, not_equals, contains, starts_with, matches_regex (AND logic, case toggle)
- **Template variables** — Source-specific `{variable}` interpolation into actions
- **Thread binding** — Each trigger bound to one thread; auto-binds to creation thread
- **Busy-thread deferral** — Detects busy threads, defers to pending queue, retries next cycle
- **Batch processing** — Multiple events per poll batched into single LLM call
- **Cooldown** — Per-trigger minimum fire interval
- **Health tracking** — healthy → degraded (2+ errors) → failing (5+) with exponential backoff
- **Execution history** — Full audit trail (200 per user) with status and duration
- **Dry-run testing** — Test with sample data without firing
- **Plugin reload** — Reload source plugins from disk without restart

---

## 8. MCP (Model Context Protocol)

### Nymeria as MCP Server
- **Dual transport**: STDIO and HTTP (port 8001)
- **Thin-client architecture** — MCP calls the Nymeria REST/SSE API with `NYMERIA_SERVICE_TOKEN`; it does not create a second in-process agent
- **Configurable transcript verbosity** — `nymeria_chat`, `nymeria_get_thread_history`, and `nymeria_thread_history` accept `verbosity`: `verbose` preserves full-fidelity thinking, preamble text, raw SSE events when requested, persisted tool calls/args/results, workspace artifacts, final response, context stats, model metadata, and copy-ready markdown; `concise` keeps thinking/response text plus tool names/status without tool payloads; `chat` returns the smallest conversational text shape
- **Core management tools** — threads, per-thread config, global settings, TODOs, triggers, memories, RAG search, and history
- **MCP-friendly collection responses** — trigger lists and execution histories are wrapped as JSON objects with `total` counts so empty collections stay valid tool results
- **Per-user isolation** via `X-Nymeria-Act-As`

### Nymeria as MCP Client
- **4 install formats**: Claude Desktop JSON, bare stdio command, HTTP/SSE URL, registry ID
- **Auto-discovery** — Connect, discover tools, store schemas
- **Tool namespacing** — `mcp__<server_id>__<tool_name>`
- **Registry search** — Official MCP registry + Smithery
- **Hot-install** — New tools available after reload

---

## 9. Multi-Model & External Reasoning

### Provider Support
- **Anthropic** (native) — Claude Opus, Sonnet, Haiku
- **OpenAI** — GPT-4o and compatible
- **OpenRouter** — 200+ models
- **Local LLMs** — llama.cpp, KoboldCpp, LM Studio, Ollama via OpenAI-compatible API
- **CLIProxyAPI** — Proxy for Claude Max subscription access

### Consult Tool
- **Cross-model second opinion** — Ask Gemini 3 Pro / 2.5 Pro / 2.5 Flash via OpenRouter
- **Reasoning token extraction** — Shows external model's reasoning process

### Claude Code Integration
- **Headless CLI invocation** — sonnet/opus/haiku model selection
- **Controlled permissions** — Toggle file edit and bash access per invocation

---

## 10. Voice

### Text-to-Speech
- **OpenAI-compatible** — tts-1, tts-1-hd with 6 voices, speed 0.25–4.0x
- **Google Gemini TTS** — 200+ inline audio tags ([whispers], [excitedly], [sighs])
- **Cartesia Sonic** — High-quality 44.1kHz output
- **Formats**: MP3, WAV, Opus, AAC, FLAC, PCM

### Speech-to-Text
- **OpenAI Whisper** — whisper-1 with language hints
- **faster-whisper** — Local inference option
- **Formats**: WAV, MP3, M4A, FLAC, OGG, Opus

### Voice Chat
- `/voice/chat` — Audio in → STT → agent processing → TTS → audio out

---

## 11. Platform Integrations

### Discord Bot (30 slash commands)
- /ask, /stop, /clear, /compact, /thread, /context, /tasks, /export, /restart, /help
- /model, /models, /think, /status
- /todos (list/add/complete/delete), /config (show/get/set), /tools (core/optional/enabled/category/enable/disable)
- /memory (list/save/forget/search), /notepad (read/write/clear)
- /show-tools, /channel-context
- SSE streaming (~1.5s edit intervals), channel context toggle, workspace artifact upload
- Intelligent message splitting (2000 chars, code block-aware)

### Telegram Bot (35 commands)
- Full command parity with Discord (underscore-separated names)
- HTML formatting, inline stop button, privacy mode
- File delivery: images inline, documents downloadable (50MB max)

### Twitch Bot
- Viewer: !ask (cooldowns), !status, !help
- Mod: !clear, !pulse (periodic chat evaluation), !context, !stop, !start
- Pulse system: configurable interval (60–3600s), minimum message threshold
- Chat buffer (50–5000 messages), moderation EventSub awareness
- 23 Twitch-specific tools: chat, moderation, polls, predictions, clips, channel management

### Outlook Add-in
- Embedded taskpane in Outlook Web
- Office.js bridge reads email subject/sender/date
- Quick-action buttons: Process RFQ, Analyse Response, Check Parts
- Per-staff threads with separate config and notepad

### Slack, Matrix, Mattermost, Zulip, Rocket.Chat, Signal, WhatsApp, Messenger, Instagram, Webex, Teams, Google Chat & LINE Bots
- Two-way Slack Socket Mode client with linked-user enforcement, bind codes, and thread-aware replies
- Two-way Matrix Client-Server API sync client with mention/free-room gating and bind codes
- Two-way Mattermost WebSocket client with linked-user enforcement, bind codes, and thread-aware replies
- Two-way Zulip Events API client with linked-user enforcement, bind codes, and stream-topic routing
- Two-way Rocket.Chat realtime client with linked-user enforcement, bind codes, and thread-aware replies
- Two-way Signal client via signal-cli-rest-api SSE/JSON-RPC with linked-user enforcement, group mention gating, and bind codes
- WhatsApp Business Cloud API webhook client hosted by the API service, with linked-user enforcement and direct-chat thread binding
- Messenger Platform webhook client hosted by the API service, with linked-user enforcement, Page-scoped direct-chat thread binding, and bind codes
- Instagram Messaging webhook client hosted by the API service, with linked-user enforcement, account-scoped direct-chat thread binding, and bind codes
- Webex Messaging webhook client hosted by the API service, with linked-user enforcement, direct/group thread routing, and bind codes
- Microsoft Teams Bot Framework webhook client hosted by the API service, with linked-user enforcement, mention-gated group/channel routing, and bind codes
- Google Chat HTTPS webhook client hosted by the API service, with linked-user enforcement, mention-gated space/group routing, and bind codes
- LINE Messaging API webhook client hosted by the API service, with linked-user enforcement, mention-gated group/room routing, and bind codes

### Slack & Teams Trigger Sources
- Channel monitoring via trigger sources
- Outbound webhook notifications

---

## 12. Desktop Application (Tauri 2.x + Svelte 5)

### Chat
- Streaming token-by-token rendering, thinking block visualization, tool call cards
- Tool reload indicators, context status bar, file attachments, workspace artifacts

### Thread Management
- Unlimited threads with folders, pinning, search/filter
- Per-thread configuration panel: instructions, system prompt, Agent/callable settings, tools, MCP, LLM, and skills
- Desktop thread sharing via `.nymeria-thread.json`: exports portable config only and imports into a new empty thread

### Tool Management
- Built-in tool browser by category, custom HTTP/MCP tool creation
- Dedicated MCP server panel with install wizard and auto-discovery
- Tool testing, import/export, default tool configuration

### Dashboard
- Activity feed, TODO management with scheduling and recurrence
- Notification center, autonomous task streaming, connection health

### Triggers
- Setup wizard, trigger feed with enable/disable/test, execution history

### Skills
- Installed skills panel, Skill Kit required-tool chips, marketplace search and install, global defaults

### Settings
- LLM config (provider, model, temperature, thinking), context management, voice, logging
- Connection profiles for multi-server switching, CLIProxy control

### Themes (5)
- Midnight (default), Monokai, Dracula, Light, High Contrast

---

## 13. Mobile Application (Capacitor 6 + Svelte 5)

- Full-featured mobile client mirroring desktop capabilities
- 44 components across chat, threads, tools, triggers, dashboard, notifications
- Native capabilities via Capacitor: camera, haptics, network, preferences, keyboard, splash screen
- Android build-ready, markdown rendering + syntax highlighting

---

## 14. REST API (60+ Endpoints)

| Category | Endpoints |
|----------|-----------|
| **Chat & Streaming** | `/chat` (SSE), `/chat/sync`, `/autonomous/stream` |
| **Threads** | CRUD, history, context stats, compact, stop, config |
| **Tools** | List, optional, defaults (GET/PUT/DELETE), categories, custom CRUD, import/export, test |
| **Unified Tools** | Combined built-in + custom view with enable/disable/config |
| **Settings** | Get, patch, LLM runtime diagnostics |
| **TODOs** | CRUD, complete, list by user |
| **Activity & Notifications** | Feed, mark read |
| **Callable Threads** | List, create |
| **RAG** | Settings, stats, search, reindex, delete index |
| **MCP Servers** | CRUD, discover tools, test |
| **Skills** | List, install, uninstall, marketplace search, global defaults |
| **Triggers** | CRUD, test, execution history, fire webhook, source list, source reload |
| **Voice** | Chat (audio→agent→audio), TTS, STT |
| **Devices** | FCM token registration |
| **Models** | List available models |

---

## 15. Deployment

### Docker Compose Stack
| Container | Port | Purpose |
|-----------|------|---------|
| nymeria-api | 8000 | FastAPI REST API + SSE streaming |
| nymeria-worker | — | Ticker daemon for autonomous tasks |
| nymeria-mcp | 8001 | MCP server (HTTP mode) |
| nymeria-postgres | 5432 | PostgreSQL 15 |
| nymeria-redis | 6379 | Redis event bus |
| cli-proxy-api | 8317 | CLIProxyAPI (Claude Max subscription proxy) |

### Other Modes
- **Local dev** — `python run.py api|cli|worker|mcp`
- **Foreground gateway** — `python run.py service` (GatewayServer with graceful shutdown)
- **Interactive CLI** — `python run.py cli`
- **Non-interactive CLI** — `python run.py cli -m "prompt"` (oneshot mode with plain or JSON output)
- **CLI JSON output** — add `--json` to slash-command list/stat commands for machine-readable stdout: `/thread list --json`, `/tools list --json`, `/settings --json`, `/context --json`, and `/usage --json`.
- **Session resume** — `python run.py cli -c` (continue most recent thread), `python run.py cli -r <ref>` (resume by ID/title)
- **Session export/import** — `python run.py cli --export <thread-id> --format json|md|jsonl` (non-interactive export); `/export` and `/import` slash commands in the REPL
- **Shell completions** — `python run.py completion bash|zsh|fish` generates tab-completion scripts (covers all subcommands, flags, and known choices)
- **Clipboard copy** — `/copy` copies the last assistant response to clipboard; `/copy N` copies the Nth most recent; `/copy code` extracts only fenced code blocks
- **Undo / retry** — `/undo` removes the last user+assistant exchange from thread state; `/retry` re-sends the last user message for a new response (or `/retry <new prompt>` to replace it). Requires `POST /threads/{id}/rewind` backend endpoint.
- **Session branching** — `/branch [title]` or `/fork` creates a new thread from the current thread's checkpoint history and per-thread config, then switches to it. `/branch --from N [title]` branches from the checkpoint at or before message index `N`.
- **Reasoning toggle** — `/reasoning on|off|low|medium|high` toggles extended thinking in one command (per-thread when a thread is active, global otherwise). Alias `/thinking`. Status bar shows `thinking: <effort>` when active.
- **Provider credentials** — `/provider`, `/provider list`, `/provider set <provider> api_key=<key>`, `/provider test [provider]`, and `/provider switch <provider>` manage LLM provider auth from the CLI. Secrets are saved in `~/.nymeria/credentials.json` with `0600` permissions and applied through the backend settings API for admin users.
- **Model fallback chain** — `/fallback`, `/fallback add <model-id> [--position N]`, `/fallback remove <model-id>`, `/fallback set <model1> <model2> ...`, and `/fallback clear` manage `LLM_FALLBACK_MODELS`. The backend tries the ordered chain for retryable provider/transport failures before any output chunk, so every frontend surface benefits from the same resilience behavior.
- **Fast model toggle** — `/fast`, `/fast on`, and `/fast off` switch the active thread between the default model and `LLM_FAST_MODEL`; `/fast set <model-id>` stores the fast model; `/fast <prompt>` uses the fast model for one turn without leaving the thread in fast mode. Status bar shows `FAST` while active.
- **Token usage / cost** — `/usage` shows current thread token consumption (input, output, context window fill with graphical bar, estimated cost). `/usage session` shows aggregate across all turns in the current CLI session with per-model breakdown. Aliases: `/tokens`, `/cost`. Status bar shows hermes-style context bar: `ctx 45.2k/200k [████████░░░░░░░░░░░░] 23%`.

### Configuration
- 100+ environment variables across LLM, API, database, messaging, autonomous, context, voice, logging
- Hot-reload settings via `PATCH /settings` (clears LRU cache, rebuilds graphs)
- Per-thread LLM overrides: provider, model, temperature, thinking mode

---

## 16. Persistence & Data

### Storage Backends
- **SQLite** (default), **PostgreSQL** (Docker), **Redis** (event bus), **Memory** (testing)

### Data Layout
| Path | Purpose |
|------|---------|
| `data/nymeria.db` | LangGraph checkpoint store (conversation history) |
| `data/todo_schedule.db` | TODO scheduling index |
| `data/todos/{user_id}.json` | TODO items |
| `data/users/{user_id}/profile.json` | User profile and memories (shared knowledge base) |
| `data/users/{user_id}/memory.db` | RAG vector store (sqlite-vec + FTS5) |
| `data/thread_notes/{thread_id}.md` | Thread notepads (per-thread knowledge base) |
| `data/thread_configs/{thread_id}.json` | Per-thread configuration |
| `data/custom_tools/` | Custom tool definitions |
| `data/skill_drafts/` | Agent-authored Skill/Skill Kit drafts |
| `data/mcp_servers/` | MCP server configurations |
| `data/skills/` | Installed skills (user/global scope) |
| `data/triggers/` | Trigger definitions and execution logs |
| `data/notifications/` | Notification storage |
| `data/logs/` | Audit trail (JSONL) + rotating service log |

---

## 17. Logging & Observability

### Named Profiles (11)
llm, tools, agent, threads, ticker, triggers, checkpoints, api, sse, compactor, all

### Log Tags
`[LLM]`, `[ASTREAM]`, `[CALLABLE]`, `[TICKER]`, `[WATCHDOG]`, `[TRIGGER]`

Framing: `=== START ===` / `=== END ===` / `=== ERROR ===` with thread ID, elapsed time, metrics. Per-module overrides via `LOG_MODULES`.

---

## 18. Key Limits & Constraints

| Feature | Limit | Configurable |
|---------|-------|:---:|
| User memories (profile) | 100 per user, 1000 chars each | No |
| Thread notepad | 50 KB | No |
| Thread instructions | 5000 chars | No |
| System prompt override | 50,000 chars | No |
| Agent tool calls per turn | 500 (main), 300 (callable) | Callable override 1–1000 |
| Spawn depth | 3 levels | Yes |
| Spawns per hour | 10 per parent | Yes |
| Tool reloads per turn | 3 | No |
| Concurrent autonomous tasks | 5 | Yes |
| Self-invocations per hour | 50 | Yes |
| Ticker poll interval | 5 seconds | Yes (1–60s) |
| Lock timeout | 120 seconds | Yes |
| Tool timeout | 300 seconds | Yes |
| Compact threshold | 80% | Yes (5–95%) |
| Graph cache | 50 entries | No |
| Active TODOs | 50 per user | No |
| TODO auto-archive | 7 days | Yes (1–30) |
| Triggers per user | 50 | No |
| Trigger execution history | 200 per user | No |
| Attachments per message | 4 files | No |
| Discord messages | 2000 chars | No |
| Telegram messages | 4096 chars | No |
| Teams messages | 4000 chars | No |
| Signal messages | 8000 chars | No |
| Twitch messages | 500 chars | No |
