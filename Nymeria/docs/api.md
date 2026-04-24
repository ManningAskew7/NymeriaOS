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
| `is_self_invoke` | bool | No | `false` | Mark invocation as autonomous/internal. Skips the `message_added` sync event, routes the request through the autonomous prompt path, and publishes `task_started` / `tool_call` / `tool_result` / `workspace_artifact` / `thinking` / `response` / `task_completed` events to the autonomous event bus (visible via `GET /autonomous/stream`). Used by the watchdog worker; gated by the same Bearer-auth check as any `/chat` call. |
| `trigger_override` | string | No | - | Label for autonomous invocations (e.g. `"watchdog"`, `"ticker"`). Becomes part of `task_id` and the `source` field on emitted autonomous events. |

**Response:** Server-Sent Events (SSE)

```
data: {"type": "thinking", "content": "...", "thread_id": "abc123"}
data: {"type": "tool_call", "name": "web_search", "args": {...}, "thread_id": "abc123"}
data: {"type": "tool_result", "name": "web_search", "result": "...", "thread_id": "abc123"}
data: {"type": "workspace_artifact", "tool_call_id": "tool1", "tool_name": "file_write", "path": "/workspace/report.csv", "name": "report.csv", "mime_type": "text/csv", "size_bytes": 1024, "thread_id": "abc123"}
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

**Request Body:** Same as the streaming endpoint, including the optional `is_self_invoke` and `trigger_override` fields. Self-invoke behavior here is identical to `/chat` — it's a pass-through to `agent.chat()` with those flags.

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

Optional query: `include_internal=true` returns system-generated messages (autonomous wake-ups, compaction markers) that are hidden by default.

**Response:**
```json
{
  "thread_id": "abc123",
  "messages": [
    {"id": "abc123-1", "role": "user", "content": "Hello"},
    {
      "id": "abc123-2",
      "role": "assistant",
      "content": "Hi there!",
      "steps": [
        {"type": "thinking", "content": "Reasoning summary..."},
        {"type": "response", "content": "Hi there!"}
      ],
      "intermediate_content": "Reasoning summary..."
    }
  ]
}
```

Assistant `steps` are optional. They appear when a turn has reasoning/thinking, tool calls, or interleaved response chunks. Anthropic typed thinking blocks, OpenAI-compatible `reasoning_content` metadata, and OpenAI Responses `reasoning` summary blocks are returned as `{"type": "thinking"}` steps so desktop and mobile can re-render the same thinking dropdown after history sync.

**Performance note:** latency scales with the checkpoint count for the thread. Compaction prunes pre-compact rows so healthy threads stay under ~100 ms. If you see multi-second latency, check the thread's checkpoint count and the troubleshooting section in [compaction-and-checkpoints.md](./compaction-and-checkpoints.md).

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

**Response:** current thread-available tool list.

### List Optional Tools

```http
GET /tools/optional
Authorization: Bearer <token>
```

Returns optional tools that can be enabled per thread.

### Default Tool Policy

```http
GET /tools/defaults
PUT /tools/defaults
DELETE /tools/defaults
Authorization: Bearer <token>
```

Manage the default tool set applied to newly created threads.

---

## SSE Event Types

| Type | Description | Fields |
|------|-------------|--------|
| `thinking` | Agent's reasoning before tool calls | `content` |
| `tool_call` | Tool being invoked | `name`, `args` |
| `tool_result` | Tool execution result | `name`, `result` |
| `workspace_artifact` | Downloadable file generated by a tool (for example `file_write(..., attach=True)`) | `tool_call_id`, `tool_name`, `path`, `name`, `mime_type`, `size_bytes` |
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
| `workspace_artifact` | Downloadable workspace file generated during the task | `tool_call_id`, `tool_name`, `path`, `name`, `mime_type`, `size_bytes` |
| `response` | Response text chunks | `content` |
| `task_completed` | Execution finished | `notify`, `content`, `summary`, `todo_id` |

**Example Stream:**
```
: heartbeat
data: {"type":"task_started","thread_id":"abc123","task_id":"todo-xyz","prompt":"Check inbox","todo_id":"xyz"}
data: {"type":"thinking","content":"I'll check the inbox now..."}
data: {"type":"tool_call","id":"tool1","name":"bash_execute","args":{"command":"ls ~/inbox"}}
data: {"type":"tool_result","id":"tool1","name":"bash_execute","result":"email1.txt\nemail2.txt"}
data: {"type":"workspace_artifact","tool_call_id":"tool2","tool_name":"file_write","path":"/workspace/report.csv","name":"report.csv","mime_type":"text/csv","size_bytes":1024}
data: {"type":"response","content":"Found 2 new emails in inbox."}
data: {"type":"task_completed","notify":false,"content":"Found 2 new emails.","todo_id":"xyz"}
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

**Response:** includes LLM settings plus voice runtime settings such as `tts_provider`, `tts_base_url`, `tts_model`, `tts_voice`, `tts_output_format`, `tts_speed`, `stt_provider`, `stt_base_url`, `stt_model`, `stt_language`, and `voice_default_thread_id`.

### LLM Runtime Diagnostics

```http
GET /settings/llm/runtime
Authorization: Bearer <token>
```

Returns the currently active runtime provider/model, effective max tokens, source env files, and OpenRouter key diagnostics when applicable.

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
  "llm_reasoning_effort": "medium",
  "llm_use_model_defaults": false
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
| `llm_use_model_defaults` | bool | true/false | Use model-specific defaults for temperature/top_p/frequency_penalty |

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

### List OpenRouter Models

```http
GET /models
Authorization: Bearer <token>
```

Returns cached OpenRouter model metadata. The backend fetches model data from the OpenRouter API and caches it for 1 hour. Returns an empty list if the cache hasn't been populated yet (non-critical enrichment data).

**Response:**
```json
[
  {
    "id": "anthropic/claude-sonnet-4",
    "name": "Claude Sonnet 4",
    "context_length": 200000,
    "max_completion_tokens": 16384,
    "pricing_prompt": 0.000003,
    "pricing_completion": 0.000015,
    "supported_parameters": ["temperature", "top_p", "tools", "reasoning", "max_tokens"],
    "input_modalities": ["text", "image", "file"],
    "tokenizer": "Claude",
    "default_temperature": 1.0,
    "default_top_p": null,
    "default_frequency_penalty": null
  }
]
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | OpenRouter model identifier (e.g. `anthropic/claude-sonnet-4`) |
| `name` | string | Human-readable model name |
| `context_length` | int | Maximum context window in tokens |
| `max_completion_tokens` | int \| null | Maximum output tokens (null if unknown) |
| `pricing_prompt` | float \| null | Cost per input token in USD |
| `pricing_completion` | float \| null | Cost per output token in USD |
| `supported_parameters` | string[] | API parameters the model accepts (used for smart parameter gating) |
| `input_modalities` | string[] | Supported input types: `text`, `image`, `file` |
| `tokenizer` | string \| null | Tokenizer family: `Claude`, `GPT`, `Llama3`, etc. |
| `default_temperature` | float \| null | Model's default temperature (shown when "Use model defaults" is enabled) |
| `default_top_p` | float \| null | Model's default top_p |
| `default_frequency_penalty` | float \| null | Model's default frequency penalty |

---

### List Threads

```http
GET /threads?user_id=default
Authorization: Bearer <token>
```

Returns all threads with server-authoritative metadata (titles, pins, platform).

**Response:**
```json
{
  "threads": [
    {
      "id": "abc123",
      "title": "Research Python tutorials",
      "title_source": "auto",
      "pinned": false,
      "platform": "desktop",
      "created_at": "2026-02-27T10:00:00Z",
      "updated_at": "2026-02-27T10:30:00Z"
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `title` | Server-authoritative display title |
| `title_source` | `"auto"` (generated from first message), `"user"` (manual rename), `"callable"` (synced from callable_name) |
| `pinned` | Whether thread is pinned to top |
| `platform` | Origin surface: `"desktop"`, `"callable"`, `"discord"`, `"telegram"`, `"slack"`, `"webhook"` |

---

### Update Thread Metadata

```http
PATCH /threads/{thread_id}/metadata?user_id=default
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:** (all fields optional)
```json
{
  "title": "New title",
  "pinned": true
}
```

Updates the thread's server-side metadata. If the thread is callable, renaming also updates its `callable_name` in thread config and rebuilds the tool registry (so the LLM sees the new tool name). Name collisions with core tools or other callables are silently skipped.

**Response:**
```json
{
  "status": "ok",
  "thread_id": "abc123",
  "updated_fields": ["title", "title_source"]
}
```

---

### Stop Thread

```http
POST /threads/{thread_id}/stop
Authorization: Bearer <token>
```

Aborts a running stream on the thread. Cascades to any active callable child threads.

**Response:**
```json
{
  "status": "ok",
  "message": "Abort signal sent for thread abc123"
}
```

---

## TODO Management API

Manage TODO items with optional scheduling for autonomous execution.

### List Users with TODOs

```http
GET /todos/users
Authorization: Bearer <token>
```

Returns all user IDs that have a TODO list on disk. Used by the watchdog worker to enumerate users before polling each one's TODOs; also useful for dashboards that need to list known users without hard-coding them.

No query parameters.

**Response:**
```json
["default", "discord_699436710118817823", "telegram_5551234567"]
```

---

### List TODOs

```http
GET /todos?user_id=default&filter_status=all
Authorization: Bearer <token>
```

**Query Parameters:**
| Parameter | Default | Description |
|-----------|---------|-------------|
| `user_id` | `"default"` | User ID |
| `filter_status` | active only | Filter: `all`, `pending`, `in_progress`, `done` |
| `thread_id` | - | Filter TODOs to a specific thread |

**Response:**
```json
{
  "user_id": "default",
  "items": [
    {
      "id": "abc123",
      "task": "Check inbox",
      "status": "pending",
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
  "scheduled_for": "30m",
  "recurrence": "daily",
  "thread_id": "optional-thread-id",
  "notes": "Optional notes"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task` | string | Yes | Task description |
| `scheduled_for` | string | No | Relative (`30s`, `5m`, `2h`, `1d`, `1w`) or absolute datetime |
| `recurrence` | string | No | Valid recurrence pattern |
| `thread_id` | string | No | Thread for autonomous output, defaults to a user-scoped default thread |
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
  "scheduled_for": "2h",
  "thread_id": "thread-xyz",
  "clear_schedule": false,
  "recurrence": "weekly",
  "clear_recurrence": false
}
```

| Field | Type | Description |
|-------|------|-------------|
| `task` | string | Updated task text |
| `status` | string | `pending`, `in_progress`, `done` |
| `notes` | string | Set or update notes |
| `scheduled_for` | string | Set or update next scheduled execution |
| `recurrence` | string | Set or update recurrence pattern |
| `thread_id` | string | Thread for autonomous output |
| `clear_schedule` | bool | Remove schedule if `true` |
| `clear_recurrence` | bool | Remove recurrence if `true` |

**Response:** Updated TODO object

---

### Complete TODO

```http
POST /todos/{todo_id}/complete?user_id=default
Authorization: Bearer <token>
```

Marks the TODO as done. Recurring TODOs are rescheduled by the backend rather than simply being unscheduled.

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
      "metadata": {"todo_id": "abc123"}
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

## Workspace API

### Download Workspace File

Download a file from the workspace directory. Used by bot clients and the desktop artifact viewer to deliver `file_write(..., attach=True)` outputs to users.

```http
GET /workspace/download?path=/workspace/report.csv
```

**Query Parameters:**
- `path` (required): Absolute file path within the workspace directory.

**Responses:**
- `200`: File content with appropriate `Content-Type` and `Content-Disposition` headers.
- `403`: Path is outside the workspace directory.
- `404`: File does not exist.

**Security:** Only files within `NYMERIA_WORKSPACE_DIR` (default `/workspace`) can be served. Paths are resolved and checked against the workspace root to prevent traversal. This same workspace boundary is what controls whether `file_write(..., attach=True)` produces a deliverable artifact at all.

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

## Callable Threads API

Callable threads replace the old sub-agent system. Any thread marked `callable=True` becomes a directly invocable tool visible to other threads.

### List Callable Threads

```http
GET /agents/threads
Authorization: Bearer <token>
```

Returns callable thread configs. The response shape is `{"threads": [...], "total": N}`.

### Create Callable Thread

```http
POST /agents/threads
Authorization: Bearer <token>
Content-Type: application/json
```

Creates a callable thread directly from the API.

---

### Thread Config (Per-Thread Settings)

```http
GET /threads/{thread_id}/config
Authorization: Bearer <token>
```

Returns per-thread configuration including callable settings, custom instructions, LLM overrides, and tool enablement.

```http
PATCH /threads/{thread_id}/config
Content-Type: application/json
Authorization: Bearer <token>
```

Updates thread config. Key fields for callable threads:

| Field | Type | Description |
|-------|------|-------------|
| `callable` | bool | Whether this thread is callable as a tool |
| `callable_name` | string | Tool name visible to the LLM (must be unique) |
| `callable_description` | string | Tool description shown to the LLM |
| `custom_instructions` | string | System prompt for this thread |
| `disabled_tools` | array | Tool names to exclude |
| `enabled_tools` | array | Optional tool names to include |
| `llm_provider` | string | Override LLM provider |
| `llm_model` | string | Override model |
| `llm_config.base_url` | string | Per-thread provider base URL. For CLIProxy sidecars, this is the URL reachable from the Nymeria backend container, e.g. `http://cli-proxy-api-latest:8317/v1`. |
| `llm_config.api_key` | string | Per-thread provider API key. For CLIProxy sidecars, this is the local sidecar gatekeeper key, not an upstream OpenAI key. |
| `llm_temperature` | float | Override temperature |
| `llm_config.openai_api_mode` | string | OpenAI-only API mode: `chat_completions` or `responses`. Use `responses` for CLIProxy Codex OAuth threads that need native Responses reasoning/tool blocks replayed from the checkpoint. |

**Callable thread naming:** The thread's sidebar title always equals `callable_name`. Renaming the thread via `PATCH /threads/{id}/metadata` automatically updates `callable_name` and rebuilds the tool registry.

```http
DELETE /threads/{thread_id}/config
Authorization: Bearer <token>
```

Resets thread config to defaults.

---

## RAG Management API

Manage RAG (Retrieval Augmented Generation) settings and indexes per user.

`rag_enabled` defaults to `true` as of 2026-04. Existing profiles created before that are migrated once on load (watermarked by `opt_in.rag_migrated`). To disable, set `rag_enabled=false` via this API or the `rag_settings` tool — the watermark prevents re-flipping.

Conversation indexing happens automatically in four places: per turn, before `/compact` (manual + auto), before `/threads/{id}/clear`, and chunks for a thread are removed when the thread is deleted. See `Nymeria/docs/architecture.md` → "RAG (Semantic Conversation Recall)".

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

Manage per-user tool visibility and configuration.

### List User Tools

```http
GET /users/{user_id}/tools
Authorization: Bearer <token>
```

Returns the user's tool inventory and enabled state.

---

### Get Tool Preferences

```http
GET /users/{user_id}/tools/preferences
Authorization: Bearer <token>
```

Returns disabled tools plus per-tool config overrides.

---

### Configure Tool

```http
PUT /users/{user_id}/tools/{tool_name}/config
Content-Type: application/json
Authorization: Bearer <token>
```

Update config for a specific tool.

---

### Reset Tool Preferences

```http
POST /users/{user_id}/tools/reset
Authorization: Bearer <token>
```

Resets all tool preferences to defaults.

---

## Unified Tools API

Access built-in and custom tools through one API surface. This is also where enablement and description updates now live.

### List Unified Tools

```http
GET /users/{user_id}/tools/unified
Authorization: Bearer <token>
```

Returns all tools (built-in + custom) in a unified format.

### Enable Unified Tool

```http
PUT /users/{user_id}/tools/unified/{tool_id}/enable
Authorization: Bearer <token>
```

Enable or disable a built-in or custom tool through the unified tool identity.

### Update Unified Tool Description

```http
PUT /users/{user_id}/tools/unified/{tool_id}/description
Authorization: Bearer <token>
```

Override the user-visible description for a tool.

### Update Unified Tool Config

```http
PUT /users/{user_id}/tools/unified/{tool_id}/config
Authorization: Bearer <token>
```

Update configuration for a unified tool entry.

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

## MCP Servers, Devices, and Voice

### MCP Servers

```http
GET /mcp-servers
POST /mcp-servers
GET /mcp-servers/{server_id}
PUT /mcp-servers/{server_id}
DELETE /mcp-servers/{server_id}
POST /mcp-servers/{server_id}/discover
POST /mcp-servers/{server_id}/test
Authorization: Bearer <token>
```

Manage MCP server definitions, trigger tool discovery, and optionally auto-enable discovered MCP tools for a thread when creating a server.

### Device Registration

```http
POST /devices/register
DELETE /devices/{token}
Authorization: Bearer <token>
```

Register or unregister FCM device tokens for push notifications.

### Voice Endpoints

```http
POST /voice/chat
POST /voice/tts
POST /voice/stt
Authorization: Bearer <token>
```

- `/voice/chat` accepts audio upload, runs STT -> agent -> TTS, and returns audio.
- `/voice/tts` accepts JSON text and returns synthesized audio.
- `/voice/stt` accepts audio and returns transcribed text.

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
    "todo": ["todo", "todo_delete", "todo_list"],
    "self_modify": ["self_modify_rollback"],
    "subagent": ["clear_agent_context", "reload_all"]
  }
}
```

---

## Triggers API

Event-driven automations that fire actions when source conditions are met.

### List Sources

```http
GET /triggers/sources/list
Authorization: Bearer <token>
```

Returns all registered trigger sources with metadata.

**Response:**
```json
{
  "sources": {
    "webhook": {
      "name": "webhook",
      "description": "Fires when an HTTP POST is received",
      "config_schema": { ... },
      "category": "custom",
      "icon": "bolt",
      "setup_guide": "...",
      "template_variables": ["fired_at", "source_ip"],
      "example_config": {},
      "requires_auth": null
    }
  }
}
```

### Create Trigger

```http
POST /triggers
Authorization: Bearer <token>
Content-Type: application/json
```

```json
{
  "name": "RSS Monitor",
  "source_type": "rss",
  "source_config": { "url": "https://example.com/feed.xml" },
  "action_type": "agent_prompt",
  "action_config": { "prompt_template": "New article: {title} - {summary}" },
  "conditions": [{ "field": "title", "operator": "contains", "value": "release" }],
  "cooldown_seconds": 60,
  "enabled": true
}
```

### List Triggers

```http
GET /triggers?thread_id=<optional>
Authorization: Bearer <token>
```

### Update Trigger

```http
PATCH /triggers/{trigger_id}
Authorization: Bearer <token>
Content-Type: application/json
```

Accepts any subset of: `name`, `enabled`, `source_config`, `action_type`, `action_config`, `conditions`, `cooldown_seconds`.

### Delete Trigger

```http
DELETE /triggers/{trigger_id}
Authorization: Bearer <token>
```

### Test Trigger (Dry Run)

```http
POST /triggers/{trigger_id}/test
Authorization: Bearer <token>
```

Returns a preview using sample event data without actually executing.

**Response:**
```json
{
  "sample_event": { "title": "...", "link": "..." },
  "rendered_output": "New article: ...",
  "action_type": "agent_prompt",
  "template_variables_used": ["title", "summary"],
  "conditions_pass": true
}
```

### Trigger Execution History

```http
GET /triggers/{trigger_id}/executions?limit=50
Authorization: Bearer <token>
```

Per-trigger execution history.

```http
GET /triggers/executions/recent?limit=50
Authorization: Bearer <token>
```

All recent executions across triggers.

### Fire Webhook

```http
POST /triggers/fire/{trigger_id}
Content-Type: application/json
```

Push-based endpoint for webhook triggers. Accepts any JSON body.

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
