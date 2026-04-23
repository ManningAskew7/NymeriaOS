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
- **Iteration limit**: 1–200 ReAct iterations per callable (default 50)
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
- **Tools**: `profile_save`, `profile_forget`, `profile_list`, `personality_set`
- **Limits**: 100 memories, 1000 chars each

### Notepad (Per-Thread Knowledge Base)
- **Scope**: Thread-local — isolated to one conversation
- **Purpose**: Thread-specific state: project context, decisions, findings, strategy, file paths
- **Survives compaction**: Automatically re-injected after context summarization
- **Tools**: `notepad_write` (append/replace), `notepad_read`, `notepad_edit` (find-and-replace), `notepad_clear`
- **Limit**: 50KB per thread
- **Used by autonomous tasks**: Ticker reads notepad for context continuity

### RAG (Nothing Gets Forgotten)
- **Every conversation turn is indexed** — User message + AI response pairs embedded into per-user vector store after each turn
- **Pre-compaction flush** — Before context is trimmed, all messages are defensively written to RAG
- **Hybrid search** — sqlite-vec vector similarity (70%) + FTS5 BM25 (30%)
- **Three chunk types**: `conversation`, `memory`, `todo` — each toggleable
- **Embedding**: OpenAI text-embedding-3-small (1536 dims), BM25-only fallback if unavailable
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

### Runtime Tool Management (`tool_search`)
- **Search** — Find tools by keyword or category (returns up to 15 results with status)
- **Enable** — Activate optional tools per-thread with TTL (30m, 2h, 6h, 24h, permanent)
- **Disable** — Non-destructively remove tools (preserves state for re-enable)
- **Status** — View thread's full tool inventory with TTL remaining

### In-Turn Hot-Loading
- **Mid-stream graph rebuild** — Enable a tool and use it in the same turn (up to 3 reloads per turn)
- Agent calls `tool_search(action="enable")` → graph ends → fresh graph compiled with new tool → agent continues in same SSE stream
- **Sliding renewal** — Re-enabling refreshes expiry; promoting to permanent upgrades classification
- **Lazy eviction** — Expired TTL tools filtered at graph-build time, no background scheduler

### MCP Server Discovery & Installation
- **`mcp_search`** — Search official MCP registry + Smithery for servers
- **`mcp_install`** — Install from Claude Desktop JSON, bare CLI command, HTTP URL, or registry ID
- Auto-discovers tools, namespaces as `mcp__<server>__<tool>`, available after reload

### Skill Discovery & Installation
- **`search_skills`** — Semantic search over installed + Anthropic marketplace skills
- **`install_skill`** — Install skill bundles from Anthropic `anthropics/skills` repo
- **Security scanning** — Detects curl|bash pipes, rm -rf, eval base64, fork bombs before install
- **Progressive disclosure** — Only name + description loaded; full body on activation
- **Per-thread control** — Enable/disable skills per thread; four scopes (thread > user > global > bundled)

---

## 4. Core Agent Architecture

### LangGraph ReAct Agent
- **Reasoning + Acting loop** — LangGraph-based ReAct pattern with configurable iteration limits (default 70, callable threads default 50, configurable 1–200)
- **Per-user graph compilation** — Graphs compiled per user/thread based on memory hash, tool set, and thread config; LRU-cached (max 50 entries)
- **Dynamic tool binding** — Tools resolved at graph-build time from core + optional + callable + MCP + skill sources
- **Multi-provider LLM support** — Anthropic (native), OpenAI, OpenRouter, local LLMs (llama.cpp, KoboldCpp, LM Studio, Ollama) via OpenAI-compatible API
- **Extended thinking** — Configurable reasoning effort (off, on, low, medium, high) with thinking block visualization
- **Model hot-switching** — Change model per-thread or globally at runtime without restart

### Streaming (SSE)
- **Real-time SSE** for chat, autonomous tasks, and trigger executions
- **Chat events**: `thinking`, `tool_call`, `tool_result`, `workspace_artifact`, `response`, `context_attached`, `compacted`, `command_result`, `tool_reload`, `error`, `done`
- **Autonomous events**: `task_started`, `thinking`, `tool_call`, `tool_result`, `workspace_artifact`, `response`, `task_completed`

### Context Management
- **Auto-compaction** (default) — Automatic summarization at configurable threshold (default 80% of model limit)
- **In-context summarization** — Agent generates its own summary, no separate LLM call
- **Pre-compaction RAG flush** — All messages indexed before trimming (nothing lost)
- **Checkpoint pruning** — Old checkpoint rows cleaned up after compaction
- **Notepad re-injection** — Thread knowledge base re-attached after compaction
- **Token tracking** — Context stats exposed via API: token count, percentage, compaction count

### Concurrency
- **Per-thread locking** — Non-blocking acquisition with timeout and lock holder metadata
- **Queued execution** — Busy threads return "queued" events with wait duration
- **User-message preemption** — User messages cancel pending autonomous tasks

---

## 5. Tool System (90+ Tools)

### Core Tools (Always Loaded)

| Category | Tools |
|----------|-------|
| **Shell & Files** | `bash_execute`, `file_read`, `file_write` |
| **Web** | `web_search` (Perplexity, 3 depth levels) |
| **Multi-Model** | `consult` (Gemini second opinion), `claude_code` (headless CLI) |
| **Memory** | `profile_save`, `profile_forget`, `profile_list`, `personality_set`, `rag_search` |
| **TODOs** | `nym_todo` (create/update with scheduling + recurrence), `nym_todo_delete`, `nym_todo_list` |
| **Notepad** | `notepad_write`, `notepad_read`, `notepad_edit`, `notepad_clear` |
| **Notifications** | `notify` (Telegram/Discord/Slack/Teams, auto mode) |
| **Self-Customization** | `tool_search`, `mcp_search`, `mcp_install`, `list_installed_skills`, `search_skills`, `install_skill`, `reload_all` |

### Optional Tool Categories

| Category | Tools | Description |
|----------|-------|-------------|
| **Browser** | 9 | Playwright with BeautifulSoup fallback; navigate, click, type, screenshot, scroll |
| **Outlook Email** | 16 | OAuth, search (KQL), send, reply, forward, drafts, categories, attachments (PDF/DOCX/Excel) |
| **Google Calendar** | 14 | OAuth, events CRUD, RSVP, free/busy, colors |
| **Twitch** | 23 | Chat, moderation, stream info, polls, predictions, clips, channel management |
| **Triggers** | 6 | Create, list, update, delete, inspect, source catalog |
| **_PRV_A/Sheets** | 9 | Google Sheets CRUD, supplier lookup, product search, lifecycle status, pricing |
| **Watchdog** | 4 | Activity feed, cross-thread dispatch, cross-thread notepad, TODO overview |
| **Thread Spawning** | 1 | Dynamic callable thread creation with full config |
| **Self-Modification** | 8 | (Legacy) Direct code editing with backups and validation |

### Dynamic Tools
- **Callable thread tools** — Any thread with `callable=True` becomes a tool
- **Custom HTTP tools** — REST API calls with templates and JSONPath extraction
- **MCP server tools** — Auto-discovered from installed MCP servers
- **Skill meta-tool** — Synthesized `Skill(name)` tool per thread

---

## 6. Autonomous Operation

### TODO-Based Scheduling
- **Relative scheduling**: "30s", "5m", "2h", "1d" from now
- **Absolute scheduling**: ISO 8601 datetime
- **Recurrence patterns**: 5min, 10min, 15min, 30min, hourly, daily, weekly, monthly
- **Status tracking**: pending → in_progress → done
- **Auto-purge**: Completed non-recurring TODOs archived after 7 days

### Ticker (Background Scheduler)
- **Daemon thread polling** — 5-second intervals (configurable 1–60s)
- **Parallel execution** — ThreadPoolExecutor (default 5 concurrent tasks)
- **Rate limiting** — 50 self-invocations per hour
- **Retry logic** — 3 retries before permanent failure, 10-minute backoff on iteration limits
- **Missed schedule recovery** — Detects and re-executes overdue TODOs on startup
- **Event publishing** — Real-time SSE for watching autonomous work

### Watchdog Monitoring
- **Staleness detection** — Finds TODOs unchanged beyond configurable threshold
- **Nudge mechanism** — Groups stale TODOs by thread, sends prompts via `/chat`
- **External notifications** — Alerts via Telegram/Discord/Slack with elapsed time
- **Kill switches** — Environment variable or file flag to disable

---

## 7. Event-Driven Triggers (N8N-Inspired)

### Trigger Sources (6 types, plugin auto-registration)
| Source | Type | Description |
|--------|------|-------------|
| **Webhook** | Push | HTTP POST from external services (IFTTT, Zapier, n8n, Tasker) with optional secret |
| **Outlook Email** | Poll | Microsoft 365 inbox monitoring with triple deduplication, folder/sender/subject/importance filters, attachment pass-through |
| **RSS/Atom** | Poll | Feed monitoring (blogs, YouTube, GitHub, Reddit) |
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
- **Exposed tools**: `nymeria_chat`, `nymeria_profile_save/list/forget`, `nymeria_todo_add/list/complete/update/delete/status`, `nymeria_rag_search`, `nymeria_thread_history`
- **Per-user isolation** on all operations

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

### Slack & Teams
- Channel monitoring via trigger sources
- Outbound webhook notifications

---

## 12. Desktop Application (Tauri 2.x + Svelte 5)

### Chat
- Streaming token-by-token rendering, thinking block visualization, tool call cards
- Tool reload indicators, context status bar, file attachments, workspace artifacts

### Thread Management
- Unlimited threads with folders, pinning, search/filter
- Per-thread configuration panel: instructions, system prompt, tools, LLM, skills, callable settings

### Tool Management
- Built-in tool browser by category, custom HTTP/MCP tool creation
- MCP server panel with install wizard and auto-discovery
- Tool testing, import/export, default tool configuration

### Dashboard
- Activity feed, TODO management with scheduling and recurrence
- Notification center, autonomous task streaming, connection health

### Triggers
- Setup wizard, trigger feed with enable/disable/test, execution history

### Skills
- Installed skills panel, marketplace search and install, global defaults

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
| **RAG** | Settings, stats, reindex, delete index |
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
- **Windows service** — `python run.py service install|start|stop`
- **Interactive CLI** — `python run.py cli`

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
`[LLM]`, `[STREAM]`, `[ASTREAM]`, `[CALLABLE]`, `[TICKER]`, `[WATCHDOG]`, `[TRIGGER]`

Framing: `=== START ===` / `=== END ===` / `=== ERROR ===` with thread ID, elapsed time, metrics. Per-module overrides via `LOG_MODULES`.

---

## 18. Key Limits & Constraints

| Feature | Limit | Configurable |
|---------|-------|:---:|
| User memories (profile) | 100 per user, 1000 chars each | No |
| Thread notepad | 50 KB | No |
| Thread instructions | 5000 chars | No |
| System prompt override | 50,000 chars | No |
| Agent iterations | 70 (main), 50 (callable) | Yes (1–200) |
| Spawn depth | 3 levels | Yes |
| Spawns per hour | 10 per parent | Yes |
| Tool reloads per turn | 3 | No |
| Concurrent autonomous tasks | 5 | Yes |
| Self-invocations per hour | 50 | Yes |
| Ticker poll interval | 5 seconds | Yes (1–60s) |
| Lock timeout | 120 seconds | Yes |
| Tool timeout | 300 seconds | Yes |
| Compact threshold | 80% | Yes (50–95%) |
| Graph cache | 50 entries | No |
| Active TODOs | 50 per user | No |
| TODO auto-archive | 7 days | Yes (1–30) |
| Triggers per user | 50 | No |
| Trigger execution history | 200 per user | No |
| Attachments per message | 4 files | No |
| Discord messages | 2000 chars | No |
| Telegram messages | 4096 chars | No |
| Twitch messages | 500 chars | No |
