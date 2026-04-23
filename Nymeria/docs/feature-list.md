# Nymeria — Complete Feature List

Comprehensive feature inventory for comparison with other AI agent platforms. Last updated: 2026-04-23.

---

## 1. Core Agent Architecture

### LangGraph ReAct Agent
- **Reasoning + Acting loop** — LangGraph-based ReAct pattern with configurable iteration limits (default 70, callable threads default 50, configurable 1–200)
- **Per-user graph compilation** — Graphs compiled per user/thread based on memory hash, tool set, and thread config; LRU-cached (max 50 entries)
- **Dynamic tool binding** — Tools resolved at graph-build time from core + optional + callable + MCP + skill sources
- **Multi-provider LLM support** — Anthropic (native), OpenAI, OpenRouter, local LLMs (llama.cpp, KoboldCpp, LM Studio, Ollama) via OpenAI-compatible API
- **Extended thinking** — Configurable reasoning effort (off, on, low, medium, high) with thinking block visualization
- **Model hot-switching** — Change model per-thread or globally at runtime without restart

### Concurrency & Thread Safety
- **Per-thread locking** — `ThreadLockManager` with non-blocking acquisition, timeout handling, and lock holder metadata
- **Queued execution** — When a thread is busy, new requests receive "queued" events with lock holder info and wait duration
- **Cascading abort** — `abort_with_cascade()` recursively aborts parent and all active child threads
- **Dangling tool-call patching** — Synthetic ToolMessages injected after mid-execution cancellation to prevent LLM hallucination
- **User-message preemption** — User messages cancel pending autonomous tasks on the same thread

### Streaming
- **Server-Sent Events (SSE)** — Real-time streaming for chat, autonomous tasks, and trigger executions
- **Chat SSE events**: `thinking`, `tool_call`, `tool_result`, `workspace_artifact`, `response`, `context_attached`, `compacted`, `command_result`, `error`, `done`
- **Autonomous SSE events**: `task_started`, `thinking`, `tool_call`, `tool_result`, `workspace_artifact`, `response`, `task_completed`
- **Tool reload events**: `tool_reload` — mid-turn graph rebuild for newly-enabled tools (up to 3 reloads per turn)

---

## 2. Context Management

### Auto-Compaction
- **Threshold-based** — Automatic summarization when context reaches configurable threshold (default 80% of model limit)
- **In-context summarization** — Agent generates its own summary (no separate LLM call), preserving full understanding
- **Pre-compaction memory flush** — Conversations defensively written to RAG store (sqlite-vec + FTS5) before trimming
- **Checkpoint pruning** — Raw SQL DELETE of pre-compact checkpoint rows (UUIDv7 boundary, safe under thread lock)
- **Auto-resume** — Summary injected inline, agent continues immediately
- **Manual compaction** — `/compact` endpoint or slash command for on-demand compression

### Context Modes
- **auto_compact** (default) — Intelligent summarization with memory preservation
- **sliding_window** (legacy) — Keep last N conversation cycles
- **none** — Manual `/compact` only, no automatic management

### Token Tracking
- `TokenTracker` monitors usage against model context window
- Context stats exposed via API: token count, percentage used, compaction count, model limits

---

## 3. Memory & Knowledge

### User Profile Memory
- **Persistent key-value memories** — Up to 100 memories per user (1000 chars each)
- **Automatic injection** — Saved memories injected into system prompt on every invocation
- **Memory tools**: `profile_save`, `profile_forget`, `profile_list`
- **Personality preferences** — `personality_set` for tone, verbosity, and expertise level

### RAG (Retrieval-Augmented Generation)
- **Hybrid search** — sqlite-vec vector similarity (70% weight) + FTS5 BM25 (30% weight)
- **Embedding provider** — OpenAI `text-embedding-3-small` (1536 dimensions), BM25-only fallback
- **Three chunk types**: `conversation`, `memory`, `todo`
- **Sentence-aware chunking** — 400-token chunks with 80-token overlap and boundary detection
- **Per-user isolation** — Separate vector stores per user
- **Configurable settings** — Enable/disable per chunk type, auto-flush on compaction

### Thread Notepad
- **Persistent per-thread notes** — Survives compaction, re-injected on context resume
- **4 operations**: write (append/replace), read, edit (find-and-replace), clear
- **50KB limit** per thread
- **Used by autonomous tasks** — Ticker reads notepad for context after compaction

---

## 4. Tool System (90+ Tools)

### Core Tools (Always Loaded — 27)

| Tool | Description |
|------|-------------|
| `bash_execute` | Shell commands with working directory and timeout (120s max) |
| `file_read` | Read files (10MB max) with line limiting |
| `file_write` | Write/append files with directory creation and workspace attachment |
| `web_search` | Perplexity-powered search with depth levels (quick/standard/deep) |
| `consult` | Second opinion from Gemini via OpenRouter (3 model choices) |
| `claude_code` | Invoke Claude Code CLI headless (sonnet/opus/haiku, file edit + bash permissions) |
| `profile_save` | Save persistent user memory |
| `profile_forget` | Remove memory by key |
| `profile_list` | List all memories and personality preferences |
| `personality_set` | Set communication traits |
| `rag_search` | Semantic search over conversations, memories, and TODOs |
| `nym_todo` | Create/update TODOs with scheduling and recurrence |
| `nym_todo_delete` | Delete TODO by 8-character ID |
| `nym_todo_list` | List TODOs with status filtering |
| `notepad_write` | Write to thread notepad (append/replace) |
| `notepad_read` | Read thread notepad |
| `notepad_edit` | Find-and-replace in notepad |
| `notepad_clear` | Clear notepad |
| `notify` | Send notifications (Telegram/Discord/Slack/Teams, auto mode) |
| `tool_search` | Search, enable, disable optional tools with TTL |
| `list_installed_skills` | List skills across all scopes |
| `search_skills` | Semantic skill search (embeddings + BM25 + substring) |
| `install_skill` | Install from Anthropic skills marketplace |
| `mcp_search` | Search MCP registries (official + Smithery) |
| `mcp_install` | Install MCP server (JSON/CLI/URL/registry ID) |
| `reload_all` | Hot-reload all tools, agents, and trigger sources |
| `self_modify_rollback` | Rollback file to previous backup |

### Optional Tool Categories

**Browser Automation (9 tools)**
- Playwright-based with automatic fallback to requests + BeautifulSoup
- Navigate, click, type, get content, screenshot, scroll, press key, close, status
- Headless auto-detection (Docker/display environment)
- Anti-bot measures, Chrome 120 user-agent spoofing
- Content extraction (8000 char limit), link extraction (20 max)

**Outlook Email (16 tools)**
- Microsoft OAuth device-code flow
- List, get, search (KQL queries), send, reply, reply-all, forward
- Draft management (create, edit, reply drafts)
- Mailbox operations: delete, mark read/unread, move, set category
- Attachment extraction (PDF/DOCX via Gemini, Excel via openpyxl)
- Batch email retrieval (comma-separated IDs)

**Google Calendar (14 tools)**
- Google OAuth flow
- List calendars, events, search, get details
- Create events (all-day or timed, with attendees/timezone/location)
- Update, delete, RSVP (accept/decline/tentative)
- Free/busy queries, color management

**Twitch (23 tools)**
- Chat read/send, announcements (with colors)
- Moderation: timeout, ban, unban, warn, delete messages, AutoMod review
- Stream info: live status, viewers, channel details, schedule
- Engagement: clips, polls (2-5 choices), channel point predictions
- Channel management: title, game, tags, shoutouts
- Subscription queries

**Trigger Management (5 tools)**
- Create, list, update, delete triggers
- Trigger source catalog with config fields and template variables

**_PRV_A/Google Sheets (9 tools)**
- Generic Google Sheets search, append, update
- _PRV_A-specific: supplier lookup, vendor info, product search
- Acme lifecycle status with migration paths
- Acme Electric pricing

**Watchdog (4 tools)**
- Activity feed with lookback window
- Cross-thread TODO dispatch
- Cross-thread notepad reading
- All-thread TODO overview

**Thread Spawning (1 tool)**
- Dynamic thread creation with scoped config
- Rate-limited: 10 spawns/hour, max depth 3
- Auto-callable (becomes invocable tool)
- Initial message with blocking response
- Bulk tool/category enablement

**Self-Modification (8 tools)**
- Read/write/delete files in tools/, agents/, triggers/sources/
- Automatic backups before modifications
- Python syntax validation before write
- Import testing to prevent broken deployments
- Live reload after changes

**Slash Command (1 tool)**
- Run Nymeria slash commands programmatically

### Dynamic Tools
- **Callable thread tools** — Any thread with `callable=True` becomes a `ThreadName(task: str) → str` tool
- **Custom HTTP tools** — REST API calls with method/URL/headers/body templates and JSONPath extraction
- **Custom MCP tools** — Connect to MCP servers via stdio with tool discovery
- **Skill meta-tool** — Synthesized `Skill(name)` tool per thread showing available skills

### Tool Hot-Loading (In-Turn Auto-Reload)
- **Mid-stream graph rebuild** — Enable a tool and use it in the same turn (up to 3 reloads)
- **TTL enablements**: 30m, 2h (default), 6h, 24h, permanent
- **Sliding renewal** — Re-enabling refreshes expiry; promoting to permanent upgrades classification
- **Lazy eviction** — Expired tools filtered at graph-build time, no background scheduler
- **Disable preserves state** — Re-enabling restores original TTL/permanent classification
- **Seamless SSE** — Client sees one continuous response across graph rebuilds

---

## 5. Autonomous Operation

### TODO-Based Scheduling
- **Relative scheduling**: "30s", "5m", "2h", "1d" from now
- **Absolute scheduling**: ISO 8601 datetime
- **Recurrence patterns**: 5min, 10min, 15min, 30min, hourly, daily, weekly, monthly
- **Status tracking**: pending → in_progress → done
- **Auto-purge**: Completed non-recurring TODOs archived after 7 days

### Ticker (Background Scheduler)
- **Daemon thread polling** — 5-second intervals (configurable 1–60s)
- **Parallel execution** — ThreadPoolExecutor with configurable concurrency (default 5)
- **Rate limiting** — 50 self-invocations per hour (configurable)
- **Retry logic** — 3 retries before permanent failure, 10-minute backoff on iteration limits
- **Missed schedule recovery** — Detects and re-executes overdue TODOs on startup
- **Event publishing** — Real-time SSE events for watching autonomous work
- **Graceful shutdown** — 2-second timeout with cleanup

### Watchdog Monitoring
- **Staleness detection** — Finds TODOs unchanged beyond configurable threshold
- **Nudge mechanism** — Groups stale TODOs by thread, sends prompts via `/chat`
- **Duplicate prevention** — Tracks nudged TODOs, clears when updated
- **External notifications** — Alerts via Telegram/Discord/Slack with elapsed time
- **Kill switches** — Environment variable or file flag to disable

---

## 6. Event-Driven Triggers (N8N-Inspired, April 2026 Revamp)

### Trigger Sources (6 types, plugin auto-registration)
| Source | Type | Description |
|--------|------|-------------|
| **Webhook** | Push | HTTP POST from external services (IFTTT, Zapier, n8n, Tasker) with optional secret authentication |
| **Outlook Email** | Poll | Microsoft 365 inbox monitoring with triple deduplication (category tagging + timestamp filtering + rolling 200-ID window), folder selection (inbox/sent/drafts/junk/archive), filters (from, subject, body, importance, unread_only), attachment pass-through |
| **RSS/Atom** | Poll | Feed monitoring (blogs, YouTube, GitHub, Reddit) with configurable max items per check |
| **HTTP Poll** | Poll | URL monitoring with fire modes: `change`, `status_code`, `contains`, `always`; custom headers support |
| **Slack** | Poll | Channel and DM monitoring with keyword filtering and bot exclusion |
| **Microsoft Teams** | Poll | Channel monitoring via Graph API with team/channel/message filtering |

All sources implement `BaseTriggerSource`, auto-register via plugin discovery, include rich metadata (category, icon, setup guide, example config), and support sample events for testing.

### Trigger Actions
- **agent_prompt** — Send prompt to bound thread with `{variable}` template interpolation; routes through `/chat` for full autonomous SSE streaming
- **notify** — Publish notification via configured platforms
- **create_todo** — Create TODO item for user with template variable support

### Trigger Features
- **Condition filtering** — equals, not_equals, contains, starts_with, matches_regex (AND logic, case-sensitivity toggle)
- **Template variables** — Source-specific variables interpolated into action templates (e.g. `{subject}`, `{from_address}`, `{title}`, `{link}`)
- **Thread binding** — Every trigger binds to exactly one thread; auto-binds to creation thread for LLM-created triggers
- **Busy-thread deferral** — Agent-prompt triggers detect busy threads (non-blocking lock check), defer events to pending_events queue, retry next poll cycle
- **Batch processing** — Multiple events from one poll cycle batched into a single LLM call
- **Cooldown** — Per-trigger minimum fire interval in seconds
- **Health tracking** — Automatic status progression: healthy (0 errors) → degraded (2+ consecutive) → failing (5+) with exponential backoff
- **Execution history** — Full audit trail (max 200 per user) with status (success/error/partial/deferred), duration, and error details
- **Dry-run testing** — Test triggers with sample data without actually firing
- **Source plugin reload** — `POST /triggers/sources/reload` reloads source plugins from disk without restart

### Trigger LLM Tools (6)
| Tool | Description |
|------|-------------|
| `trigger_create` | Create event-driven trigger with full source/action/condition config |
| `trigger_list` | List triggers (filter: enabled_only, current_thread_only) |
| `trigger_update` | Update trigger name, config, or enabled status |
| `trigger_delete` | Delete trigger permanently |
| `trigger_inspect` | View trigger details, test with sample data, view execution history |
| `trigger_sources_info` | Get catalog of available sources with setup guides and template variables |

### Trigger REST API (11 endpoints)
- `GET /triggers` — List triggers (filter by enabled_only, thread_id)
- `POST /triggers` — Create new trigger
- `GET /triggers/{trigger_id}` — Get trigger details
- `PATCH /triggers/{trigger_id}` — Update trigger
- `DELETE /triggers/{trigger_id}` — Delete trigger
- `POST /triggers/fire/{trigger_id}` — Webhook fire endpoint (public, no auth; optional secret validation)
- `POST /triggers/{trigger_id}/test` — Dry-run with sample data
- `GET /triggers/{trigger_id}/executions` — Execution history for specific trigger
- `GET /triggers/executions/recent` — Recent executions across all triggers
- `GET /triggers/sources/list` — List available sources with metadata
- `POST /triggers/sources/reload` — Reload source plugins from disk

### Trigger Limits
| Limit | Value |
|-------|-------|
| Triggers per user | 50 |
| Execution history per user | 200 entries |
| Deduplication window (Outlook) | 200 message IDs |

---

## 7. Skills System (Anthropic SKILL.md Spec)

### Skill Architecture
- **Progressive disclosure** — Only name + description loaded initially; full body on activation
- **Three-layer scoping** — Thread-enabled > User > Global > Bundled (precedence order)
- **No new Python** — Skills use existing tools through procedural text instructions

### Skill Features
- **Semantic search** — OpenAI embeddings → BM25 → substring fallback
- **Marketplace integration** — Anthropic `anthropics/skills` repo via GitHub (15-min cache)
- **Security scanning** — Detects curl|bash pipes, rm -rf, eval base64, fork bombs
- **Per-thread control** — `enabled_skills` extends, `disabled_skills` subtracts from global set
- **Graph cache invalidation** — Detects skill changes and forces recompilation
- **Cross-check on activation** — Warns if skill's allowed-tools conflict with thread's disabled-tools

---

## 8. MCP (Model Context Protocol)

### MCP Server Mode (Nymeria as MCP server)
- **Dual transport**: STDIO mode and HTTP mode (port 8001)
- **Exposed tools**: `nymeria_chat`, `nymeria_profile_save/list/forget`, `nymeria_todo_add/list/complete/update/delete/status`, `nymeria_rag_search`, `nymeria_thread_history`
- **Stateless HTTP** — Each request independent, agent lazily initialized
- **Per-user isolation** — All operations scoped to user_id

### MCP Client (Nymeria consuming MCP servers)
- **4 source formats**: Claude Desktop JSON, bare stdio command, HTTP/SSE URL, registry ID
- **Auto-discovery** — Connect, discover tools, store schemas
- **Tool namespacing** — `mcp__<server_id>__<tool_name>`
- **Registry search** — Official MCP registry + Smithery
- **Hot-install** — New MCP tools available after reload

---

## 9. Multi-Model & External Reasoning

### Provider Support
- **Anthropic** (native `/v1/messages`) — Claude Opus, Sonnet, Haiku
- **OpenAI** — GPT-4o and compatible
- **OpenRouter** — 200+ models with automatic routing
- **Local LLMs** — llama.cpp, KoboldCpp, LM Studio, Ollama via OpenAI-compatible API
- **CLIProxyAPI** — Proxy for Claude Max subscription access

### Consult Tool (Second Opinion)
- **Cross-model reasoning** — Ask Gemini 3 Pro / 2.5 Pro / 2.5 Flash for independent analysis
- **Reasoning token extraction** — Shows reasoning process from external model
- **180-second timeout** — Long-running reasoning support

### Claude Code Integration
- **Headless CLI invocation** — sonnet/opus/haiku model selection
- **Controlled permissions** — Toggle file edit and bash access per invocation
- **Output capture** — Full stdout/stderr with exit code

---

## 10. Voice

### Text-to-Speech (TTS)
- **OpenAI-compatible** — tts-1, tts-1-hd with 6 voices, speed control 0.25–4.0x
- **Google Gemini TTS** — 200+ inline audio tags (`[whispers]`, `[excitedly]`, `[sighs]`)
- **Cartesia Sonic** — High-quality TTS at 44.1kHz
- **Output formats**: MP3, WAV, Opus, AAC, FLAC, PCM

### Speech-to-Text (STT)
- **OpenAI Whisper** — whisper-1 with language hints
- **faster-whisper** — Local inference option
- **Supported formats**: WAV, MP3, M4A, FLAC, OGG, Opus

### Voice Chat Endpoint
- `/voice/chat` — Audio in → STT → agent processing → TTS → audio out

---

## 11. Platform Integrations

### Discord Bot (30 slash commands)
- **Slash commands**: /ask, /stop, /clear, /compact, /thread, /context, /tasks, /export, /restart, /help, /model, /models, /think, /status, /todos (4 subcommands), /config (3), /tools (6), /memory (4), /notepad (3), /show-tools, /channel-context
- **Respond modes**: mention-only (default) or all messages
- **SSE streaming** — ~1.5s edit intervals for live responses
- **Channel context** — Toggle recent channel messages as input context
- **Tool call display** — Hidden (default with separators) or shown (blue → green embeds)
- **File delivery** — Workspace artifacts auto-uploaded
- **Attachment support** — 4 files max (images 10MB, docs 20MB)
- **Intelligent message splitting** — 2000 char limit with code block, paragraph, sentence awareness

### Telegram Bot (35 commands)
- **Full command set**: /ask, /stop, /clear, /compact, /thread, /context, /tasks, /export, /restart, /showtools, /help, /start, /model, /models, /think, /status, todo commands (4), config commands (3), tool commands (6), memory commands (4), notepad commands (3)
- **HTML formatting** — Markdown-to-HTML conversion
- **Stop button** — Inline keyboard on first message
- **Privacy mode** — Only receives commands and replies in groups
- **File delivery** — Images < 10MB inline, others as downloadable documents (50MB max)

### Twitch Bot
- **Viewer commands**: !ask (with cooldowns), !status, !help
- **Mod commands**: !clear, !pulse, !context, !stop, !start
- **Pulse system** — Periodic evaluation of chat with configurable interval (60–3600s) and minimum message threshold
- **Chat buffer** — Configurable 50–5000 message buffer
- **Moderation awareness** — EventSub for bans, timeouts, unbans, message deletions
- **23 Twitch-specific tools** — Chat, moderation, stream info, polls, predictions, clips, channel management

### Outlook Add-in
- **Embedded taskpane** — Opens in Outlook Web as side panel
- **Office.js bridge** — Reads email subject, sender, date from current item
- **Quick-action buttons**: Process RFQ, Analyse Response, Check Parts
- **Per-staff threads** — 5 identical threads with separate config and notepad
- **Attachment extraction** — PDF/DOCX (via Gemini), Excel (openpyxl), CSV/TXT direct

### Slack Integration
- **Channel monitoring** — Trigger source for event-driven actions
- **Webhook notifications** — Outbound alerts via webhook URL

### Microsoft Teams Integration
- **Channel monitoring** — Trigger source via Graph API
- **Webhook notifications** — Outbound alerts

---

## 12. Desktop Application (Tauri 2.x + Svelte 5)

### Chat Interface
- **Streaming message display** — Real-time token-by-token rendering
- **Thinking block visualization** — Collapsible reasoning display
- **Tool call cards** — Arguments and results with expand/collapse
- **Tool reload indicators** — Visual marker between graph rebuilds
- **Context status bar** — Token usage, context limits, compaction status
- **File attachments** — Upload with preview (images + documents)
- **Workspace artifact modal** — Preview and download generated files
- **Image modal** — Full-size image viewing

### Thread Management
- **Unlimited threads** with folder organization
- **Multiple sort modes** — Date, name, pinned
- **Thread pinning** and custom titles
- **Per-thread configuration panel** — Instructions, system prompt, tool overrides, LLM config, skills, callable settings
- **Thread search/filter**

### Tool Management
- **Built-in tool browser** — Core and optional tools by category
- **Custom tool creation** — HTTP tools (method/URL/headers/body/JSONPath) and MCP tools
- **MCP server panel** — Add, configure, test, discover tools from MCP servers
- **MCP install wizard** — Pre-configured recipes for common servers
- **Tool testing** — Execute tools with parameter input
- **Import/export** — Bulk custom tool JSON import/export
- **Default tools** — Configure default tool set for new threads
- **Tool count warnings** — Performance alerts when too many tools enabled

### Dashboard
- **Activity feed** — Polling-based (45s intervals) with thread filtering
- **TODO management** — Create, edit, schedule, complete, delete with recurrence
- **Scheduled tasks feed** — View all autonomous tasks
- **Notification center** — Unread count, mark-as-read
- **Autonomous task streaming** — Real-time SSE for watching background work
- **Connection status** — Backend health with latency measurement (15s polling)

### Trigger Management
- **Setup wizard** — Multi-step trigger creation
- **Trigger feed** — List with enable/disable, test, delete
- **Execution history** — Recent trigger firings with status

### Skills Management
- **Installed skills panel** — View and manage per-scope
- **Marketplace search** — Browse and install from Anthropic repository
- **Global skill defaults** — Applied to every new thread

### Settings
- **LLM configuration** — Provider, model, temperature, top_p, top_k, penalties, max tokens, thinking mode
- **Context management** — Strategy selection, threshold, keep messages
- **Agent settings** — Watchdog, logging level
- **Voice settings** — TTS provider, voice, format
- **Connection profiles** — Save/switch between multiple backend instances
- **CLIProxy panel** — Service control, OAuth session management

### Themes (5)
- **Midnight** — Dark with cyan accents (default)
- **Monokai** — Editor theme with hot pink
- **Dracula** — Purple/pink dark theme
- **Light** — Warm paper editorial theme
- **High Contrast** — Pure black with amber accents

### Cross-Client Sync
- **Unique client IDs** for deduplication
- **Sync polling** — Real-time updates across multiple open instances

---

## 13. Mobile Application (Capacitor 6 + Svelte 5)

- **Full-featured mobile client** — Not a lite version; mirrors desktop capabilities
- **44 components** across chat, threads, tools, triggers, dashboard, notifications
- **17 Svelte stores** for state management
- **Native capabilities** via Capacitor: camera, haptics, network detection, preferences, keyboard handling, splash screen, status bar
- **Android build-ready** — Capacitor syncs to native Android project
- **Markdown rendering** (Marked 12) + syntax highlighting (highlight.js 11)

---

## 14. Self-Modification

### SelfModifyAgent
- **Live code editing** — Read, write, delete files in `tools/`, `agents/`, `triggers/sources/`
- **Automatic backups** — BackupManager creates versioned copies before changes
- **Code validation** — Python syntax checking, `@tool` decorator enforcement for new tools
- **Import testing** — Validates all tools import successfully before going live
- **Tool invocation testing** — Execute newly created tools with JSON arguments
- **Hot-reload** — `agent.reload_tools()` makes changes available immediately
- **Rollback** — Restore any file to previous backup version

---

## 15. REST API (60+ Endpoints)

### Endpoint Categories
- **Chat & Streaming** — `/chat` (SSE), `/chat/sync`, `/autonomous/stream`
- **Thread Management** — CRUD, history, context stats, compact, stop, config
- **Tools** — List, optional, defaults, categories, custom CRUD, import/export, test
- **Unified Tools** — Combined built-in + custom view with enable/disable/config
- **Settings** — Get, patch, LLM runtime diagnostics
- **TODOs** — CRUD, complete, list by user
- **Activity & Notifications** — Feed, mark read
- **Workspace** — File download
- **Callable Threads** — List, create
- **RAG** — Settings, stats, reindex, delete index
- **MCP Servers** — CRUD, discover tools, test
- **Skills** — List, install, uninstall, marketplace search, global defaults
- **Triggers** — CRUD, test, execution history, fire webhook
- **Voice** — Chat (audio→agent→audio), TTS, STT
- **Devices** — FCM token registration/unregistration
- **Models** — List OpenRouter models

---

## 16. Persistence & Data

### Storage Backends
- **SQLite** (default) — `data/nymeria.db` for conversations, `data/todo_schedule.db` for scheduling
- **PostgreSQL** — Production Docker deployment
- **Redis** — Event bus for cross-container communication
- **Memory** — Ephemeral (testing only)

### Data Layout
- `data/nymeria.db` — LangGraph checkpoint store
- `data/todo_schedule.db` — TODO scheduling index
- `data/todos/{user_id}.json` — TODO items
- `data/users/{user_id}/profile.json` — User profile and memories
- `data/users/{user_id}/memory.db` — RAG vector store
- `data/thread_notes/{thread_id}.md` — Thread notepads
- `data/custom_tools/` — Custom tool definitions
- `data/mcp_servers/` — MCP server configurations
- `data/notifications/` — Notification storage
- `data/logs/audit_YYYYMMDD.jsonl` — Audit trail
- `data/logs/service.log` — Rotating application log
- `data/backups/` — Self-modification backup versions

### Audit Logging
- JSONL format with daily rotation
- Logs all tool executions with arguments and results
- Configurable via `AUDIT_LOG_ENABLED`

---

## 17. Deployment

### Docker Compose Stack
| Container | Port | Purpose |
|-----------|------|---------|
| nymeria-api | 8000 | FastAPI REST API + SSE streaming |
| nymeria-worker | — | Ticker daemon for autonomous tasks |
| nymeria-mcp | 8001 | MCP server (HTTP mode) |
| nymeria-postgres | 5432 | PostgreSQL 15 |
| nymeria-redis | 6379 | Redis event bus |
| cli-proxy-api | 8317 | CLIProxyAPI (Claude Max subscription proxy) |

### Additional Deployment Modes
- **Local development** — `python run.py api|cli|worker|mcp`
- **Windows service** — `python run.py service install|start|stop`
- **Interactive CLI** — `python run.py cli`

### Configuration
- **100+ environment variables** across LLM, API, database, messaging, autonomous, context, voice, logging
- **Hot-reload settings** — `PATCH /settings` clears LRU cache and rebuilds graphs
- **Per-thread LLM overrides** — Provider, model, temperature, thinking mode per conversation

---

## 18. Logging & Observability

### Log Profiles (11 named profiles)
| Profile | Purpose |
|---------|---------|
| `llm` | Full message arrays, provider decisions |
| `tools` | Tool call/result tracing |
| `agent` | Stream lifecycle, lock details |
| `threads` | Callable thread orchestration |
| `ticker` | TODO polling/execution |
| `triggers` | Event-driven trigger firing |
| `checkpoints` | State persistence |
| `api` | HTTP request handling |
| `sse` | Event bus publishing |
| `compactor` | Auto-compaction, token tracking |
| `all` | Full firehose |

### Log Tags
- Subsystem markers: `[LLM]`, `[STREAM]`, `[ASTREAM]`, `[CALLABLE]`, `[TICKER]`, `[WATCHDOG]`, `[TRIGGER]`
- Framing pattern: `=== START ===`, `=== END ===`, `=== ERROR ===` with thread ID, elapsed time, metrics
- Per-module overrides via `LOG_MODULES=nymeria.core.agent:DEBUG`

---

## 19. Security & Safety

### Self-Modification Guards
- Path escaping prevention (absolute path resolution + project root check)
- Write restricted to 3 directories only (tools, agents, trigger sources)
- `@tool` decorator enforcement on new tool files
- Automatic backups with rollback capability

### Skill Marketplace Scanning
- Detects dangerous patterns: curl|bash pipes, wget|bash, rm -rf /, eval base64, fork bombs
- Warnings logged before installation

### API Security
- Bearer token authentication (`NYMERIA_API_KEY`)
- CORS origin configuration
- Webhook endpoints are public (by design) with trigger-specific validation

### Autonomous Safety
- Rate limiting: 50 self-invocations per hour
- Retry cap: 3 retries before permanent failure
- Watchdog kill switches (env var + file flag)
- Thread locking prevents concurrent access

---

## 20. Key Limits & Constraints

| Feature | Limit | Configurable |
|---------|-------|:---:|
| User memories | 100 per user | No |
| Memory value | 1000 chars | No |
| Active TODOs | 50 per user | No |
| Thread notepad | 50 KB | No |
| Thread instructions | 5000 chars | No |
| System prompt override | 50,000 chars | No |
| Agent iterations | 70 (main), 50 (callable) | Yes |
| Spawn depth | 3 levels | Yes |
| Spawns per hour | 10 per parent | Yes |
| Tool reloads per turn | 3 | No |
| Concurrent autonomous tasks | 5 | Yes |
| Self-invocations per hour | 50 | Yes |
| Ticker poll interval | 5 seconds | Yes (1–60s) |
| Lock timeout | 120 seconds | Yes |
| Tool timeout | 300 seconds | Yes |
| Browser page content | 8000 chars | No |
| Consult timeout | 180 seconds | No |
| Claude Code timeout | 300 seconds | Yes |
| TODO auto-archive | 7 days | Yes (1–30) |
| Compact threshold | 80% | Yes (50–95%) |
| Graph cache | 50 entries | No |
| Attachment limit | 4 files | No |
| Discord message limit | 2000 chars | No |
| Telegram message limit | 4096 chars | No |
| Twitch message limit | 500 chars | No |
| Triggers per user | 50 | No |
| Trigger execution history | 200 per user | No |
| Outlook dedup window | 200 message IDs | No |
