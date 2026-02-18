# Nymeria REST API

Base URL: `http://localhost:8000`

## Authentication

Most endpoints require Bearer token authentication:

```
Authorization: Bearer <NYMERIA_API_KEY>
```

The API key is configured via the `NYMERIA_API_KEY` environment variable.

Exceptions without Bearer auth:
- `GET /health`
- `POST /triggers/fire/{trigger_id}` (uses optional per-trigger secret instead)

---

## Endpoints

### Health Check

```http
GET /health
```

No authentication required.

**Response:**
```json
{
  "status": "ok",
  "version": "1.0.0"
}
```

---

### Chat (Streaming)

```http
POST /chat
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:**
```json
{
  "message": "Hello",
  "thread_id": "optional-thread-id",
  "user_id": "default",
  "attachments": [],
  "force_unsupported_attachments": false
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `message` | string | Yes | - | User message |
| `thread_id` | string | No | auto-generated | Conversation thread ID |
| `user_id` | string | No | `"default"` | User ID for profile/memory isolation |
| `attachments` | array | No | - | Optional multimodal attachments (images/documents) |
| `force_unsupported_attachments` | bool | No | `false` | Send request even if model modality checks fail |

**Response:** Server-Sent Events (SSE)

```
data: {"type": "thinking", "content": "...", "thread_id": "abc123"}
data: {"type": "tool_call", "name": "web_search", "args": {...}, "thread_id": "abc123"}
data: {"type": "tool_result", "name": "web_search", "result": "...", "thread_id": "abc123"}
data: {"type": "response", "content": "...", "thread_id": "abc123"}
data: {"type": "done", "thread_id": "abc123"}
```

---

### Chat (Synchronous)

```http
POST /chat/sync
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:** Same as streaming endpoint

**Response:**
```json
{
  "response": "Hello! How can I help?",
  "thread_id": "abc123"
}
```

---

### Get Thread History

```http
GET /threads/{thread_id}/history
Authorization: Bearer <token>
```

**Response:**
```json
{
  "thread_id": "abc123",
  "messages": [
    {"type": "HumanMessage", "content": "Hello"},
    {"type": "AIMessage", "content": "Hi there!"},
    {"type": "ToolMessage", "name": "web_search", "content": "..."}
  ]
}
```

---

### Get Thread Context Stats

```http
GET /threads/{thread_id}/context
Authorization: Bearer <token>
```

Get context window usage statistics for a thread.

**Response:**
```json
{
  "thread_id": "abc123",
  "total_tokens": 45000,
  "input_tokens": 30000,
  "output_tokens": 15000,
  "context_limit": 200000,
  "usage_percentage": 22.5,
  "compaction_count": 1,
  "last_compaction": "2025-01-15T10:30:00Z",
  "context_management": "auto_compact"
}
```

---

### Compact Thread

```http
POST /threads/{thread_id}/compact?user_id=default
Authorization: Bearer <token>
```

Manually trigger context compaction for a thread.

**Response:**
```json
{
  "success": true,
  "messages_removed": 42,
  "summary_pending": true
}
```

When `summary_pending` is `true`, the summary will be attached to the user's next message in that thread.

---

### List Tools

```http
GET /tools
Authorization: Bearer <token>
```

**Response:**
```json
{
  "tools": [
    {"name": "bash_execute", "description": "Execute shell commands...", "enabled": true},
    {"name": "file_read", "description": "Read contents of a file...", "enabled": true},
    {"name": "memory_save", "description": "Save a memory about the user...", "enabled": true}
  ]
}
```

---

## SSE Event Types

| Type | Description | Fields |
|------|-------------|--------|
| `thinking` | Agent's reasoning before tool calls | `content` |
| `tool_call` | Tool being invoked | `name`, `args` |
| `tool_result` | Tool execution result | `name`, `result` |
| `response` | Final response text (may be chunked) | `content` |
| `context_attached` | Previous context summary attached to this message | `message` |
| `compacted` | Auto-compact triggered, agent resuming | `messages_removed`, `auto_resumed` |
| `command_result` | Slash command executed | `command`, `result` |
| `error` | Error message | `content` |
| `done` | Stream complete | - |

All events include `thread_id` for correlation.

### Slash Commands

The `/chat` endpoint supports slash commands. Send the command as the message:

| Command | Description |
|---------|-------------|
| `/compact` | Manually trigger context compaction. Returns `command_result` with summary status. |

Example:
```json
{"message": "/compact", "thread_id": "abc123"}
```

Response:
```
data: {"type": "command_result", "command": "compact", "result": {"success": true, "messages_removed": 42, "summary_pending": true}}
data: {"type": "done", "thread_id": "abc123"}
```

After `/compact`, the summary is attached to the user's **next** message. The UI should show an indicator like "(context summary attached)".

---

---

### Autonomous Task Stream (SSE)

```http
GET /autonomous/stream?user_id=default&api_key=<token>
```

**Note:** API key passed as query parameter because `EventSource` doesn't support custom headers.

Connects to a Server-Sent Events stream for receiving real-time updates during autonomous task execution (scheduled TODOs).

**Query Parameters:**
| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `user_id` | No | `"default"` | Filter events by user ID |
| `api_key` | Yes | - | API authentication key |

**Event Types:**

| Event | Description | Fields |
|-------|-------------|--------|
| `task_started` | Scheduled TODO execution begins | `thread_id`, `task_id`, `prompt`, `todo_id` |
| `thinking` | Agent reasoning | `content` |
| `tool_call` | Tool invocation | `id`, `name`, `args` |
| `tool_result` | Tool execution result | `id`, `name`, `result` |
| `response` | Response text chunks | `content` |
| `task_completed` | Execution finished | `visibility`, `notify`, `content`, `summary`, `todo_id` |

**Visibility Values:**
- `"full"` - Response should be shown in chat thread
- `"activity"` - Response should only appear in activity log (muted via `mute_response` tool)

**Example Stream:**
```
: heartbeat
data: {"type":"task_started","thread_id":"abc123","task_id":"todo-xyz","prompt":"Check inbox","todo_id":"xyz"}
data: {"type":"thinking","content":"I'll check the inbox now..."}
data: {"type":"tool_call","id":"tool1","name":"bash_execute","args":{"command":"ls ~/inbox"}}
data: {"type":"tool_result","id":"tool1","name":"bash_execute","result":"email1.txt\nemail2.txt"}
data: {"type":"response","content":"Found 2 new emails in inbox."}
data: {"type":"task_completed","visibility":"full","notify":false,"content":"Found 2 new emails.","todo_id":"xyz"}
: heartbeat
```

**Heartbeat:** Sent every ~1 second when no events to keep connection alive.

---

## Multi-User Support

The `user_id` field enables per-user memory isolation:

- Each `user_id` has its own profile and memories
- Memories saved by user A are not visible to user B
- Default is `"default"` for single-user deployments
- Useful for multi-tenant applications sharing one API

**Example:**
```json
{
  "message": "My name is Alex",
  "user_id": "user_123"
}
```

This will save the name to user_123's profile, isolated from other users.

---

## Example: Python Client

### Synchronous Request

```python
import httpx

response = httpx.post(
    "http://localhost:8000/chat/sync",
    json={
        "message": "Hello",
        "user_id": "my_user"
    },
    headers={"Authorization": "Bearer dev-key"}
)
print(response.json()["response"])
```

### Streaming Request

```python
import httpx

with httpx.stream(
    "POST",
    "http://localhost:8000/chat",
    json={"message": "Search for Python tutorials"},
    headers={"Authorization": "Bearer dev-key"}
) as r:
    for line in r.iter_lines():
        if line.startswith("data: "):
            import json
            event = json.loads(line[6:])

            if event["type"] == "response":
                print(event["content"], end="", flush=True)
            elif event["type"] == "tool_call":
                print(f"\n[Calling {event['name']}...]")
            elif event["type"] == "done":
                print("\n[Done]")
```

### Async Client

```python
import httpx
import asyncio

async def chat():
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:8000/chat/sync",
            json={"message": "Hello"},
            headers={"Authorization": "Bearer dev-key"}
        )
        return response.json()

result = asyncio.run(chat())
print(result["response"])
```

---

### Get Settings

```http
GET /settings
Authorization: Bearer <token>
```

**Response:**
```json
{
  "llm_provider": "anthropic",
  "llm_model": "claude-sonnet-4-20250514",
  "llm_temperature": 1.0,
  "llm_max_tokens": null,
  "llm_top_p": null,
  "llm_top_k": null,
  "llm_frequency_penalty": null,
  "llm_presence_penalty": null,
  "llm_reasoning_effort": null,
  "llm_extended_thinking": false,
  "context_management": "auto_compact",
  "compact_threshold": 0.8,
  "compact_keep_messages": 4,
  "compact_model": null,
  "sliding_window_cycles": 5,
  "max_self_invokes_per_hour": 50,
  "log_level": "INFO",
  "watchdog_enabled": true,
  "watchdog_interval_minutes": 30,
  "todo_staleness_hours": 4,
  "activity_retention_hours": 12
}
```

---

### Update Settings

```http
PATCH /settings
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:** (all fields optional)
```json
{
  "llm_provider": "openrouter",
  "llm_model": "anthropic/claude-sonnet-4",
  "llm_temperature": 0.7,
  "llm_max_tokens": 4096,
  "llm_top_p": 0.95,
  "llm_top_k": 40,
  "llm_frequency_penalty": 0.5,
  "llm_presence_penalty": 0.3,
  "llm_reasoning_effort": "medium"
}
```

| Field | Type | Range | Description |
|-------|------|-------|-------------|
| `llm_provider` | string | - | Provider: `anthropic`, `openai`, `openrouter` |
| `llm_model` | string | - | Model identifier |
| `llm_temperature` | float | 0.0-2.0 | Sampling temperature |
| `llm_max_tokens` | int | 1-32000 | Max output tokens |
| `llm_top_p` | float | 0.0-1.0 | Nucleus sampling threshold |
| `llm_top_k` | int | 1-100 | Top-k sampling |
| `llm_frequency_penalty` | float | -2.0-2.0 | Reduce repetition |
| `llm_presence_penalty` | float | -2.0-2.0 | Encourage new topics |
| `llm_reasoning_effort` | string | low/medium/high | For reasoning models |

**Response:**
```json
{
  "message": "Settings updated and applied",
  "updated": ["llm_model", "llm_temperature"],
  "restart_required": false
}
```

**Note:** Changes are written to `.env`/`.env.docker` and hot-reloaded immediately.

---

## TODO Management API

Manage TODO items with optional scheduling for autonomous execution.

### List TODOs

```http
GET /todos?user_id=default&filter_status=all
Authorization: Bearer <token>
```

**Query Parameters:**
| Parameter | Default | Description |
|-----------|---------|-------------|
| `user_id` | `"default"` | User ID |
| `filter_status` | active only | Filter: `all`, `pending`, `in_progress`, `blocked`, `done` |

**Response:**
```json
{
  "user_id": "default",
  "items": [
    {
      "id": "abc123",
      "task": "Check inbox",
      "status": "pending",
      "priority": "medium",
      "created_at": "2026-02-02T10:00:00Z",
      "updated_at": "2026-02-02T10:00:00Z",
      "scheduled_for": "2026-02-02T14:00:00Z",
      "thread_id": "thread-xyz",
      "recurrence": "daily",
      "created_by": "user"
    }
  ],
  "total": 1
}
```

---

### Create TODO

```http
POST /todos?user_id=default
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:**
```json
{
  "task": "Check inbox",
  "priority": "medium",
  "scheduled_for": "30m",
  "recurrence": "daily",
  "thread_id": "optional-thread-id",
  "notes": "Optional notes"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task` | string | Yes | Task description |
| `priority` | string | No | `low`, `medium`, `high` |
| `deadline` | string | No | Optional due date/time (ISO datetime) |
| `scheduled_for` | string | No | Relative (`30s`, `5m`, `2h`, `1d`) or absolute (`YYYY-MM-DD HH:MM[:SS]` / `YYYY-MM-DDTHH:MM[:SS]`, interpreted in `USER_TIMEZONE`) |
| `recurrence` | string | No | `5min`, `10min`, `15min`, `30min`, `hourly`, `daily`, `weekly`, `monthly` |
| `thread_id` | string | No | Thread for autonomous output |
| `notes` | string | No | Additional context |

**Response:** Created TODO object

---

### Update TODO

```http
PATCH /todos/{todo_id}?user_id=default
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:** (all fields optional)
```json
{
  "task": "Updated task",
  "status": "in_progress",
  "priority": "high",
  "scheduled_for": "2h",
  "thread_id": "thread-xyz",
  "clear_schedule": false,
  "recurrence": "weekly",
  "clear_recurrence": false,
  "clear_deadline": false
}
```

| Field | Type | Description |
|-------|------|-------------|
| `task` | string | Updated task text |
| `status` | string | `pending`, `in_progress`, `blocked`, `done` |
| `priority` | string | `low`, `medium`, `high` |
| `deadline` | string | Set/update deadline (ISO datetime) |
| `notes` | string | Set/update notes |
| `blocked_reason` | string | Reason for blocked status |
| `scheduled_for` | string | Set/update next scheduled execution |
| `recurrence` | string | Set/update recurrence pattern |
| `thread_id` | string | Thread for autonomous output |
| `clear_schedule` | bool | Remove schedule if `true` |
| `clear_recurrence` | bool | Remove recurrence if `true` |
| `clear_deadline` | bool | Remove deadline if `true` |

**Response:** Updated TODO object

---

### Complete TODO

```http
POST /todos/{todo_id}/complete?user_id=default
Authorization: Bearer <token>
```

Marks the TODO as done and removes any schedule.

**Response:** Updated TODO object

---

### Delete TODO

```http
DELETE /todos/{todo_id}?user_id=default
Authorization: Bearer <token>
```

**Response:**
```json
{
  "status": "ok",
  "deleted_id": "abc123"
}
```

---

## Activity Log API

### Get Activity

```http
GET /activity?user_id=default&limit=50
Authorization: Bearer <token>
```

Returns recent activity entries (autonomous tasks, tool executions, etc.).

**Response:**
```json
{
  "entries": [
    {
      "id": "entry123",
      "timestamp": "2026-02-02T10:00:00Z",
      "type": "task_completed",
      "message": "Checked inbox, found 2 emails",
      "thread_id": "thread-xyz",
      "metadata": {"todo_id": "abc123", "visibility": "full"}
    }
  ],
  "total": 1
}
```

---

## Notifications API

### Get Notifications

```http
GET /notifications?user_id=default
Authorization: Bearer <token>
```

**Response:**
```json
{
  "notifications": [
    {
      "id": "notif123",
      "summary": "Found important email from boss",
      "thread_id": "thread-xyz",
      "task_id": "todo-abc",
      "created_at": "2026-02-02T10:00:00Z",
      "read": false
    }
  ],
  "unread_count": 1
}
```

### Mark Notification Read

```http
POST /notifications/{notification_id}/read?user_id=default
Authorization: Bearer <token>
```

### Mark All Read

```http
POST /notifications/read-all?user_id=default
Authorization: Bearer <token>
```

---

## Custom Tools API

Manage custom HTTP and MCP tools programmatically.

### List Custom Tools

```http
GET /tools/custom
Authorization: Bearer <token>
```

**Response:**
```json
{
  "tools": [
    {
      "id": "get_weather",
      "name": "Get Weather",
      "description": "Get current weather for a city",
      "implementation_type": "http",
      "parameters": {
        "city": {
          "type": "string",
          "description": "City name",
          "required": true
        }
      },
      "http_config": {
        "method": "GET",
        "url": "https://api.weather.com/v1/current?city=${city}",
        "headers": {"Authorization": "Bearer ${env:WEATHER_API_KEY}"}
      },
      "enabled": true,
      "tags": ["weather", "api"],
      "created_at": "2026-02-02T10:00:00Z",
      "updated_at": "2026-02-02T10:00:00Z"
    }
  ],
  "total": 1
}
```

---

### Create Custom Tool

```http
POST /tools/custom
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body (HTTP Tool):**
```json
{
  "id": "get_weather",
  "name": "Get Weather",
  "description": "Get current weather for a city",
  "implementation_type": "http",
  "parameters": {
    "city": {
      "type": "string",
      "description": "City name",
      "required": true
    }
  },
  "http_config": {
    "method": "GET",
    "url": "https://api.weather.com/v1/current?city=${city}",
    "headers": {
      "Authorization": "Bearer ${env:WEATHER_API_KEY}"
    },
    "timeout_seconds": 30,
    "response_path": "$.data"
  },
  "enabled": true,
  "tags": ["weather"]
}
```

**Request Body (MCP Tool):**
```json
{
  "id": "read_file_mcp",
  "name": "Read File (MCP)",
  "description": "Read a file using MCP filesystem server",
  "implementation_type": "mcp",
  "parameters": {
    "path": {
      "type": "string",
      "description": "File path",
      "required": true
    }
  },
  "mcp_config": {
    "server_command": "npx",
    "server_args": ["-y", "@anthropic/mcp-server-filesystem", "/path"],
    "tool_name": "read_file",
    "idle_timeout_seconds": 300
  },
  "enabled": true
}
```

**Response:** The created tool object

---

### Get Custom Tool

```http
GET /tools/custom/{tool_id}
Authorization: Bearer <token>
```

**Response:** Single tool object

---

### Update Custom Tool

```http
PUT /tools/custom/{tool_id}
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:** Same as create (all fields optional except those being updated)

**Response:** Updated tool object

---

### Delete Custom Tool

```http
DELETE /tools/custom/{tool_id}
Authorization: Bearer <token>
```

**Response:**
```json
{
  "message": "Tool 'get_weather' deleted"
}
```

---

### Test Custom Tool

```http
POST /tools/custom/{tool_id}/test
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:**
```json
{
  "params": {
    "city": "London"
  }
}
```

**Response:**
```json
{
  "success": true,
  "result": "{\"temperature\": 15, \"conditions\": \"cloudy\"}",
  "execution_time_ms": 234
}
```

---

### Export/Import Custom Tools

```http
GET /tools/custom/export
Authorization: Bearer <token>
```

Returns all tools as a JSON array for backup/transfer.

```http
POST /tools/custom/import
Content-Type: application/json
Authorization: Bearer <token>
```

Import tools from a JSON array.

---

## Sub-Agents API

Manage specialized sub-agents programmatically.

### List Sub-Agents

```http
GET /agents
Authorization: Bearer <token>
```

**Response:**
```json
{
  "agents": [
    {
      "name": "code_reviewer",
      "description": "Reviews code for bugs and style issues",
      "system_prompt": "You are a code review specialist...",
      "tools": ["file_read", "bash_execute"],
      "context_turns": 5,
      "enabled": true
    }
  ]
}
```

---

### Create Sub-Agent

```http
POST /agents
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:**
```json
{
  "name": "code_reviewer",
  "description": "Reviews code for bugs and style issues",
  "system_prompt": "You are a code review specialist. Analyze code for:\n- Bugs and logic errors\n- Security vulnerabilities\n- Style and readability issues\n\nProvide specific line numbers and suggested fixes.",
  "tools": ["file_read", "bash_execute"],
  "context_turns": 5
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Unique identifier (lowercase, underscores) |
| `description` | string | Yes | Brief description |
| `system_prompt` | string | Yes | Instructions for the agent |
| `tools` | array | No | Allowed tool names (empty = all) |
| `context_turns` | int | No | Conversation history (default: 5) |

**Response:** The created agent object

---

### Get Sub-Agent

```http
GET /agents/{agent_name}
Authorization: Bearer <token>
```

**Response:** Single agent object

---

### Update Sub-Agent

```http
PUT /agents/{agent_name}
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:** Same as create (all fields optional)

**Response:** Updated agent object

---

### Delete Sub-Agent

```http
DELETE /agents/{agent_name}
Authorization: Bearer <token>
```

**Response:**
```json
{
  "message": "Agent 'code_reviewer' deleted"
}
```

---

### Test Sub-Agent

```http
POST /agents/{agent_name}/test
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:**
```json
{
  "instruction": "Review this function for potential bugs"
}
```

**Response:**
```json
{
  "success": true,
  "response": "I've analyzed the function and found...",
  "tools_used": ["file_read"],
  "execution_time_ms": 1523
}
```

---

### Export/Import Sub-Agents

```http
GET /agents/export
Authorization: Bearer <token>
```

Returns all agents as a JSON array.

```http
POST /agents/import
Content-Type: application/json
Authorization: Bearer <token>
```

Import agents from a JSON array.

---

## RAG Management API

Manage RAG (Retrieval Augmented Generation) settings and indexes per user.

### Get RAG Settings

```http
GET /users/{user_id}/rag/settings
Authorization: Bearer <token>
```

**Response:**
```json
{
  "user_id": "default",
  "rag_enabled": true,
  "preferences": {
    "max_chunks": 5,
    "include_conversations": true,
    "include_memories": true,
    "include_todos": true,
    "auto_flush": true
  }
}
```

---

### Update RAG Settings

```http
PUT /users/{user_id}/rag/settings
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:**
```json
{
  "rag_enabled": true,
  "max_chunks": 5,
  "include_conversations": true,
  "include_memories": true,
  "include_todos": false,
  "auto_flush": true
}
```

**Response:** Updated settings object

---

### Get RAG Statistics

```http
GET /users/{user_id}/rag/stats
Authorization: Bearer <token>
```

**Response:**
```json
{
  "user_id": "default",
  "total_chunks": 156,
  "chunks_by_type": {
    "conversation": 120,
    "memory": 25,
    "todo": 11
  },
  "index_size_bytes": 524288,
  "last_indexed": "2026-02-02T10:00:00Z"
}
```

---

### Trigger RAG Reindex

```http
POST /users/{user_id}/rag/reindex
Authorization: Bearer <token>
```

Rebuilds the RAG index from scratch.

**Response:**
```json
{
  "message": "Reindexing started",
  "chunks_indexed": 156
}
```

---

### Delete RAG Index

```http
DELETE /users/{user_id}/rag/index
Authorization: Bearer <token>
```

Permanently deletes the user's RAG index.

**Response:**
```json
{
  "message": "RAG index deleted for user default"
}
```

---

## User Tool Preferences API

Manage per-user tool enablement and configuration.

### List User Tools

```http
GET /users/{user_id}/tools
Authorization: Bearer <token>
```

**Response:**
```json
{
  "user_id": "default",
  "tools": [
    {"name": "bash_execute", "enabled": true, "category": "core"},
    {"name": "file_read", "enabled": true, "category": "core"},
    {"name": "browser_navigate", "enabled": false, "category": "browser"}
  ]
}
```

---

### Get Tool Preferences

```http
GET /users/{user_id}/tools/preferences
Authorization: Bearer <token>
```

**Response:**
```json
{
  "user_id": "default",
  "disabled_tools": ["browser_navigate", "browser_click"],
  "disabled_categories": [],
  "tool_configs": {
    "bash_execute": {"timeout_seconds": 60}
  }
}
```

---

### Enable Tool

```http
PUT /users/{user_id}/tools/{tool_name}/enable
Authorization: Bearer <token>
```

**Response:**
```json
{
  "message": "Tool 'browser_navigate' enabled for user default"
}
```

---

### Disable Tool

```http
DELETE /users/{user_id}/tools/{tool_name}/enable
Authorization: Bearer <token>
```

**Response:**
```json
{
  "message": "Tool 'browser_navigate' disabled for user default"
}
```

---

### Enable Tool Category

```http
PUT /users/{user_id}/tools/categories/{category}/enable
Authorization: Bearer <token>
```

Enables all tools in a category (e.g., "browser", "outlook", "memory").

**Response:**
```json
{
  "message": "Category 'browser' enabled for user default",
  "tools_enabled": ["browser_navigate", "browser_click", "browser_type", ...]
}
```

---

### Configure Tool

```http
PUT /users/{user_id}/tools/{tool_name}/config
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:**
```json
{
  "timeout_seconds": 60,
  "max_retries": 3
}
```

**Response:**
```json
{
  "message": "Tool 'bash_execute' configured",
  "config": {"timeout_seconds": 60, "max_retries": 3}
}
```

---

### Reset Tool Preferences

```http
POST /users/{user_id}/tools/reset
Authorization: Bearer <token>
```

Resets all tool preferences to defaults.

**Response:**
```json
{
  "message": "Tool preferences reset for user default"
}
```

---

## Unified Tools API

Access both built-in and custom tools through a unified interface.

### List Unified Tools

```http
GET /users/{user_id}/tools/unified
Authorization: Bearer <token>
```

Returns all tools (built-in + custom) in a unified format.

**Response:**
```json
{
  "user_id": "default",
  "tools": [
    {
      "id": "bash_execute",
      "name": "bash_execute",
      "description": "Execute shell commands...",
      "type": "builtin",
      "category": "core",
      "enabled": true,
      "parameters": {...}
    },
    {
      "id": "get_weather",
      "name": "Get Weather",
      "description": "Get current weather...",
      "type": "custom",
      "category": "custom",
      "enabled": true,
      "parameters": {...}
    }
  ],
  "total": 48
}
```

---

### Enable Unified Tool

```http
PUT /users/{user_id}/tools/unified/{tool_id}/enable
Authorization: Bearer <token>
```

Works for both built-in and custom tools.

**Response:**
```json
{
  "message": "Tool 'get_weather' enabled"
}
```

---

### Update Tool Description

```http
PUT /users/{user_id}/tools/unified/{tool_id}/description
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:**
```json
{
  "description": "Updated description for the tool"
}
```

**Response:**
```json
{
  "message": "Description updated for tool 'get_weather'"
}
```

---

### Configure Unified Tool

```http
PUT /users/{user_id}/tools/unified/{tool_id}/config
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:**
```json
{
  "timeout_seconds": 30,
  "custom_option": "value"
}
```

**Response:**
```json
{
  "message": "Tool 'get_weather' configured",
  "config": {...}
}
```

---

## Tool Categories API

### List Tool Categories

```http
GET /tools/categories
Authorization: Bearer <token>
```

Returns available tool categories.

**Response:**
```json
{
  "categories": {
    "core": ["bash_execute", "file_read", "file_write", "file_list", "web_search", "think", "claude_code", "notify"],
    "memory": ["memory_save", "memory_forget", "memory_clear_all", "personality_set", "rag_search"],
    "todo": ["todo_add", "todo_update", "todo_delete", "todo_list"],
    "self_modify": ["self_modify_rollback"],
    "subagent": ["clear_agent_context", "reload_all"],
    "visibility": ["mute_response"]
  }
}
```

---

## Error Responses

All errors follow this format:

```json
{
  "detail": "Error message here"
}
```

| Status Code | Meaning |
|-------------|---------|
| 401 | Missing or invalid API key |
| 422 | Invalid request body |
| 500 | Internal server error |

---

## CORS

CORS is controlled by `CORS_ORIGINS` in environment settings.

---

## Interactive Documentation

When the server is running:
- **Swagger UI:** http://localhost:8000/docs
- **ReDoc:** http://localhost:8000/redoc
