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
│  │              LangGraph ReAct Loop (max 70 iterations)         │  │
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
│  │      (Summarizes at 80% context, saves to memory)              │  │
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
┌──────────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│  SQLite          │ │ User Profile │ │  TODO-based  │ │    Ticker    │
│  Checkpointer    │ │   Manager    │ │  Scheduling  │ │  (Polling)   │
│  (nymeria.db)    │ │  (Memories)  │ │ (schedules)  │ │  (5s loop)   │
└──────────────────┘ └──────────────┘ └──────────────┘ └──────────────┘
```

---

## Core Components

### 1. NymeriaAgent (`nymeria/core/agent.py`)

Main orchestrator that:
- Loads configuration from environment variables
- Initializes the LangGraph execution graph
- Manages tool registration and hot-reloading
- Injects time context and user memories into each conversation
- Provides `chat()`, `stream()`, and `astream()` methods
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
| `thread_config.py` | Per-thread config (custom instructions, disabled/enabled tools, LLM overrides, callable thread settings) |
| `thread_metadata.py` | Server-authoritative thread metadata (titles, pins, platform). Replaces frontend-only localStorage titles. |
| `thread_agent_executor.py` | Delegates tasks to callable threads via `NymeriaAgent.stream()`. Publishes live SSE events. |
| `trigger_manager.py` | Event-driven trigger coordination, fires agent prompts or direct actions |
| `activity_log.py` | Per-thread activity feed with time-based retention |
| `prompts.py` | System prompt templates, mode-specific rules, time context generation |
| `audit.py` | AuditLogger for tool execution logging |
| `rate_limiter.py` | Sliding window rate limiting for autonomous operations |
| `time_utils.py` | Shared time parsing utilities (durations, schedules, deadlines) |
| `todo_constants.py` | Single source of truth for TODO display (status icons, priority markers) |
| `graph_cache.py` | Graph caching utilities (extracted, ready for integration) |
| `migration.py` | One-time migration from legacy scheduler to TODO system |
| `_deprecated/` | Legacy modules retained for migration (task_db.py, etc.) |

**Deprecated Modules (`_deprecated/`):**
- `task_db.py`: Old SQLite task database for `self_invoke` - replaced by TODO scheduling
- Will be removed after migration period is complete

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
2. LLM calls `memory_save("name", "Alex")`
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
thread_agent_executor delegates to callable thread's NymeriaAgent.stream()
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
- Rate limiting via `RateLimiter` class (default: 50/hour)
- Parallel execution via thread pool (`MAX_CONCURRENT_AUTONOMOUS`, default 5)

**Components:**
- `TodoManager`: Manages user TODO lists with atomic updates (JSON files in `data/todos/`)
- `TodoScheduleDB`: SQLite index for efficient polling (not source of truth - mirrors JSON)
- `RateLimiter`: Sliding window rate limiting (extracted to `rate_limiter.py`)
- `Ticker`: Global daemon thread that executes due scheduled TODOs
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
    1. Marks TODO as IN_PROGRESS (prevents duplicates)
    2. Publishes "task_started" event to EventBus
    3. Calls agent.stream(todo.task, _is_self_invoke=True)
    4. Streams tool_call, tool_result, thinking, response events to frontend
    5. Publishes "task_completed" event
    7. If recurring: calculates next execution, reschedules
    8. If not recurring: clears schedule, marks done
    9. Auto-compacts context if needed (or trims if using legacy sliding window)
```

**Recovery on Restart:**
When Nymeria starts, the Ticker:
1. Rebuilds schedule index from TODO JSON files (`rebuild_from_todos()`)
2. Recovers any missed scheduled TODOs that were due during downtime

**Legacy Support (Deprecated):**
The old `self_invoke` and `DurableScheduler` system is deprecated but retained in `_deprecated/` for migration. The `scheduler.py` file contains a deprecation warning. Migration from old scheduled tasks to TODOs is handled by `migration.py`.

---

### 4.1 Event Bus & Autonomous Streaming (`nymeria/core/event_bus.py`)

The **EventBus** enables real-time streaming of autonomous task execution to connected frontend clients.

**Purpose:**
- Decouples the Ticker from frontend SSE connections
- Enables multiple simultaneous frontend clients
- Provides real-time insight into autonomous operations

**Architecture:**
```
Ticker executes TODO
    ↓
publish_autonomous_event(type, thread_id, task_id, data)
    ↓
EventBus.publish() → distributes to all subscriber queues
    ↓
/autonomous/stream SSE endpoint reads from queue → sends to frontend
```

**Event Types:**
| Event | When Published | Data |
|-------|---------------|------|
| `task_started` | TODO execution begins | `prompt`, `todo_id` |
| `thinking` | LLM reasoning | `content` |
| `tool_call` | Tool invocation | `id`, `name`, `args` |
| `tool_result` | Tool returns | `id`, `name`, `result` |
| `response` | Final response chunks | `content` |
| `task_completed` | Execution finishes | `notify`, `content`, `summary` |

**Subscriber Management:**
- Per-subscriber `Queue` (maxsize=100)
- Non-blocking publish (drops if queue full)
- Subscribers identified by UUID
- Events logged when no subscribers connected (helps debug frontend issues)

**SSE Endpoint (`/autonomous/stream`):**
```python
GET /autonomous/stream?user_id=default&api_key={key}
```
- API key passed as query param (EventSource doesn't support headers)
- Sends heartbeat every 1 second when idle
- Filters events by user_id if specified
- Unsubscribes on client disconnect

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
2. Agent calls memory_save for important persistent facts
3. All messages cleared from thread
4. Summary injected as context for continuation
5. Agent continues where it left off (auto-compact) OR
   Summary attached to user's next message (manual /compact)
```

**Key Components:**
- `TokenTracker` (`token_tracker.py`): Tracks cumulative tokens per thread
- `ConversationCompactor` (`compactor.py`): Generates summary prompts, formats resume context
- Model limits fetched from OpenRouter API with static fallbacks

**Configuration:**
```bash
CONTEXT_MANAGEMENT=auto_compact  # auto_compact, sliding_window, or none
COMPACT_THRESHOLD=0.8            # Trigger at 80% of context limit
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
| Core System | bash_execute, file_read, file_write, web_search, consult, claude_code |
| Profile & RAG | memory_save, memory_forget, personality_set, rag_search |
| TODO | todo, todo_delete, todo_list |
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

The agent provides two streaming methods with different use cases:

**`stream()` (Synchronous)**
- Uses `graph.stream()` with `stream_mode="updates"`
- Returns complete node outputs after each node execution
- Provides full tool call arguments (unlike `stream_mode="messages"` which has empty args)
- Used by CLI interface and Ticker (autonomous mode)

**`astream()` (Asynchronous)**
- Uses `graph.astream_events()` with `version="v2"`
- Captures complete tool call information via `on_tool_start` events
- Returns tool calls with full arguments
- Used by FastAPI for SSE responses to desktop UI

**Why these stream modes?**

LangGraph's `stream_mode="messages"` doesn't include tool arguments in streaming chunks - only the tool name and ID are available until after execution. Both methods now provide complete tool arguments:

- `stream_mode="updates"`: Complete AIMessage with tool_calls after agent node
- `astream_events`: `on_tool_start` event with complete input arguments

This enables the desktop UI to display tool call arguments in real-time for both interactive and autonomous modes.

**Content Block Classification**

Anthropic models produce typed content blocks: `thinking` (internal reasoning), `text` (preamble/response), `tool_use`, `redacted_thinking`, and `signature`. Both `stream()` and `astream()` iterate content blocks in order and emit correctly typed SSE events:

- `thinking` blocks → `type: "thinking"` events (rendered as collapsible ThinkingBlock)
- `text` blocks → `type: "response"` events (rendered as inline markdown)
- `tool_use` blocks → `type: "tool_call"` events (rendered as ToolCallCard)

For OpenRouter/OpenAI models (string content, no typed blocks), all text is emitted as `type: "response"` events.

The `get_conversation_history()` method (used for page refresh/checkpoint rebuild) applies the same classification: it iterates through stored content blocks in order, preserving interleaved thinking between tool calls.

**Callable Thread Streaming**

Callable thread invocations stream SSE events (thinking, tool_call, tool_result, response) to the event bus in real-time via `thread_agent_executor.py`, so the frontend can display callable thread activity as it happens. Parent→child invocations are tracked for cascading abort support.

---

### 9. Triggers (`nymeria/triggers/`)

Input interfaces and event-driven adapters that route messages to the agent:

**CLI** (`cli.py`):
- Interactive terminal interface
- Rich formatting for responses
- Special commands: `/history`, `/clear`, `/tools`, `/quit`

**API** (`api.py`):
- FastAPI server with SSE streaming
- Bearer token authentication
- Key endpoints: `/chat` (SSE), `/autonomous/stream`, `/threads`, `/todos`, `/tools`, `/agents/threads`, `/triggers`
- Thread metadata management: `PATCH /threads/{id}/metadata` syncs titles, pins, and platform across surfaces. Renaming a callable thread also updates its `callable_name` and rebuilds the tool registry.
- CRUD for threads, TODOs, custom tools, callable threads, and triggers
- Hot-reload settings via `PATCH /settings` (clears `@lru_cache`, rebuilds agent graphs)

**Webhook** (`webhook.py`):
- Incoming webhook endpoints for Telegram, Discord, and Slack
- Message routing to agent with platform-specific formatting

**Discord Bot** (`discord_bot.py`):
- Gateway (WebSocket) or webhook mode
- Configurable respond mode: `mention` (only @Nymeria) or `all`

**Event-Driven Trigger Sources** (`triggers/sources/`):
- `base.py` — Abstract `TriggerSource` base class
- `webhook_source.py` — Generic incoming webhook trigger
- `outlook_email_source.py` — Polls Outlook for new emails, fires agent prompts
- Managed by `core/trigger_manager.py` which coordinates source lifecycle

---

### 10. Configuration (`nymeria/config/`)

- `settings.py`: Pydantic settings from environment variables
- `soul.md`: System prompt defining Nymeria's personality and capabilities
- `self_agent_prompt.md`: Specialized prompt for self-modification flows

---

## Data Flow

### Standard Conversation

1. User sends message via CLI or API
2. NymeriaAgent receives message
3. Time context injected (example): `[Time: Thursday, February 18, 2026 at 05:42 PM (America/New_York)]`
4. User memories loaded and formatted into system prompt
5. Message wrapped in `HumanMessage` and sent to graph
6. LLM decides: respond directly OR call tools
7. If tools needed: execute tools, feed results back to LLM
8. All tool calls logged to audit log
9. Loop until LLM generates final response (max 70 iterations)
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

---

## Persistence

### Conversation Storage

SQLite stores conversation state per `thread_id` using LangGraph's checkpointer system:
- Each thread is an isolated conversation
- **Survives application restarts** (true persistence, not in-memory)
- Located at `data/nymeria.db`
- Auto-compact summarizes at 80% context (or legacy sliding window keeps last N cycles)

**Dual-Saver Architecture:**
- **SqliteSaver**: Handles sync operations (ticker, `stream()`, `chat()`)
- **LazyAsyncSqliteSaver**: Handles async operations (`astream()` for API)
- Both share the same database file with WAL mode for concurrent access

The async saver uses lazy initialization to avoid event loop issues in Windows services. Connection is established on first async call, not at startup.

See [LangGraph PERSISTENCE.md](../../LangGraph/docs/PERSISTENCE.md) for full technical details.

### Scheduled TODO Storage

SQLite stores scheduled TODOs for autonomous execution:
- Located at `data/todo_schedule.db` (managed by `TodoScheduleDB`)
- Scheduled TODOs survive application restarts
- Missed scheduled TODOs are recovered on startup
- Schema: todo_id, user_id, thread_id, scheduled_for, task_preview, created_at

**Legacy Task Storage (Deprecated):**
- Located at `data/tasks.db` (via `_deprecated/task_db.py`)
- Old `self_invoke` tasks are migrated to TODO system on startup
- Will be removed after migration period

### Thread Metadata

Server-authoritative thread metadata replaces frontend-only localStorage titles:
- Location: `data/thread_metadata/{user_id}.json`
- Contains: per-thread title, title_source, pinned state, platform, timestamps
- Title sources: `"auto"` (generated from first message), `"user"` (manual rename), `"callable"` (synced from callable_name)
- Platform detection: stored in metadata (e.g., `"desktop"`, `"callable"`, `"discord"`, `"telegram"`) rather than inferred from thread ID prefixes
- Thread-safe with per-user RLock (same pattern as `todo_manager.py`)
- All trigger sources (API, Discord, webhooks) create metadata entries on first message

### User Profiles

JSON files store user memories:
- Location: `data/users/{user_id}/profile.json`
- Thread-safe with atomic file operations
- Contains: memories, personality_overrides, timestamps

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

## Vendored Dependencies

The `react_agent` module from LangGraph is bundled at `nymeria/vendor/react_agent/`. This provides:
- Zero external path dependencies (no need to configure `LANGGRAPH_PATH`)
- Consistent behavior across all installations
- Easier deployment for beta testers

The vendored package includes: config, state management, tool registry, LLM providers, nodes, and graph construction.

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
   - `audit.py`: AuditLogger class
   - `rate_limiter.py`: RateLimiter class (from scheduler.py)
   - `time_utils.py`: Shared time parsing (from todo.py, scheduler.py)
   - `todo_constants.py`: TODO display constants
   - `graph_cache.py`: Graph caching utilities
   - `migration.py`: One-time migration function

2. **Deprecated Modules:**
   - `scheduler_deprecated.py`: Deleted (was already deprecated)
   - `task_db.py`: Moved to `_deprecated/task_db.py`
   - `DurableScheduler` in `scheduler.py`: Deprecated warning added

3. **Frontend Cleanup:**
   - `tasks.svelte.ts`: Deleted (replaced by `todosStore.scheduledTodos`)
   - `ScheduledTaskItem.svelte`: Deleted (no longer used)
   - Legacy type aliases removed from `types/index.ts`
   - Store utilities added: `polling.ts`, `crud-store.ts`

### Potential Failure Points

These changes may cause issues in certain scenarios:

| Change | Potential Issue | Mitigation |
|--------|----------------|------------|
| `task_db.py` moved to `_deprecated/` | External code importing `from nymeria.core.task_db` will fail | Update import to `from nymeria.core._deprecated.task_db` |
| `DurableScheduler` deprecated | Deprecation warnings in logs for code using scheduler directly | Migrate to TODO-based scheduling |
| `tasksStore` removed (frontend) | Any external frontend code using `tasksStore` will break | Use `todosStore.scheduledTodos` instead |
| `message.images` removed | Code accessing `message.images` property will fail | Use `message.attachments` instead |
| `ImageAttachment` type removed | TypeScript errors for code using this type | Use `Attachment` type instead |
| `graph_cache.py` not yet integrated | Graph caching still inline in agent.py | GraphCache class available but optional |

### Migration Checklist

For users upgrading from older versions:

1. **Check import paths**: If you have custom code importing from `nymeria.core`, verify paths are correct
2. **Review logs**: Look for deprecation warnings about `DurableScheduler` or `self_invoke`
3. **Frontend stores**: Replace any `tasksStore` usage with `todosStore`
4. **Run migration**: The `_migrate_old_scheduled_tasks()` function runs automatically on startup to convert old scheduled tasks to TODOs
5. **Verify data**: Check that scheduled tasks appear in the TODO list with `scheduled_for` times

### Cleanup Timeline

- **Now**: Deprecated modules in `_deprecated/` with warnings
- **Future**: Complete removal of `_deprecated/` folder after migration period
- **Future**: Integration of `GraphCache` class into agent.py (optional optimization)
