# Nymeria Architecture

## Overview

Nymeria wraps LangGraph's ReAct (Reasoning + Acting) agent pattern with additional features for personal assistant use cases:

- **Persistent Memory**: Remembers user facts and preferences across conversations
- **Self-Modification**: Can add, remove, and fix its own tools at runtime
- **Autonomous Operation**: Operates 24/7 via scheduled TODOs (with `scheduled_for` parameter)
- **Multi-User Support**: Isolated profiles per user with thread-safe operations

```
┌─────────────────────────────────────────────────────────────────────┐
│                         USER INPUT                                   │
│                    (CLI or REST API)                                 │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       NymeriaAgent                                   │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │                 Context Injection                              │  │
│  │   • Time context (configured user timezone)                   │  │
│  │   • User memories from profile                                │  │
│  │   • Personality preferences                                    │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                           │                                          │
│                           ▼                                          │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │          LangGraph ReAct Loop (500-call safety budget)        │  │
│  │                                                                │  │
│  │   ┌─────────┐    ┌─────────┐    ┌─────────────────┐          │  │
│  │   │   LLM   │───▶│ Router  │───▶│     Tools       │          │  │
│  │   │ (Think) │    │         │    │ (core + optional│          │  │
│  │   └─────────┘    └─────────┘    └────────┬────────┘          │  │
│  │        ▲                                  │                   │  │
│  │        └──────────────────────────────────┘                   │  │
│  │              (loop until done)                                │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                           │                                          │
│                           ▼                                          │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │              Auto-Compact Context Management                   │  │
│  │      (Summarizes at configured context threshold, saves memory) │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                           │                                          │
│                           ▼                                          │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │                    Audit Logging                               │  │
│  │            (All tool executions logged)                        │  │
│  └───────────────────────────────────────────────────────────────┘  │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
           ┌───────────────┼───────────────┬───────────────┐
           ▼               ▼               ▼               ▼
┌────────────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ Conversation       │ │ User Profile │ │  TODO-based  │ │    Ticker    │
│ Checkpointer       │ │   Manager    │ │  Scheduling  │ │  (Polling)   │
│ SQLite local       │ │  (Memories)  │ │ (schedules)  │ │  (5s loop)   │
│ PostgreSQL Docker  │ │              │ │              │ │              │
└────────────────────┘ └──────────────┘ └──────────────┘ └──────────────┘
```

---

## Core Components

### 1. NymeriaAgent (`nymeria/core/agent.py`)

Main orchestrator that:
- Loads configuration from environment variables
- Initializes the LangGraph execution graph
- Manages tool registration and hot-reloading
- Injects time context and user memories into each conversation
- Provides `chat()` and `astream()` methods
- Caches user-specific graphs (rebuilds when memories change)

```python
from nymeria.tools import get_all_tools
agent = NymeriaAgent(tools=get_all_tools())
response = agent.chat("Hello", thread_id="user123", user_id="default")
```

**Key Features:**
- Memory hash caching: Graphs are rebuilt only when user memories or thread config change
- Time context injection: Every message includes current time in the configured `USER_TIMEZONE`
- Dynamic callable tool sync: `sync_agent_tools()` refreshes callable-thread-backed tools when thread configs change
- Per-thread configuration: Custom instructions, tool overrides, and LLM settings per thread

---

### 1.1 Core Module Structure

The `nymeria/core/` directory contains modular components extracted for maintainability:

| Module | Purpose |
|--------|---------|
| `agent.py` | Main NymeriaAgent class (orchestrator) |
| `agent_history.py` | Conversation-history projection for API/frontend clients, including checkpoint timestamp recovery, internal-message filtering, reasoning/tool-step rendering, and attachment metadata |
| `agent_streaming.py` | `GraphStreamProcessor` for converting LangGraph stream events into Nymeria SSE chunks, plus helpers for classifying streamed model chunks and deduplicating reasoning deltas |
| `command_service.py` | Central slash-command registry, path metadata catalog, alias resolver, direct backend adapter, and markdown dispatcher used by REST, desktop/mobile command input, bots, and the `slash_command` agent tool |
| `thread_config.py` | Per-thread config (custom instructions, disabled/enabled tools, LLM overrides, callable thread settings) |
| `thread_metadata.py` | Server-authoritative thread metadata (titles, pins, platform). Replaces frontend-only localStorage titles. |
| `thread_deletion.py` | Cascade deletion for a thread — removes checkpoints, TODOs, triggers bound to the thread, callable-thread bindings, notepad, and activity entries in one transaction so `DELETE /threads/{id}` doesn't leave orphans. |
| `thread_agent_executor.py` | Delegates tasks to callable threads through `stream_and_collect()` over the sync `iter_agent_astream()` bridge. Publishes live SSE events. |
| `stream_bridge.py` | Lets synchronous autonomous/callable workers consume `NymeriaAgent.astream()` through one process-local asyncio loop, while provider HTTP pools and async graph caches remain local to whichever event loop owns the invocation. |
| `trigger_manager.py` | Event-driven trigger coordination, fires agent prompts or direct actions |
| `activity_log.py` | Per-thread activity feed with time-based retention |
| `prompts.py` | System prompt templates, mode-specific rules, time context generation |
| `time_utils.py` | Shared time parsing utilities (durations, schedules, deadlines) |
| `todo_constants.py` | Single source of truth for TODO display (status icons, priority markers) |
| `keyed_locks.py` | Shared keyed `RLock` factory used by JSON-backed stores to serialize per-user or per-thread file writes |
| `http_policy.py` | HTTP tool egress policy and redacted HTTP audit-event logging |

---

### 2. UserProfileManager (`nymeria/core/user_profile.py`)

Thread-safe persistent storage for user memories and preferences.

**Components:**
- `Memory`: Individual memory with key, value, timestamps, access count
- `UserProfile`: User's complete profile (memories, personality_overrides)
- `UserProfileManager`: Disk persistence with atomic updates

**Storage:** `data/users/{user_id}/profile.json`

**Limits:**
- 100 memories per user
- 1000 characters per memory value

**Key Methods:**
```python
manager = UserProfileManager(data_path)
profile = manager.get_profile("user123")

# Thread-safe atomic updates
with manager.atomic_update("user123") as profile:
    profile.add_memory("name", "Alex")
```

**How Memories Work:**
1. User says "My name is Alex"
2. LLM calls `memory_add(scope="global", key="name", content="Alex")`
3. Profile saved to disk
4. Next conversation: memories automatically included in system prompt
5. LLM knows memories without needing to "recall" them

---

### 3. Callable Threads (`nymeria/agents/tool_factory.py`, `core/thread_agent_executor.py`)

Any thread with `callable=True` in its thread config becomes a directly invocable tool. This replaces the older separate sub-agent registry approach.

**How it works:**
1. User marks a thread as callable via thread settings and gives it a name (e.g., "ResearchAgent")
2. `tool_factory.create_callable_thread_tool()` creates a LangChain `BaseTool` wrapper
3. The tool appears as `ResearchAgent(task="...")` in other threads' tool lists
4. Callable threads are configured entirely through thread config (system prompt, LLM overrides, enabled/disabled tools)

**Title/Name Sync:**
- The thread's sidebar title always equals its `callable_name` (the LLM-visible tool name)
- Renaming a callable thread in the sidebar updates `callable_name` in thread config and rebuilds the tool registry via `sync_agent_tools()`
- Name collisions with core tools or other callables are rejected

Built-in callable behavior is configured through thread config and tool-factory wiring, rather than a separate hard-coded callable registry in this doc layer.

**Execution:**
```
User: "Research Python tutorials"
    ↓
LLM calls: ResearchAgent(task="Find Python tutorials")
    ↓
thread_agent_executor delegates to callable thread's NymeriaAgent.astream()
through stream_and_collect() / iter_agent_astream()
    ↓
Callable thread runs with its own system prompt, tools, and LLM settings
    ↓
SSE events published live (thinking, tool_call, tool_result, response)
    ↓
Result returned to parent thread
```

---

### 4. TODO-based Scheduling (`nymeria/core/todo_*.py`, `ticker.py`)

Manages autonomous operation for 24/7 functionality through the **TODO system** with scheduled items.

**Key Concepts:**
- TODOs can have a `scheduled_for` datetime for future execution
- **Durable**: Scheduled TODOs persist across restarts (SQLite via `todo_schedule_db.py`)
- Global polling ticker instead of threading.Timer
- Parallel execution via thread pool (`MAX_CONCURRENT_AUTONOMOUS`, default 5)

**Components:**
- `TodoManager`: Manages user TODO lists with atomic updates (JSON files in `data/todos/`)
- `TodoScheduleDB`: SQLite index for efficient polling and cross-process active-execution markers (not source of truth - mirrors JSON)
- `Ticker`: Global daemon thread that executes due scheduled TODOs and removes completed TODOs older than `TODO_AUTO_ARCHIVE_DAYS`
- `EventBus`: Pub/sub system for streaming autonomous events to frontend (see Section 4.1)

**Scheduled TODO Flow:**
```
TODO with scheduled_for → ticker polls → executes when due → streams via EventBus → completes/reschedules
```

**Workflow:**
```
User creates TODO with scheduled time
    ↓
LLM calls: todo_add("Check inbox", scheduled_for="30m")
    ↓
TodoManager:
    1. Creates TODO item with scheduled_for datetime (JSON file)
    2. Syncs to TodoScheduleDB (SQLite index)
    ↓
[Ticker polls every 5 seconds (configurable via TICKER_POLL_INTERVAL)...]
    ↓
Ticker finds due scheduled TODO:
    1. Claims an active-execution marker in TodoScheduleDB (skips duplicate claims)
    2. Reads the JSON TODO and marks it IN_PROGRESS
    3. Starts the agent via the async streaming bridge
    4. Publishes "task_started" after the first stream chunk is available
    5. Streams tool_reload, tool_call, tool_result, thinking, response, and other supported events to frontend
    6. If recurring: calculates next execution, reschedules
    7. If not recurring: clears schedule
    8. Publishes "task_completed" event
    9. Auto-compacts context if needed (or trims if using legacy sliding window)
    10. Clears the active-execution marker when the run exits
```

Autonomous executions append `AUTONOMOUS_MODE_RULES` from `core/prompts.py`.
Those rules make scheduled TODOs, watchdog nudges, triggers, and autonomous
callable-thread wake-ups user-visible work: the agent must complete/update/delete
the TODO when appropriate, or briefly explain what it checked and why no action
was taken. Callable threads with a custom system prompt still keep their focused
context (no profile/TODO injection), but autonomous wake-ups receive the same
autonomous rules.

**Recovery on Restart:**
When Nymeria starts, the Ticker:
1. Rebuilds schedule index from TODO JSON files (`rebuild_from_todos()`)
2. Recovers any missed scheduled TODOs that were due during downtime

---

### 4.2 Watchdog (thin client, `nymeria/triggers/watchdog_worker.py`)

The **Watchdog** nudges Nymeria when active TODOs haven't been touched for a while. It runs as a **thin-client process** alongside (not inside) the API — same structural pattern as the Discord and Telegram bots.

**Why a separate process?** The old watchdog lived inside `NymeriaAgent.__init__`, which meant every container that built an agent (both `api` and `worker` in Docker) started its own watchdog — two instances scanning the same TODOs, with in-process callbacks via `get_watchdog()` to clear nudge state. Running it as a sibling service eliminates the duplication and the global coupling.

**Flow:**
```
watchdog container (run.py watchdog)
    │
    ├─ every watchdog_interval_minutes:
    │     GET /todos/users  → list user IDs
    │     GET /todos with X-Nymeria-Act-As: X  → fetch each user's TODOs
    │     filter: active + not recurring + scheduled_for not in future
    │            + updated_at older than todo_staleness_minutes
    │     group stale TODOs by thread_id
    │
    └─ per thread:
          POST /chat  { is_self_invoke: true, trigger_override: "watchdog" }
          ↓
          API sets holder=autonomous, routes through autonomous prompt,
          publishes task_started/tool_call/.../task_completed to
          /autonomous/stream subscribers
          ↓
          Watchdog also fires a Telegram/Discord/Slack notification
          via `core.notification_dispatch.send_external_notifications()`.
```

**State:** The worker keeps per-(user, todo_id) "last nudge time" and "last-seen updated_at" in memory. Nudge eligibility resets naturally on the next poll whenever `updated_at > last_seen` — no cross-process callbacks required. State is lost on restart, which is fine: stale TODOs will simply re-nudge on the next cycle.

**Kill switches:** `NYMERIA_WATCHDOG_DISABLED=1` env var, or a `{data_dir}/flags/watchdog-off` file (persistent across container restarts).

---

### 4.3 Docker Runtime Images

Docker Compose uses two Nymeria application image families instead of one
workstation image for every process:

- `nymeria-full:local` (`Dockerfile.full`) runs the API, worker, and Twitch
  bot. These services own a `NymeriaAgent` or execute autonomous agent work, so
  they keep the Kali tools, browser runtime, and CLI-oriented environment that
  shell-capable tools may call.
- `nymeria-slim:local` (`Dockerfile.slim`) runs Watchdog, Discord, Telegram,
  Slack, Matrix, Mattermost, Zulip, Rocket.Chat, Signal, and MCP. Those services are
  HTTP thin clients over the API and do not execute local agent tools, so they
  omit Kali packages, Playwright browsers, Node.js, Claude Code CLI, and
  dev/test dependencies. WhatsApp Cloud API, Messenger Platform, Instagram
  Messaging, Webex Messaging, Microsoft Teams Bot Framework, Google Chat, and
  LINE webhooks are hosted by the API service rather than separate bot
  containers.

Both images install the same runtime Python requirements so thin-client imports
stay simple; `requirements-dev.txt` is deliberately host/CI-only.

---

### 4.1 Event Bus & Autonomous Streaming (`nymeria/core/event_bus.py`)

The **EventBus** enables real-time streaming of autonomous task execution to connected frontend clients.

**Purpose:**
- Decouples the Ticker from frontend SSE connections
- Enables multiple simultaneous frontend clients
- Provides real-time insight into autonomous operations

**Architecture:**
```
Ticker / watchdog / trigger / self-invoke executes autonomous work
    ↓
publish_autonomous_event(type, thread_id, task_id, data)
    ↓
EventBus.publish()
    ↓
RedisEventBus publishes to Redis when Redis is enabled
    ↓
API container Redis subscriber receives the message and enqueues it locally
    ↓
/autonomous/stream SSE endpoint reads from queue → sends to frontend
    ↓
Desktop fetch stream parses frames and updates the active chat thread
```

**Event Types:**
| Event | When Published | Data |
|-------|---------------|------|
| `task_started` | TODO execution begins | `prompt`, `todo_id` |
| `thinking` | LLM reasoning | `content` |
| `tool_call_delta` | Model is preparing tool-call arguments | none |
| `tool_call` | Tool invocation | `id`, `name`, `args` |
| `tool_result` | Tool returns | `id`, `name`, `result` |
| `workspace_artifact` | Tool generated an attachable workspace file | `path`, `name`, `mime_type`, `size_bytes` |
| `tool_reload` | Tool registry was reloaded mid-turn | `tools`, `ttl`, `ttl_seconds`, `source`, `skill_name`, `reason` |
| `response` | Final response chunks | `content` |
| `context_attached` / `compacting` / `compacted` | Context-management progress | summary/progress fields |
| `iteration_limit` | Turn safety stop | limit and repeated-tool metadata |
| `task_completed` | Execution finishes | `notify`, `content`, `summary` |

**Subscriber Management:**
- Per-subscriber `Queue` (maxsize=100)
- Non-blocking publish (drops if queue full)
- Subscribers identified by UUID
- Docker Compose Redis requires `REDIS_PASSWORD`; application containers connect through an authenticated `REDIS_URL`, and startup logs redact URL credentials
- Events logged when no subscribers are connected, when Redis receives a message, when the API enqueues/receives/yields a message, and when queues drop events
- High-volume events (`response`, `thinking`, `tool_call_delta`) log the first few chunks and then sample, so long runs remain debuggable without flooding logs

**SSE Endpoint (`/autonomous/stream`):**
```python
GET /autonomous/stream?user_id={user_id}&client_id={client_id}
Authorization: Bearer {key}
```
- Desktop and mobile open the stream with `fetch()` + `ReadableStream`, `Accept: text/event-stream`, Bearer auth headers, and a per-session `client_id`. The legacy `api_key` query parameter remains available for older EventSource-style clients.
- Sends heartbeat every 1 second when idle
- Filters events to the authenticated user unless an admin caller uses `X-Nymeria-Act-As`; `client_id` suppresses same-client sync echoes
- Unsubscribes on client disconnect
- Desktop and mobile reconnect accidental drops, stream ends, HTTP errors, and idle timeouts; on reconnect they refresh the current thread history/context and sync the thread list to catch missed events. Mobile also uses the Capacitor Network plugin to pause stream reconnects while offline and resume when connectivity returns.

---

### 5. Context Management (Auto-Compact)

Prevents context overflow by summarizing conversations when approaching token limits.

**Modes** (`CONTEXT_MANAGEMENT` setting):
- **`auto_compact`** (default): Intelligent summarization with memory persistence
- **`sliding_window`**: Legacy mode that removes old messages
- **`none`**: No automatic management

#### Auto-Compact (Default)

When token usage reaches the threshold (default: 80% of model's context limit):

```
1. Agent generates summary (already has full context - no re-sending)
2. Agent calls `memory_add(scope="global", ...)` for important persistent facts
3. All messages cleared from thread (RemoveMessage + a single
   compaction_marker HumanMessage so the router can still read messages[-1])
4. Pre-compact checkpoint rows pruned from the checkpointer via raw SQL
   (checkpoints + checkpoint_writes + orphan checkpoint_blobs) so
   /history calls don't keep paying to hydrate history nobody references
5. Summary injected as context for continuation
6. Agent continues where it left off (auto-compact) OR
   Summary attached to user's next message (manual /compact)
```

**Key Components:**
- `TokenTracker` (`token_tracker.py`): Tracks cumulative tokens per thread
- `CompactionManager` (`agent_compaction.py`): Owns compaction policy, summary generation, message clearing, and pending-summary state. `NymeriaAgent` delegates via `self._compaction`.
- `CheckpointCleaner` (`checkpoint_cleanup.py`): shared raw-SQL checkpoint deletion and pruning for SQLite/Postgres, used by compaction and thread deletion.
- Model limits and attachment modality checks are resolved from live model metadata when available, including bare OpenAI IDs routed through CLIProxy (`gpt-5.5` -> `openai/gpt-5.5`). Static fallbacks cover known long-context and multimodal families, and fallback capability matching is exact/snapshot-aware so distinct hyphenated variants do not inherit each other's capabilities.

See [compaction-and-checkpoints.md](./compaction-and-checkpoints.md) for the end-to-end flow, the display filter's `internal_type` branches (including the `compaction_marker` edge case), and a troubleshooting playbook.

**Configuration:**
```bash
CONTEXT_MANAGEMENT=auto_compact  # auto_compact, sliding_window, or none
COMPACT_THRESHOLD=0.8            # Trigger at 80% of context limit (0.05-0.95)
COMPACT_MODEL=                   # Optional: use cheaper model for summarization
```

**Manual Compaction:**
- Chat: Send `/compact` command
- API: `POST /threads/{thread_id}/compact`
- CLI: `/compact` command

**Why Auto-Compact is Better:**
- Preserves important context via intelligent summarization
- Saves persistent facts to memory (survives across all threads)
- Agent explicitly lists files to read for continuity
- No abrupt loss of recent context

#### Sliding Window (Legacy)

Simple removal of oldest messages:
```bash
CONTEXT_MANAGEMENT=sliding_window
SLIDING_WINDOW_CYCLES=5  # Keep last 5 conversation cycles
```

---

### 6. BackupManager (`nymeria/core/backup.py`)

Safe file backup system for self-modification.

**Features:**
- Timestamped backups: `{filename}_{YYYYMMDD_HHMMSS}{ext}`
- Mirrors project directory structure
- Keeps max 10 backups per file
- Auto-cleanup of old backups
- Rollback capability

**Storage:** `data/backups/{relative_path_to_file}`

---

### 7. Tool System (`nymeria/tools/`)

Tools use the `@tool` decorator from `langchain_core.tools`. The system has three tiers:

| Tier | Count | Description |
|------|-------|-------------|
| **Core (`ALL_TOOLS`)** | Current code-defined set | Static tools always loaded |
| **Callable Thread Tools** | Dynamic | One per callable thread, generated by `tool_factory.py` |
| **Optional (`OPTIONAL_TOOLS`)** | Current code-defined set | Per-thread enabled tools loaded on demand |

**Core tools by category:**

| Category | Tools |
|----------|-------|
| Core System | file_read, file_write, web_search, consult, slash_command |
| Profile & RAG | memory_add, memory_edit, memory_read, personality_set, rag_search |
| TODO | nym_todo, nym_todo_delete, nym_todo_list |
| Runtime / utility | consult, notify and other currently registered core utilities |

**Callable thread tools** (generated by `agents/tool_factory.py`):
- Any thread marked `callable=True` becomes a tool (e.g., `ResearchAgent(task="...")`)
- Tools are synced via `agent.sync_agent_tools()` and added per-graph in `_build_graph_with_prompt()`
- Built-in callables: BrowserAgent, OutlookAgent, CalendarAgent, SelfModifyAgent

**Per-thread tool filtering pipeline:**
1. Start with core tools + callable thread tools
2. Filter by user preferences and `TOOL_METADATA`
3. Remove any in `ThreadConfig.disabled_tools`
4. Add any from `OPTIONAL_TOOLS` listed in `ThreadConfig.enabled_tools`

See [Tools Reference](./tools.md) for detailed documentation.

---

### 8. Streaming Implementation

The agent has one streaming implementation: `NymeriaAgent.astream()`.

**`astream()` (Asynchronous)**
- Uses `graph.astream_events()` with `version="v2"`
- Delegates event conversion to `core/agent_streaming.py::GraphStreamProcessor`,
  which owns tool-start/tool-end events, provider reasoning/text chunk
  classification, inline `<think>` stripping, cancellation chunks, diagnostic
  counters, and model-end fallback emission. `astream()` remains the
  orchestrator for lock handling, input preparation, in-turn tool reloads,
  compaction, token tracking, and RAG indexing.
- The ReAct agent node has an async implementation that consumes the model via
  `llm_with_tools.astream()` and merges the chunks back into the final
  `AIMessage`. This is what makes `on_chat_model_stream` provider-token events
  available to regular chat and autonomous callers instead of batching one full
  response per LLM turn.
- Transient provider or transport failures are retried with exponential backoff
  only before a model chunk has been emitted, so visible streamed output and
  downstream tool side effects are not duplicated.
- Captures complete tool call information via `on_tool_start` events
- Returns tool calls with full arguments
- Used by FastAPI for SSE responses to desktop UI
- Also used by the CLI, scheduled TODOs, triggers, callable-thread execution, and spawned-thread dispatch through `core/stream_bridge.py`; autonomous callers share `stream_and_collect()` for response collection, error propagation, and iteration-limit bookkeeping while still publishing caller-specific events. Sync callers share a process-local bridge event loop so concurrent callable threads do not create short-lived event loops. Async graph caches and provider SDK HTTP pools are keyed by their owning loop, so FastAPI-loop chat and bridge-loop callable calls do not share loop-bound transports.

**Why these stream modes?**

LangGraph's `stream_mode="messages"` doesn't include tool arguments in streaming chunks - only the tool name and ID are available until after execution. `astream_events` provides complete tool arguments:

- `astream_events`: `on_tool_start` event with complete input arguments

This enables the desktop UI to display tool call arguments in real time for both interactive and autonomous modes.

**Content Block Classification**

Anthropic models produce typed content blocks: `thinking` (internal reasoning), `text` (preamble/response), `tool_use`, `redacted_thinking`, and `signature`. OpenAI-compatible providers may also expose visible assistant text as bare string content blocks. `astream()` iterates content blocks in order and emits correctly typed SSE events:

- `thinking` blocks → `type: "thinking"` events (rendered as collapsible ThinkingBlock)
- `text` blocks and bare string blocks → `type: "response"` events (rendered as inline markdown)
- `tool_use` blocks → `type: "tool_call"` events (rendered as ToolCallCard)

For OpenRouter/OpenAI models (string content, no typed blocks), all text is emitted as `type: "response"` events. This includes pre-tool commentary/preamble, which should appear before the `tool_call` event rather than inside the thinking dropdown.

The `get_conversation_history()` method (used for page refresh/checkpoint rebuild) applies the same classification: it iterates through stored content blocks in order, preserving interleaved thinking between tool calls.

**Callable Thread Streaming**

Callable thread invocations stream supported agent events (including thinking, tool calls/results, workspace artifacts, tool reloads, and responses) to the event bus in real-time via `thread_agent_executor.py`, so the frontend can display callable thread activity as it happens. Blocking `mode="ask"` calls run through the shared sync stream bridge loop rather than creating a fresh event loop per invocation. Async graph cache keys include the owning event loop, and `vendor/react_agent/providers.py` keeps Anthropic plus OpenAI-compatible HTTP pools loop-local. Parent→child invocations are tracked via `_active_callable_invocations` for cascading abort support. This dict is intentionally process-local — a restart kills all in-flight invocations, so an empty dict is the correct post-restart state.

---

### 9. Triggers (`nymeria/triggers/`)

Input interfaces and event-driven adapters that route messages to the agent:

**CLI** (`cli.py`):
- Interactive terminal interface
- Rich formatting for responses
- Slash-command registry merges local interactive commands with backend global
  command proxies. Local commands include `/history`, `/clear`, `/exit`,
  `/theme`, clipboard helpers, thread navigation, and chat-control flows such
  as `/compact`; global paths such as `/memory save`, `/tools core`, `/context`,
  `/status`, `/model`, `/config`, `/env`, `/todos`, and `/notepad` execute
  through the backend command service.

**API** (`api.py`):
- FastAPI server with SSE streaming
- Bearer token authentication
- Low-coupling route slices are being extracted under `nymeria/api/routers/`
  while `triggers/api.py` remains the app factory and owns shared dependencies;
  extracted routers now include System, devices, workspace, RAG, user memory,
  user tool preferences, Skills, voice, Agent Threads, activity/notifications,
  TODO dashboard, autonomous stream, custom tools, classic tool
  discovery/default/callable routes, unified tools, MCP server management,
  settings/model catalog, thread config/callable-team routes, command routes,
  and Chat SSE.
- Key endpoints: `/chat` (SSE), `/commands`, `/commands/execute`, `/autonomous/stream`, `/threads`, `/todos`, `/tools`, `/agents/threads`, `/triggers`
- Thread metadata management: `PATCH /threads/{id}/metadata` syncs titles, pins, and platform across surfaces. Renaming a callable thread also updates its `callable_name` and rebuilds the tool registry.
- CRUD for threads, TODOs, custom tools, callable threads, and triggers
- Hot-reload settings via `PATCH /settings` (clears `@lru_cache`, rebuilds agent graphs)

**Shared SSE Consumer** (`sse_consumer.py`):
- `SSEEventHandler` protocol: platform handlers implement rendering callbacks
- `dispatch_event()` routes one event dict and returns the updated tool-call count
- `consume_sse_stream()` drives a full async event stream through the dispatcher
- `parse_attach_paths()` extracts `[attach:/path]` tags from tool output
- Centralises flush-before-status discipline, tool counting, and field extraction

**Shared Bot Helpers** (`bot_helpers.py`):
- Bot-neutral formatting for token counts and context usage bars
- Shared bot formatting and identity helpers; global slash-command metadata and execution live in `core/command_service.py`
- Cached platform-user resolution for thin clients that resolve linked platform identities through the API

**Discord Bot** (`discord_bot.py`):
- Gateway (WebSocket) or webhook mode
- Configurable respond mode: `mention` (only @Nymeria) or `all`

**Slack Bot** (`slack_bot.py`):
- Socket Mode thin client using Slack Bolt
- DMs always respond; channels respond to app mentions by default
- Supports linked-user enforcement, chat-app bind codes, thread-aware replies, and SSE streaming through the API

**Matrix Bot** (`matrix_bot.py`):
- Client-Server API sync-loop thin client
- Performs baseline sync before processing new room events
- Supports linked-user enforcement, chat-app bind codes, mention/free-room gating, plaintext replies, and SSE streaming through the API

**Mattermost Bot** (`mattermost_bot.py`):
- WebSocket thin client using Mattermost `/api/v4/websocket` plus REST posting
- DMs always respond; channels respond to `@bot` mentions by default
- Supports linked-user enforcement, chat-app bind codes, thread-aware replies, inbound dedupe, and SSE streaming through the API

**Zulip Bot** (`zulip_bot.py`):
- Events API long-poll thin client using Zulip `/api/v1/register` and `/events`
- Direct messages always respond; channels respond to bot mentions by default
- Supports linked-user enforcement, chat-app bind codes, stream-topic routing, queue re-registration, and SSE streaming through the API

**Rocket.Chat Bot** (`rocketchat_bot.py`):
- Realtime thin client using Rocket.Chat `stream-room-messages` plus REST posting
- DMs always respond; rooms respond to `@bot` mentions by default
- Supports linked-user enforcement, chat-app bind codes, thread-aware replies, inbound dedupe, and SSE streaming through the API

**Signal Bot** (`signal_bot.py`):
- Thin client for a user-managed `signal-cli-rest-api` daemon in JSON-RPC/SSE mode
- Direct messages always respond; groups respond to Signal mentions by default
- Supports linked-user enforcement, chat-app bind codes, native Signal thread IDs, inbound dedupe, and SSE streaming through the API

**WhatsApp Bot** (`api/routers/whatsapp_bot.py`, `whatsapp_bot.py`):
- API-hosted WhatsApp Business Cloud webhook client; no standalone polling process
- Handles Meta webhook challenge/signature verification and sends replies through the Graph messages endpoint
- Supports linked-user enforcement, chat-app bind codes, inbound dedupe, direct-chat thread IDs, and SSE streaming through the in-process agent

**Messenger Bot** (`api/routers/messenger_bot.py`, `messenger_bot.py`):
- API-hosted Meta Messenger webhook client; no standalone polling process
- Handles Meta webhook challenge/signature verification and sends replies through the Messenger Send API
- Supports linked-user enforcement, Page-scoped chat-app bind codes, inbound dedupe, direct-chat thread IDs, and SSE streaming through the in-process agent

**Instagram Bot** (`api/routers/instagram_bot.py`, `instagram_bot.py`):
- API-hosted Meta Instagram Messaging webhook client; no standalone polling process
- Handles Meta webhook challenge/signature verification and sends replies through the Instagram Messaging API
- Supports linked-user enforcement, account-scoped chat-app bind codes, inbound dedupe, direct-chat thread IDs, and SSE streaming through the in-process agent

**Webex Bot** (`api/routers/webex_bot.py`, `webex_bot.py`):
- API-hosted Webex Messaging webhook client; no standalone polling process
- Verifies optional `X-Spark-Signature`, fetches message details through Webex, and sends replies through `POST /messages`
- Supports linked-user enforcement, chat-app bind codes, self-message filtering, direct/group thread IDs, and SSE streaming through the in-process agent

**Microsoft Teams Bot** (`api/routers/teams_bot.py`, `teams_bot.py`):
- API-hosted Teams Bot Framework webhook client; no standalone polling process
- Validates Bot Framework bearer tokens when enabled and replies through the Bot Connector REST API
- Supports linked-user enforcement, chat-app bind codes, mention-gated group/channel routing, native Teams thread IDs, inbound dedupe, and SSE streaming through the in-process agent

**Google Chat Bot** (`api/routers/google_chat_bot.py`, `google_chat_bot.py`):
- API-hosted Google Chat HTTPS webhook client; no standalone polling process
- Validates Google Chat bearer tokens when enabled and replies through the Google Chat REST API with app authentication
- Supports linked-user enforcement, chat-app bind codes, mention-gated space/group routing, native Google Chat thread IDs, inbound dedupe, and SSE streaming through the in-process agent

**LINE Bot** (`api/routers/line_bot.py`, `line_bot.py`):
- API-hosted LINE Messaging API webhook client; no standalone polling process
- Validates raw-body `x-line-signature` HMACs when enabled and replies through LINE push messages
- Supports linked-user enforcement, chat-app bind codes, mention-gated group/room routing, native LINE thread IDs, inbound dedupe, and SSE streaming through the in-process agent

**Event-Driven Trigger Sources** (`triggers/sources/`):
- `base.py` — Abstract `BaseTriggerSource` with rich metadata (category, icon, setup_guide, template_variables, example_config, requires_auth, get_sample_event())
- `webhook_source.py` — Push-based incoming webhook with file-backed queue persistence
- `outlook_email_source.py` — Polls Outlook inbox via Graph API, supports sender/subject/importance filters
- `rss_source.py` — Polls RSS/Atom feeds, deduplicates via rolling seen_ids window
- `http_poll_source.py` — Generic URL monitoring with change/status/contains/always fire modes
- `slack_source.py` — Polls Slack conversations.history API for new channel messages
- `teams_source.py` — Polls Microsoft Graph API for Teams channel messages
- Sources auto-register via `register_source()` and are discovered by `list_sources()`
- Managed by `core/trigger_manager.py` which coordinates source lifecycle, health tracking (healthy/degraded/failing with exponential backoff), condition filtering (AND logic), and execution history logging

---

### 10. Configuration (`nymeria/config/`)

- `settings.py`: Pydantic settings from environment variables
- `soul.md`: System prompt defining Nymeria's personality and capabilities
- `self_agent_prompt.md`: Specialized prompt for self-modification flows

---

## Supervised & Delegated Execution: `/goal` and `/orchestrate`

Two slash commands turn a regular thread into a long-running, structured-work
mode. They are command-specific chat-stream lifecycles that bind internal
Skill Kits as part of their setup, then rewrite the user's message into a
kickoff prompt the agent acts on. They are inverse of each other in
who-does-what:

| | `/orchestrate` (delegated) | `/goal` (supervised) |
|---|---|---|
| User's thread X is the… | **orchestrator** | **worker** |
| Helper thread is the… | **worker** (forked from X via `branch_thread`) | **supervisor** (freshly spawned, no inherited context) |
| Owns the task list | X | X |
| Authority to mark "done" | X (self-managed) | Helper thread (structurally enforced) |
| Helper lifetime | `temporary` (idle-deletes) | `temporary` (idle-deletes) |
| Persisted state | None — agent self-manages via `nym_todo` | `Goal` record (`data/goals/{user}.json`) |

### `/orchestrate` — prompt-driven delegation

- Implementation: a single SKILL.md (`nymeria/skills_bundled/orchestrate/`)
  plus the slash-command intercept in `api/routers/chat.py`.
- Required tools bound on activation: `spawn_thread`, `nym_todo`,
  `nym_todo_list`, `tool_search`.
- Workers are spawned via `spawn_thread(mode="branched",
  lifetime="temporary")` so they inherit X's context up to the spawn point.
- No framework enforcement — the orchestrator persona in the kit body tells
  the agent how to decompose, delegate, and judge. Extensible to a swarm
  (multiple parallel workers) for free, since `spawn_thread` doesn't cap it.

### `/goal` — framework-enforced supervision

The structural novelty: **the worker thread cannot mark its own tasks
complete.** Authority is held by the supervisor thread, enforced via three
layers of defence-in-depth:

1. **Tool binding** — the `goal-worker` kit binds `propose_task` and
   `request_review` only; `mark_task_done` is in the `goal-supervisor` kit.
2. **Runtime authority check** — `mark_task_done` itself verifies
   `goal_manager.can_authority(user_id, goal_id, thread_id)` and refuses
   if the calling thread isn't the recorded supervisor.
3. **`nym_todo` lock** — TODO items carry an optional `goal_id`; the
   done-transition path in `nym_todo` refuses for any thread that isn't
   the supervisor, catching attempts that bypass the goal tools.

Lifecycle (`pending_approval` → `active` → `done` / `paused` / `cleared` /
`budget_limited`):

1. User runs `/goal <objective>` on thread X. The Goal record is created
   in `pending_approval` and the `goal-worker` kit is activated on X.
2. The worker decomposes the objective into tasks via `propose_task`
   (each task has a `description` and a verifiable `criterion`).
3. User runs `/goal approve`. A fresh supervisor thread S is spawned
   (`mode="fresh"`, `lifetime="temporary"`, `make_callable=True`) with the
   `goal-supervisor` kit activated. The Goal transitions to `active` and
   records `helper_thread_id=S`.
4. Worker executes tasks one at a time. When it believes a task is done,
   it calls `request_review(task_id, summary, evidence)`, which marks the
   task `awaiting_review`, invokes S via `thread_agent_executor.invoke`,
   and **blocks for S's verdict**.
5. S verifies independently (it can read files, run tests, web_search,
   enable more tools) and either calls `mark_task_done` (approve) or
   `provide_review_feedback` (refine). The verdict and any feedback flow
   back to X as the tool's return value.
6. Circuit breakers: `max_turns` (default 20) → `budget_limited`,
   `token_budget` (optional, excludes cached tokens) → `budget_limited`,
   `consecutive_rejections` ≥ 3 → auto-`paused` for user intervention.
7. Terminal lifecycle: `/goal clear` aborts and deletes S;
   `lifetime="temporary"` cleanup deletes S 48h after last invocation;
   all tasks done → Goal auto-transitions to `done`.

### Slash-command-activates-skill

User-facing skills and Skill Kits also use this pattern through fixed
`/skill <name>` and `/kit <name>` chat-stream commands. Module-level helpers on
`command_service.py`:

```python
activate_skill_kit(*, agent, thread_id, user_id, skill_name, reason, ttl_override=None)
    # → adds to ThreadConfig.enabled_skills
    # → bind_tools_for_thread(kit.required_tools, source="slash_command")
    # → graph rebuild queued via agent._pending_tool_reload

deactivate_skill_kit(*, agent, thread_id, user_id, skill_name)
    # → removes from enabled_skills, evicts bound tools from temporary_tools
```

`/skill` activates markdown-only skills and prepends the SKILL.md body to the
current model turn. `/kit` activates Skill Kits, optionally accepts a TTL
override, binds required tools, and prepends the kit body to the same turn. The
command registry exposes `/skills` for listing, showing, and deactivating
visible skills. The chat-stream intercept (`api/routers/chat.py`) still calls
the same helpers for `/goal` and `/orchestrate`, whose bundled kits opt out of
user-facing activation with `metadata.nymeria.internal: true`. See [Agent
Skills](./skills.md#slash-command-activation).

### Key files

| Concern | File |
|---|---|
| Goal data model + lifecycle + authority check | `nymeria/core/goal_manager.py` |
| Goal tools (`propose_task`, `request_review`, `mark_task_done`, `provide_review_feedback`) | `nymeria/tools/goal_tools.py` |
| `nym_todo` lock guard | `nymeria/tools/todo.py` (TodoItem.goal_id + done-transition check) |
| Worker kit | `nymeria/skills_bundled/goal-worker/SKILL.md` |
| Supervisor kit | `nymeria/skills_bundled/goal-supervisor/SKILL.md` |
| Orchestrator kit | `nymeria/skills_bundled/orchestrate/SKILL.md` |
| Slash-command registration | `nymeria/core/command_service.py::_register_defaults` |
| Slash-command activation helpers | `nymeria/core/command_service.py::activate_skill_kit` / `deactivate_skill_kit` |
| Chat-stream intercept + supervisor spawn helper | `nymeria/api/routers/chat.py` |
| `spawn_thread` modes (`branched`, `temporary`) | `nymeria/tools/spawn_thread.py` |
| Idle-sweep for temporary threads | `nymeria/core/ticker.py::_maybe_submit_spawn_sweep` |

---

## Data Flow

### Standard Conversation

1. User sends message via CLI or API
2. NymeriaAgent receives message
3. Time context injected (example): `[Time: Thursday, February 18, 2026 at 05:42 PM (America/New_York)]`
4. User memories loaded and formatted into system prompt
5. Message wrapped in `HumanMessage` and sent to graph
6. LLM decides: respond directly OR call tools
7. If tools needed: execute tools, truncate oversized tool results, then feed results back to LLM
8. HTTP/API primitive tool calls append redacted audit events when `AUDIT_LOG_ENABLED=true`
9. Loop until LLM generates final response, hits the turn tool-call budget, or repeats the same tool call/result 5 times in a row
10. State saved to SQLite for conversation continuity
11. Response returned to user

### Memory Injection

User memories are NOT recalled via tool calls. They're automatically included in the system prompt:

```
System Prompt = soul.md content

---
## User Memories

### Communication Preferences
- tone: casual and friendly
- verbosity: concise, bullet points

### Known Facts
- name: Alex
- occupation: Software engineer
- current_project: Building Nymeria
```

The LLM naturally uses these memories without explicit retrieval.

### RAG (Semantic Conversation Recall)

`MemoryIndex` (`core/memory_index.py`) provides per-user semantic search over indexed conversation turns, profile memories, and completed TODO outcomes. Storage is per-user SQLite at `data/users/{user_id}/memory.db` using sqlite-vec (1536-dim vectors via the OpenAI-compatible `EMBEDDING_MODEL`, default `text-embedding-3-small`) plus FTS5 for hybrid BM25 + vector retrieval.

Conversation indexing is **automatic** as of 2026-04 (`opt_in.rag_enabled` defaults to `True`; existing profiles are migrated once via the `opt_in.rag_migrated` watermark). Four hook points keep the index in sync with thread state:

| Hook | Where | Function |
|------|-------|----------|
| Per turn | After every chat turn | `Agent._index_conversation_turn` |
| Pre-compact | Before manual `/compact`, async + sync auto-compact | `Agent._pre_trim_memory_flush` |
| Pre-clear | Before `POST /threads/{id}/clear` deletes checkpoints | `Agent._pre_trim_memory_flush` |
| Delete cleanup | During the full `DELETE /threads/{id}` cascade | `MemoryIndex.delete_by_thread` plus thread-bound resource cleanup |

Agents query the index via the `rag_search` tool. When retrieved chunks are inserted into hidden prompt context, they are marked as untrusted reference data and rendered as JSONL records so stored conversation text is not interpreted as new instructions. Users can opt out at any time via the RAG settings API or frontend settings UI; the migration watermark prevents re-flipping.

---

## Persistence

Thread deletion is a hard cascade. `DELETE /threads/{id}` removes conversation
checkpoints, metadata, config, notepad content, RAG chunks, TODOs and schedule
rows, triggers and execution logs, chat-app bindings, bind codes, owner rows,
activity entries, notifications, and FCM thread filters. User/account-level
resources such as platform identities, registered Telegram bots, profile
memories, skills, and custom tools are preserved.

### Conversation Storage

Conversation state is stored per `thread_id` through LangGraph's checkpointer
system. The configured `DATABASE_BACKEND` selects the durable store:
- Local/default mode uses SQLite at `data/nymeria.db`.
- Docker mode sets `DATABASE_BACKEND=postgres` and stores checkpoints in the
  `nymeria-postgres` service via `POSTGRES_URI`.
- `memory` mode is available for non-persistent test or throwaway runs.

In all durable modes:
- Each thread is an isolated conversation
- **Survives application restarts** (true persistence, not in-memory)
- Auto-compact summarizes at the configured threshold, defaulting to 80% context (or legacy sliding window keeps last N cycles)

**Shared SQLite saver architecture:**
- `vendor/react_agent/graph.py` keeps one process-wide `SqliteSaver` and
  SQLite connection per database path.
- `AsyncCheckpointSaverWrapper` wraps that same sync saver and exposes both sync
  methods (`get_tuple`, `put`, `put_writes`) and async methods (`aget_tuple`,
  `aput`, `aput_writes`) by running sync operations in a thread executor.
- `create_checkpointer()` returns the wrapper for both `sqlite` and
  `sqlite_async`, so `chat()` and `astream()` use the same saver instance and
  serialization path. WAL mode still provides concurrent SQLite access.

Nymeria intentionally does not mix LangGraph's upstream `SqliteSaver` and
`AsyncSqliteSaver` for the same SQLite checkpoint database. Earlier mixed-saver
experiments could write checkpoints or pending writes that the other execution
path did not reliably rehydrate, which surfaced as missing history or stale
thread state when a conversation moved between sync chat, API streaming,
scheduled TODOs, triggers, callable threads, and spawned threads.

**PostgreSQL checkpointer architecture:**
- Docker compose provisions `postgres:15-alpine` as `nymeria-postgres`.
- All app services use `DATABASE_BACKEND=postgres` and a shared
  `POSTGRES_URI` pointing at that container.
- `vendor/react_agent/graph.py` creates a LangGraph `PostgresSaver`, runs
  `setup()`, and wraps it with `AsyncCheckpointSaverWrapper` so sync and async
  agent paths use the same checkpoint tables through the same adapter logic as
  SQLite.

See [LangGraph PERSISTENCE.md](../../LangGraph/docs/PERSISTENCE.md) for full technical details.

### Scheduled TODO Storage

SQLite stores scheduled TODOs for autonomous execution:
- Located at `data/todo_schedule.db` (managed by `TodoScheduleDB`)
- Scheduled TODOs survive application restarts
- Missed scheduled TODOs are recovered on startup
- `scheduled_todos` schema: todo_id, user_id, thread_id, scheduled_for, task_preview, created_at
- `active_todo_executions` tracks TODOs currently owned by a ticker worker so API edit/complete/delete requests can return `409 Conflict` while a run is in progress. Markers older than 24 hours are treated as stale crash leftovers and removed automatically.

Completed TODO retention is handled by the ticker against `data/todos/{user_id}.json`. Items whose status is `done` and whose `updated_at` is older than `TODO_AUTO_ARCHIVE_DAYS` (default 7) are removed from the active TODO list during hourly cleanup; there is no separate TODO archive table or file.

Legacy `data/tasks.db` files from older installs are ignored by the current runtime.

### Thread Metadata

Server-authoritative thread metadata replaces frontend-only localStorage titles:
- Location: `data/thread_metadata/{user_id}.json`
- Contains: per-thread title, title_source, pinned state, platform, timestamps
- Title sources: `"auto"` (generated from first message), `"user"` (manual rename), `"callable"` (synced from callable_name)
- Platform detection: stored in metadata (e.g., `"desktop"`, `"callable"`, `"discord"`, `"telegram"`) rather than inferred from thread ID prefixes
- Thread-safe with a per-user `KeyedRLockMap` lock
- All trigger sources (API, Discord, webhooks) create metadata entries on first message

### User Profiles

JSON files store user memories:
- Location: `data/users/{user_id}/profile.json`
- Thread-safe with atomic file operations guarded by a per-user `KeyedRLockMap` lock
- Contains: memories, personality_overrides, timestamps
- Prompt injection guard: profile memories and personality overrides are injected into prompts as explicitly untrusted JSONL records, not Markdown instructions.

### Audit Logs

Tool executions logged to JSONL:
- Location: `data/logs/audit_YYYYMMDD.jsonl`
- Contains: timestamp, tool name, arguments, result

### Backups

Self-modification backups:
- Location: `data/backups/`
- Max 10 backups per file
- Mirrors source directory structure

---

## Graph Caching

NymeriaAgent caches compiled graphs per user. The cache key includes:
- User ID
- Hash of user memories
- Hash of personality overrides
- Hash of thread configuration (custom instructions, enabled/disabled tools, LLM overrides)

When memories or thread config change, the graph is automatically rebuilt with the updated system prompt and tool set.

```python
# Internal cache structure
self._user_graphs: Dict[tuple, tuple] = {}
# (user_id, thread_id) -> (combined_hash, graph)
```

---

## Owned Runtime Fork

Nymeria's LangGraph ReAct runtime lives at `nymeria/vendor/react_agent/`. The
path is kept for import stability, but this package is an owned fork, not a
drop-in upstream mirror. It provides:
- Zero external source-path dependencies (no need to configure `LANGGRAPH_PATH`)
- Consistent behavior across all installations
- Easier deployment for beta testers
- Nymeria-specific provider, checkpoint, timeout, tool-reload, and reasoning
  streaming behavior

The package includes config, state management, tool registry, LLM providers,
nodes, and graph construction. See
`nymeria/vendor/react_agent/README.md` for the fork ownership and sync policy.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| **LangGraph** | ReAct agent framework |
| **LangChain** | Tool abstractions, message types |
| **FastAPI** | REST API server |
| **langgraph-checkpoint-sqlite** | Conversation persistence |
| **Pydantic** | Settings and validation |
| **httpx** | HTTP client for Perplexity API |

---

## Recent Refactoring & Migration Notes

### Code Cleanup (2026-02)

The codebase underwent significant modularization:

1. **Extracted Modules from `agent.py`:**
   - `prompts.py`: System prompt templates and time context
   - `time_utils.py`: Shared time parsing (from TODO tools and scheduler parsing)
   - `todo_constants.py`: TODO display constants
   - `agent_history.py`: Conversation-history projection, checkpoint timestamp recovery, and provider reasoning/tool-step rendering
   - `agent_streaming.py`: Graph stream event processing, stream chunk classification, and reasoning-delta deduplication helpers

2. **Removed Legacy Scheduler:**
   - `scheduler.py`, `rate_limiter.py`, `migration.py`, and `_deprecated/task_db.py` were removed after scheduled TODOs became the only runtime scheduling path.
   - The REST `GET /tasks` compatibility endpoint was removed; use `GET /todos` and filter scheduled TODOs instead.

3. **Frontend Cleanup:**
   - `tasks.svelte.ts`: Deleted (replaced by `todosStore.scheduledTodos`)
   - `ScheduledTaskItem.svelte`: Deleted (no longer used)
   - Legacy type aliases removed from `types/index.ts`
   - Unused experimental store utilities were later removed after never being adopted

### Potential Failure Points

These changes may cause issues in certain scenarios:

| Change | Potential Issue | Mitigation |
|--------|----------------|------------|
| Legacy scheduler removed | External code importing `DurableScheduler`, `TaskDatabase`, or calling `GET /tasks` will fail | Use TODO scheduling through `nym_todo` or REST `/todos` |
| `tasksStore` removed (frontend) | Any external frontend code using `tasksStore` will break | Use `todosStore.scheduledTodos` instead |
| `message.images` removed | Code accessing `message.images` property will fail | Use `message.attachments` instead |
| `ImageAttachment` type removed | TypeScript errors for code using this type | Use `Attachment` type instead |

### Migration Checklist

For users upgrading from older versions:

1. **Check import paths**: If you have custom code importing legacy scheduler classes from `nymeria.core`, remove those imports.
2. **Frontend stores**: Replace any `tasksStore` usage with `todosStore`.
3. **Verify data**: Check that scheduled tasks appear in the TODO list with `scheduled_for` times.

### Cleanup Timeline

- **Done**: All legacy scheduler modules removed; `_deprecated/` is empty.
