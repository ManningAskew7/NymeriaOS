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
│  │   • Time context (Sydney timezone, quiet hours)               │  │
│  │   • User memories from profile                                │  │
│  │   • Personality preferences                                    │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                           │                                          │
│                           ▼                                          │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │              LangGraph ReAct Loop (max 25 iterations)         │  │
│  │                                                                │  │
│  │   ┌─────────┐    ┌─────────┐    ┌─────────────────┐          │  │
│  │   │   LLM   │───▶│ Router  │───▶│     Tools       │          │  │
│  │   │ (Think) │    │         │    │  (15 available) │          │  │
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
agent = NymeriaAgent(tools=ALL_TOOLS)
response = agent.chat("Hello", thread_id="user123", user_id="default")
```

**Key Features:**
- Memory hash caching: Graphs are rebuilt only when user memories change
- Time context injection: Every message includes current time (Sydney timezone)
- Quiet hours awareness: Tracks if 10 PM - 7 AM for autonomous behavior

---

### 1.1 Core Module Structure

The `nymeria/core/` directory contains modular components extracted for maintainability:

| Module | Purpose |
|--------|---------|
| `agent.py` | Main NymeriaAgent class (orchestrator) |
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

### 3. SelfModifyAgent (`nymeria/core/self_agent.py`)

Sub-agent that safely modifies Nymeria's own codebase.

**Capabilities:**
- `self_file_read()`: Read files in `nymeria/tools/`
- `self_file_write()`: Write files (with Python syntax validation)
- `self_file_delete()`: Delete files
- `self_file_list()`: List directory contents
- `self_test_import()`: Test that tools can be imported

**Safety Features:**
- Restricted to `nymeria/tools/` directory only
- Cannot modify core agent, settings, or configuration
- Creates timestamped backups before every modification
- Validates Python syntax before saving
- Max 15 iterations to prevent infinite loops

**Workflow:**
```
User: "Add a calculator tool"
    ↓
LLM calls: self_modify("Create a calculator tool", "add_tool")
    ↓
SelfModifyAgent:
    1. Creates backup of files to be modified
    2. Reads existing tools to understand patterns
    3. Creates new tool file
    4. Updates __init__.py exports
    5. Runs self_test_import() to validate
    6. Reports what was done
    ↓
LLM calls: tools_reload()
    ↓
New tool immediately available
```

---

### 4. TODO-based Scheduling (`nymeria/core/todo_*.py`, `ticker.py`)

Manages autonomous operation for 24/7 functionality through the **TODO system** with scheduled items.

**Key Concepts:**
- TODOs can have a `scheduled_for` datetime for future execution
- **Durable**: Scheduled TODOs persist across restarts (SQLite via `todo_schedule_db.py`)
- Global polling ticker instead of threading.Timer
- Rate limiting via `RateLimiter` class (default: 50/hour)
- Quiet hours support (10 PM - 7 AM Sydney time)

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
    5. Checks mute_response flag for visibility control
    6. Publishes "task_completed" event with visibility info
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
- Provides real-time visibility into autonomous operations

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
| `task_completed` | Execution finishes | `visibility`, `notify`, `content`, `summary` |

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

### 4.2 Response Visibility Control (`nymeria/tools/visibility.py`)

The **mute_response** tool allows the LLM to control how autonomous responses are displayed.

**Tool Definition:**
```python
@tool
def mute_response(reason: str = "") -> str:
    """Move current response to activity log instead of showing in chat."""
```

**Visibility Modes:**
| Mode | Chat Display | Activity Log | Use Case |
|------|-------------|--------------|----------|
| `full` | ✅ Shown | ✅ Logged | Important results, user questions |
| `activity` | ❌ Hidden | ✅ Logged | Routine checks, nothing to report |

**How It Works:**
1. LLM calls `mute_response("routine check")` during execution
2. Tool sets thread-local flag in `_mute_flags[thread_id]`
3. After streaming completes, Ticker calls `get_and_clear_mute_flag(thread_id)`
4. Response object created with `visibility="activity"` or `"full"`
5. `task_completed` event includes visibility for frontend to handle
6. Frontend removes message from chat if `visibility="activity"`

**Response Object (`nymeria/core/response_handler.py`):**
```python
class NymeriaResponse:
    visibility: Literal["activity", "full"]
    notify: bool          # Create push notification
    summary: str | None   # Notification text (max 100 chars)
    content: str          # Full response content
```

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

Tools are LangChain `@tool` decorated functions organized by category:

| Category | Count | Examples |
|----------|-------|----------|
| Core | 5 | bash_execute, file_read, file_write, file_list, web_search |
| Memory | 5 | memory_save, memory_forget, memory_list, memory_clear_all, personality_set |
| Self-Modification | 3 | self_modify, self_modify_rollback, tools_reload |
| TODO | 5 | todo_add, todo_update, todo_complete, todo_delete, todo_list |
| Sub-Agents | 4 | sub_agent, list_agents, clear_agent_context, reload_agents |
| Visibility | 1 | mute_response |

Tools are registered dynamically and can be hot-reloaded via `tools_reload()`.

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

**Sub-Agent Isolation**

The `SelfModifyAgent` uses context variable isolation to prevent its internal tool calls from leaking into the parent's SSE stream:

```python
# Reset LangGraph streaming context before sub-agent invocation
from langchain_core.runnables.config import var_child_runnable_config
config_token = var_child_runnable_config.set(None)
try:
    result = graph.invoke(...)
finally:
    var_child_runnable_config.reset(config_token)
```

---

### 9. Triggers (`nymeria/triggers/`)

Input interfaces that route messages to the agent:

**CLI** (`cli.py`):
- Interactive terminal interface
- Rich formatting for responses
- Special commands: `/history`, `/clear`, `/tools`, `/quit`

**API** (`api.py`):
- FastAPI server with SSE streaming
- Bearer token authentication
- Endpoints: `/chat`, `/chat/sync`, `/threads/{id}/history`, `/tools`

---

### 10. Configuration (`nymeria/config/`)

- `settings.py`: Pydantic settings from environment variables
- `soul.md`: System prompt defining Nymeria's personality and capabilities
- `self_agent_prompt.md`: Specialized prompt for the self-modification sub-agent

---

## Data Flow

### Standard Conversation

1. User sends message via CLI or API
2. NymeriaAgent receives message
3. Time context injected: `[Current Time: 2025-01-15 14:30:00 AEDT]`
4. User memories loaded and formatted into system prompt
5. Message wrapped in `HumanMessage` and sent to graph
6. LLM decides: respond directly OR call tools
7. If tools needed: execute tools, feed results back to LLM
8. All tool calls logged to audit log
9. Loop until LLM generates final response (max 25 iterations)
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
- Located at `data/schedules.db` (managed by `TodoScheduleDB`)
- Scheduled TODOs survive application restarts
- Missed scheduled TODOs are recovered on startup
- Schema: todo_id, user_id, scheduled_for, task_preview, thread_id, status

**Legacy Task Storage (Deprecated):**
- Located at `data/tasks.db` (via `_deprecated/task_db.py`)
- Old `self_invoke` tasks are migrated to TODO system on startup
- Will be removed after migration period

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

When memories change, the graph is automatically rebuilt with updated system prompt.

```python
# Internal cache structure
self._user_graphs: Dict[str, Tuple[str, CompiledGraph]] = {}
# user_id -> (memory_hash, graph)
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
