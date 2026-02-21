# Nymeria Tools Reference

Nymeria has a three-tier tool system: **18 core tools** always loaded, **4 sub-agent wrapper tools** generated dynamically, and **17 optional tools** (13 Outlook + 4 trigger) available for per-thread enabling. Use `get_all_tools_with_agents()` to get all 22 default tools.

## Summary Table

### Core Tools (18)

| # | Tool | Category | Security | Default | Description |
|---|------|----------|----------|---------|-------------|
| 1 | `bash_execute` | Core | MODERATE | On | Execute shell commands |
| 2 | `file_read` | Core | SAFE | On | Read file contents |
| 3 | `file_write` | Core | MODERATE | On | Write content to files |
| 4 | `web_search` | Core | SAFE | On | Search the web via Perplexity |
| 5 | `consult` | Core | SAFE | On | Ask Gemini for a second opinion (OpenRouter) |
| 6 | `claude_code` | Core | MODERATE | On | Invoke Claude Code CLI for coding tasks |
| 7 | `memory_save` | Memory | SAFE | On | Save a user memory |
| 8 | `memory_forget` | Memory | SAFE | On | Remove a memory by key |
| 9 | `memory_list` | Memory | SAFE | On | List all saved memories and preferences |
| 10 | `personality_set` | Memory | SAFE | On | Set communication preferences |
| 11 | `rag_search` | Memory | SAFE | On | Semantic search over past conversations |
| 12 | `todo` | TODO | SAFE | On | Create or update a TODO item |
| 13 | `todo_delete` | TODO | SAFE | On | Delete a TODO permanently |
| 14 | `todo_list` | TODO | SAFE | On | List TODO items |
| 15 | `clear_agent_context` | Subagent | SAFE | On | Clear sub-agent conversation context |
| 16 | `reload_all` | Subagent | MODERATE | On | Reload all tools, agents, and trigger sources |
| 17 | `self_modify_rollback` | Self-modify | **SENSITIVE** | **Off** | Rollback a self-modification from backup |
| 18 | `notify` | Core | MODERATE | On | Send notifications (Telegram/Discord/Slack) |

### Optional: Trigger Tools (4)

Not loaded by default. Enable per-thread via thread config, or use through SelfModifyAgent.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `trigger_create` | Trigger | MODERATE | Create an event-driven trigger |
| 2 | `trigger_list` | Trigger | SAFE | List triggers |
| 3 | `trigger_update` | Trigger | MODERATE | Update a trigger |
| 4 | `trigger_delete` | Trigger | MODERATE | Delete a trigger |

### Sub-Agent Wrapper Tools (4)

| Tool | LLM | Context Turns | Internal Tools |
|------|-----|---------------|----------------|
| `BrowserAgent` | `google/gemini-3-flash-preview` (OpenRouter) | 10 | 9 browser tools |
| `OutlookAgent` | `x-ai/grok-4.1-fast` (OpenRouter) | 8 | 13 Outlook tools |
| `CalendarAgent` | `x-ai/grok-4.1-fast` (OpenRouter) | 8 | 12 calendar tools |
| `SelfModifyAgent` | `anthropic/claude-opus-4.5` (OpenRouter) | 5 | 7 self-modify tools |

---

## Core System Tools

### bash_execute

Execute shell commands on the local system.

```python
bash_execute(command: str, working_directory: Optional[str] = None, timeout_seconds: int = 120)
```

**Parameters:**
- `command` (`str`): Shell command to execute
- `working_directory` (`Optional[str]`, default `None`): Directory to run the command in
- `timeout_seconds` (`int`, default `120`): Maximum execution time in seconds

**Returns:** Command output (stdout + stderr combined) or error message. Non-zero exit codes are appended. Output truncated at 50,000 characters.

**Security:** MODERATE — runs commands without sandboxing.

---

### file_read

Read the contents of a file.

```python
file_read(file_path: str, encoding: str = "utf-8", max_lines: Optional[int] = None)
```

**Parameters:**
- `file_path` (`str`): Absolute or relative path to the file
- `encoding` (`str`, default `"utf-8"`): File encoding
- `max_lines` (`Optional[int]`, default `None`): Limit number of lines to read

**Returns:** File contents, or error message.

**Limits:** 10 MB maximum file size.

---

### file_write

Write content to a file.

```python
file_write(file_path: str, content: str, encoding: str = "utf-8", create_directories: bool = True, append: bool = False)
```

**Parameters:**
- `file_path` (`str`): Absolute or relative path to the file
- `content` (`str`): Content to write
- `encoding` (`str`, default `"utf-8"`): File encoding
- `create_directories` (`bool`, default `True`): Create parent directories if they don't exist
- `append` (`bool`, default `False`): Append to file instead of overwriting

**Returns:** Success/error message with character count.

**Protected paths:** Writes to `nymeria/core/`, `nymeria/config/`, `nymeria/triggers/`, `nymeria/gateway/`, and `nymeria/__init__.py` are blocked. Use the SelfModifyAgent for those directories.

---

### ~~file_list~~ (removed)

**Deprecated.** Redundant with `bash_execute` — use `bash_execute("ls -la /path")` or `bash_execute("find /path -name '*.py'")` instead. Removed from `ALL_TOOLS` and `TOOL_METADATA`.

---

### web_search

Search the web using the Perplexity API.

```python
web_search(query: str, search_depth: Optional[str] = None, max_sources: Optional[int] = None)
```

**Parameters:**
- `query` (`str`): Search query
- `search_depth` (`Optional[str]`): `"quick"` (sonar), `"standard"` (sonar-pro), or `"deep"` (sonar-deep-research). Defaults to `settings.perplexity_search_model`.
- `max_sources` (`Optional[int]`): Maximum sources to cite (1-10, default 5)

**Returns:** Search results with citations.

**Requires:** `PERPLEXITY_API_KEY` environment variable.

**Timeouts:** 60s for quick/standard, 180s for deep research. Max tokens: 2000 for quick/standard, 4000 for deep.

---

### consult

Ask another AI (Gemini) for a second opinion. Sends the question to a Gemini model via OpenRouter with reasoning tokens enabled and returns its analysis. Use when you want an outside perspective, need help with a hard problem, or want to cross-check your own reasoning.

> **Previously named `think`.** Renamed to `consult` to clarify that this is an external LLM call (costs credits, takes seconds), not internal reasoning.

```python
consult(question: str, context: Optional[str] = None, model: Optional[str] = None)
```

**Parameters:**
- `question` (`str`): The question or problem to get help with
- `context` (`Optional[str]`, default `None`): Additional context to include
- `model` (`Optional[str]`, default `None`): Model alias — `"gemini-3-pro"` (default), `"gemini-2.5-pro"`, or `"gemini-2.5-flash"`

**Model mapping:**

| Alias | OpenRouter model ID |
|-------|-------------------|
| `gemini-3-pro` (default) | `google/gemini-3-pro-preview` |
| `gemini-2.5-pro` | `google/gemini-2.5-pro` |
| `gemini-2.5-flash` | `google/gemini-2.5-flash-preview` |

**Returns:** Gemini's reasoning text, content, and reasoning token count.

**Requires:** `OPENROUTER_API_KEY` environment variable. Uses OpenRouter credits.

**Config:** temperature=1.0, max_tokens=16000, reasoning enabled. Timeout: 180s.

---

### claude_code

Invoke Claude Code in headless mode to create, modify, or analyze code.

```python
claude_code(prompt: str, working_dir: Optional[str] = None, model: str = "sonnet",
            allow_edit: bool = True, allow_bash: bool = True, timeout: int = 300)
```

**Parameters:**
- `prompt` (`str`): The coding task or question
- `working_dir` (`Optional[str]`, default `None`): Directory to run in (defaults to current directory)
- `model` (`str`, default `"sonnet"`): Model — `"sonnet"`, `"opus"`, or `"haiku"`
- `allow_edit` (`bool`, default `True`): Allow Claude Code to edit files
- `allow_bash` (`bool`, default `True`): Allow Claude Code to run commands
- `timeout` (`int`, default `300`): Timeout in seconds

**Returns:** Claude Code's response or error message. Output truncated at 50,000 characters.

**Requires:** `claude` CLI binary in PATH (install with `npm install -g @anthropic-ai/claude-code`).

**Tools passed to Claude Code:** Always includes `Read`. Adds `Edit` if `allow_edit=True`, `Bash` if `allow_bash=True`.

---

## Memory Tools

Memories are **automatically injected** into Nymeria's system prompt. The `memory_list` tool provides an explicit way for models to retrieve exact keys before updating or deleting.

> **Implementation note:** `memory_save`, `memory_forget`, `memory_list`, `personality_set`, and `rag_search` all accept a `config: Annotated[RunnableConfig, InjectedToolArg]` parameter that is automatically injected by LangGraph. The LLM never passes this parameter.

### memory_save

Save a memory about the user to persistent storage.

```python
memory_save(key: str, value: str)
```

**Parameters:**
- `key` (`str`): Category identifier (e.g., `"user_name"`, `"occupation"`)
- `value` (`str`): Information to remember (truncated to 1000 characters)

**Returns:** Confirmation message, or error if limit reached.

**Behavior:**
- If a memory with the same `key` exists, it's updated (not duplicated).
- Also indexed in RAG for semantic search if RAG is enabled.

**Limits:** 100 memories per user (`MAX_MEMORIES` in `UserProfile`).

---

### memory_forget

Remove a memory or personality preference by key.

```python
memory_forget(key: str)
```

**Parameters:**
- `key` (`str`): Memory key or personality trait to delete

**Returns:** Confirmation, or error with list of available keys (memories + personality traits) if not found.

**Behavior:** Tries memories first, then personality preferences. Use `memory_list` to see exact keys before calling.

---

### memory_list

List all saved memories and personality preferences for the current user.

```python
memory_list()
```

**Returns:** Formatted list of all memories (key: value) and personality preferences (trait: value), or a message indicating no memories are stored.

**Notes:**
- Use this before `memory_forget` to get exact key names.
- Memories are also auto-injected into the system prompt, but weaker models may struggle to extract exact keys from long prompts.

---

### ~~memory_clear_all~~ (removed)

Removed — a cheap model could hallucinate this call and wipe all user memories irreversibly. Delete memories individually with `memory_forget` instead.

---

### personality_set

Set a communication/personality preference.

```python
personality_set(trait: str, value: str)
```

**Parameters:**
- `trait` (`str`): Preference category (e.g., `"tone"`, `"verbosity"`, `"expertise_level"`)
- `value` (`str`): Desired behavior

**Returns:** Confirmation message.

---

### rag_search

Search past conversations and memories for relevant context using semantic vector search.

```python
rag_search(query: str, max_results: int = 5)
```

**Parameters:**
- `query` (`str`): What to search for
- `max_results` (`int`, default `5`): Maximum results (clamped 1-10)

**Returns:** Formatted results with content type, relevance score, and content. Results filtered by user's RAG preferences (`include_conversations`, `include_memories`, `include_todos`).

**Requires:** RAG must be enabled for the user (`opt_in.rag_enabled`).

---

## TODO Tools

TODOs are the **primary driver for autonomous operation**. Active TODOs are automatically injected into the system prompt. Every TODO must have a `scheduled_for` time — TODOs are for the agent's autonomous work queue, not a general task list.

> **Implementation note:** `todo`, `todo_delete`, and `todo_list` all accept an injected `config` parameter for user/thread identification. The LLM never passes this.
>
> **Migration note:** `todo_add` and `todo_update` were merged into the single `todo` tool. Priority, deadline, blocked status, and the permanent flag have been removed.

### todo

Create or update a TODO item. Omit `todo_id` to create; provide it to update.

```python
todo(todo_id: Optional[str] = None, task: Optional[str] = None,
     scheduled_for: Optional[str] = None, status: Optional[str] = None,
     notes: Optional[str] = None, recurrence: Optional[str] = None,
     clear_schedule: bool = False, clear_recurrence: bool = False)
```

**Parameters:**
- `todo_id` (`Optional[str]`): Omit to create a new TODO, provide the 8-character ID to update an existing one
- `task` (`str`): Task description — **required** for create, optional for update
- `scheduled_for` (`str`): When to execute — **required** for create, optional for update. Formats:
  - Relative: `"30s"`, `"5m"`, `"1h"`, `"1d"` (seconds, minutes, hours, days)
  - Absolute: `"YYYY-MM-DD HH:MM[:SS]"` or `"YYYY-MM-DDTHH:MM[:SS]"` (user timezone)
- `status` (`Optional[str]`): `"pending"`, `"in_progress"`, or `"done"` (update only)
- `notes` (`Optional[str]`): Add or update notes (max 1000 characters)
- `recurrence` (`Optional[str]`): `"5min"`, `"10min"`, `"15min"`, `"30min"`, `"hourly"`, `"daily"`, `"weekly"`, `"monthly"`
- `clear_schedule` (`bool`, default `False`): Remove scheduled time (update only)
- `clear_recurrence` (`bool`, default `False`): Remove recurrence pattern (update only)

**Returns:** Confirmation with TODO ID and details, or error.

**Limits:** 50 active TODOs per user (`MAX_TODOS` in `TodoList`).

**Statuses:** `pending` (default), `in_progress`, `done`. Use `todo(todo_id=..., status="done")` to complete a TODO.

**Recurring TODOs:** Recurring TODOs **auto-reschedule when marked done** — regardless of whether the ticker executed them or the agent/user marked them done manually. The next `scheduled_for` is calculated from the `recurrence` pattern and the status resets to `pending`. This applies to all completion paths: the `todo` tool, the REST API, and the MCP server. To permanently stop a recurring TODO, use `todo(todo_id=..., clear_recurrence=True)` or `todo_delete`.

**Auto-purge:** Non-recurring completed TODOs are automatically archived after 7 days by the ticker daemon.

---

### todo_delete

Delete a TODO permanently. No archive — immediately removed. Cancels any scheduled execution.

```python
todo_delete(todo_id: str)
```

**Parameters:**
- `todo_id` (`str`): The 8-character TODO ID

**Returns:** Confirmation with deleted task text, or error.

---

### todo_list

List TODO items. Shows active (non-done) by default.

```python
todo_list(filter_status: Optional[str] = None)
```

**Parameters:**
- `filter_status` (`Optional[str]`): `"pending"`, `"in_progress"`, `"done"`, or `"all"`

**Returns:** Formatted list sorted by: status (in_progress first, then pending, then done), then scheduled time, then creation date.

**Status icons:** `[ ]` pending, `[>]` in_progress, `[x]` done.

---

## Notification Tool

### notify

Send notifications to messaging platforms (Telegram, Discord, Slack).

```python
notify(message: str, platform: Literal["auto", "telegram", "discord", "slack"] = "auto")
```

**Parameters:**
- `message` (`str`): The message text to send
- `platform` (`Literal["auto", "telegram", "discord", "slack"]`, default `"auto"`): Target platform. `"auto"` tries all configured platforms.

**Returns:** Success/error message.

**Requires:** Platform-specific credentials in `.env`:
- Telegram: `TELEGRAM_BOT_TOKEN` + `TELEGRAM_DEFAULT_CHAT_ID`
- Discord: `DISCORD_WEBHOOK_URL`
- Slack: `SLACK_WEBHOOK_URL`

**Behavior in auto mode:** Tries all configured platforms. If any succeed, returns the success messages (failures are not reported in mixed outcomes). If all fail, returns all errors. If none are configured, returns an error listing the required env vars.

---

## Agent Management Tools

### clear_agent_context

Clear the conversation context for a sub-agent, making it start fresh.

```python
clear_agent_context(agent_name: str)
```

**Parameters:**
- `agent_name` (`str`): Name of the sub-agent (e.g., `"BrowserAgent"`)

**Returns:** Success or info message.

---

### reload_all

Reload all tools, agents, and trigger sources. Call after SelfModifyAgent creates/modifies code, or after manual file edits.

```python
reload_all()
```

**Returns:** Count of reloaded tools and trigger sources.

**Important:** Due to how LangGraph works, newly created tools are NOT available in the same conversation turn. They work on the next user message.

---

### self_modify_rollback

Rollback a file to its previous version from a SelfModifyAgent backup.

```python
self_modify_rollback(file_path: str)
```

**Parameters:**
- `file_path` (`str`): Path to file to rollback (e.g., `"nymeria/tools/my_tool.py"`)

**Returns:** Success or error message.

**Security:** **SENSITIVE** — disabled by default. Requires explicit opt-in via user tool preferences or per-thread config. This is the only core tool with `SecurityLevel.SENSITIVE`.

---

## Trigger Tools (Optional)

Event-driven automation — triggers fire agent prompts or actions in response to external events. These complement recurring TODOs, which handle time-based work.

> **Note:** Trigger tools are **not loaded by default** for the main agent. They are available in `OPTIONAL_TOOLS` for per-thread enabling, and are always available to SelfModifyAgent.

### trigger_create

Create a new event-driven trigger.

```python
trigger_create(name: str, source_type: str, action_type: str, action_config: dict,
               source_config: Optional[dict] = None, cooldown_seconds: int = 0)
```

**Parameters:**
- `name` (`str`): Human-friendly trigger name (e.g., `"Wake-up morning briefing"`)
- `source_type` (`str`): Event source type. Use `"webhook"` for HTTP push triggers. Call `trigger_list_sources()` to see available sources.
- `action_type` (`str`): What to do when triggered:
  - `"agent_prompt"` — send a prompt to the agent (most powerful, triggers an LLM call)
  - `"notify"` — send a notification to the user (no LLM call)
  - `"create_todo"` — create a TODO item (no LLM call)
- `action_config` (`dict`): Action-specific configuration:
  - `agent_prompt`: `{"prompt_template": "...", "thread_id": "optional"}`
  - `notify`: `{"message_template": "...", "platform": "auto"}`
  - `create_todo`: `{"task_template": "..."}`
  - Templates support `{variable}` interpolation from event data.
- `source_config` (`Optional[dict]`, default `None`): Source-specific config (e.g., `{"secret": "mykey"}` for webhooks)
- `cooldown_seconds` (`int`, default `0`): Minimum seconds between trigger firings

**Returns:** Success message with trigger ID and webhook URL (for webhook sources), or error.

**Examples:**
```python
trigger_create("Wake-up briefing", "webhook", "agent_prompt",
    {"prompt_template": "User woke up at {fired_at}. Create morning briefing."})

trigger_create("Deployment alert", "webhook", "notify",
    {"message_template": "Deploy event: {status}"}, {"secret": "s3cr3t"}, cooldown_seconds=60)
```

---

### trigger_list

List all event triggers with their status and configuration.

```python
trigger_list(enabled_only: bool = False)
```

**Parameters:**
- `enabled_only` (`bool`, default `False`): If `True`, only show enabled triggers

**Returns:** Formatted list of triggers with ID, status (ON/OFF), name, source type, action type, fire count, and last fired timestamp.

---

### trigger_update

Update an existing trigger's configuration or enable/disable it.

```python
trigger_update(trigger_id: str, name: Optional[str] = None, enabled: Optional[bool] = None,
               source_config: Optional[dict] = None, action_type: Optional[str] = None,
               action_config: Optional[dict] = None, cooldown_seconds: Optional[int] = None)
```

**Parameters:**
- `trigger_id` (`str`): The 8-char trigger ID
- `name` (`Optional[str]`): New display name
- `enabled` (`Optional[bool]`): Enable (`True`) or disable (`False`) the trigger
- `source_config` (`Optional[dict]`): Updated source configuration
- `action_type` (`Optional[str]`): New action type
- `action_config` (`Optional[dict]`): New action config
- `cooldown_seconds` (`Optional[int]`): New cooldown in seconds

**Returns:** Success or error message. Returns error if no updates are specified.

---

### trigger_delete

Delete a trigger permanently.

```python
trigger_delete(trigger_id: str)
```

**Parameters:**
- `trigger_id` (`str`): The 8-char trigger ID

**Returns:** Success or error message.

---

## Sub-Agent Wrapper Tools

Sub-agents are specialized assistants with their own system prompts, LLMs, and tool restrictions. They appear as **directly callable tools** — each generated by `agents/tool_factory.py`, which wraps `SubAgentExecutor.invoke()`.

```python
# All agent wrapper tools have this signature:
AgentName(task: str) -> str
```

The `task` parameter is the instruction for the sub-agent. An injected `config` parameter provides user context.

### BrowserAgent

Autonomous browser control — navigate, click, type, extract data from websites.

| Setting | Value |
|---------|-------|
| LLM | `google/gemini-3-flash-preview` via OpenRouter |
| Temperature | 0.3 |
| Context turns | 10 |
| Required env | `OPENROUTER_API_KEY` |
| Internal tools | 9 browser tools (see below) |

### OutlookAgent

Email management — read, send, search, and organize Outlook emails via Microsoft Graph API.

| Setting | Value |
|---------|-------|
| LLM | `x-ai/grok-4.1-fast` via OpenRouter |
| Temperature | 0.3 |
| Context turns | 8 |
| Required env | `OPENROUTER_API_KEY` |
| Internal tools | 13 Outlook tools (see below) |

### CalendarAgent

Google Calendar management — list, create, update, delete events and manage Google accounts.

| Setting | Value |
|---------|-------|
| LLM | `x-ai/grok-4.1-fast` via OpenRouter |
| Temperature | 0.3 |
| Context turns | 8 |
| Required env | `OPENROUTER_API_KEY`, `GOOGLE_OAUTH_CREDENTIALS` |
| Internal tools | 12 calendar tools (see below) |

### SelfModifyAgent

Code modification agent — creates/modifies tools, agents, trigger sources, and manages trigger instances.

| Setting | Value |
|---------|-------|
| LLM | `anthropic/claude-opus-4.5` via OpenRouter |
| Temperature | 0.5 |
| Max tokens | 16000 |
| Context turns | 5 |
| Required env | `OPENROUTER_API_KEY` |
| Internal tools | 7 self-modify tools (see below) |

---

## Sub-Agent Internal Tools

### Browser Tools (9)

Used internally by BrowserAgent. Defined in `tools/browser.py`.

| Tool | Signature | Description |
|------|-----------|-------------|
| `browser_navigate` | `(url: str)` | Navigate to a URL. Falls back to requests+BeautifulSoup if Playwright unavailable. |
| `browser_click` | `(selector: str)` | Click element by CSS selector or `text=` selector. |
| `browser_type` | `(selector: str, text: str)` | Type text into an input field. |
| `browser_get_content` | `(include_links: bool = True)` | Get page text content and optionally links. |
| `browser_screenshot` | `()` | Take a screenshot. Returns a data URI preview string (first 100 chars of base64 + total length). |
| `browser_scroll` | `(direction: str = "down", amount: int = 500)` | Scroll page up or down by pixel amount. |
| `browser_press_key` | `(key: str)` | Press a keyboard key (e.g., `"Enter"`, `"Tab"`). |
| `browser_close` | `()` | Close the browser and reset the thread. |
| `browser_status` | `()` | Check Playwright availability and browser state. |

**Architecture:** All browser operations run on a dedicated `BrowserThread` to satisfy Playwright's single-thread requirement. Operations are queued and results retrieved via thread-safe queues. The browser persists between calls until explicitly closed.

**Fallback mode:** Set `BROWSER_FORCE_FALLBACK=true` in `.env` to skip Playwright entirely and use requests+BeautifulSoup for navigation and content extraction. Useful when Playwright hangs.

---

### Outlook Tools (13)

Used internally by OutlookAgent. Also available as **optional tools** for per-thread enabling. Defined in `tools/outlook_auth.py` (3 auth) and `tools/outlook_email.py` (10 email).

**Authentication tools:**

| Tool | Signature | Description |
|------|-----------|-------------|
| `outlook_auth_start` | `()` | Start Microsoft OAuth device code flow. Returns URL and code. |
| `outlook_auth_complete` | `()` | Complete auth after user signs in. Polls Microsoft (up to 5 min). |
| `outlook_list_authenticated_accounts` | `()` | List all authenticated Microsoft accounts with IDs. |

**Email tools:**

| Tool | Signature | Description |
|------|-----------|-------------|
| `outlook_list_emails` | `(account_id?, limit=10, folder="inbox", unread_only=False)` | List recent emails. Limit max 50. |
| `outlook_get_email` | `(email_id, account_id?)` | Get full email details including body. |
| `outlook_search_emails` | `(query, account_id?, limit=10)` | Search emails by keyword. Limit max 25. |
| `outlook_send_email` | `(to, subject, body, account_id?, cc?, bcc?, is_html=False)` | Send a new email. |
| `outlook_reply_email` | `(email_id, body, account_id?, reply_all=False)` | Reply to an email. |
| `outlook_create_draft` | `(to, subject, body, account_id?, cc?)` | Create a draft without sending. |
| `outlook_delete_email` | `(email_id, account_id?, permanent=False)` | Move to trash or permanently delete. |
| `outlook_mark_email` | `(email_id, is_read, account_id?)` | Mark email as read or unread. |
| `outlook_move_email` | `(email_id, folder, account_id?)` | Move email to a folder. |
| `outlook_forward_email` | `(email_id, to, comment?, account_id?)` | Forward an email. |

**Folder names** (case-insensitive):
- `outlook_list_emails` accepts: `inbox`, `sent`/`sentitems`, `drafts`, `deleted`/`deleteditems`, `junk`/`junkemail`, `archive`
- `outlook_move_email` additionally accepts: `trash` (→ deleteditems), `spam` (→ junkemail)

---

### Calendar Tools (12)

Used internally by CalendarAgent. Wraps the Google Calendar MCP server via JSON-RPC over stdio. Defined in `agents/calendar_agent_tools.py`.

| Tool | Signature | Description |
|------|-----------|-------------|
| `calendar_list_calendars` | `()` | List all available Google calendars. |
| `calendar_list_events` | `(calendar_id="primary", max_results=10, time_min?, time_max?)` | List events from a calendar. |
| `calendar_get_event` | `(event_id, calendar_id="primary")` | Get full event details. |
| `calendar_search_events` | `(query, calendar_id="primary", max_results=10)` | Search events by text. |
| `calendar_create_event` | `(summary, start_time, end_time, calendar_id="primary", description?, location?, attendees?, timezone?)` | Create a new event. |
| `calendar_update_event` | `(event_id, calendar_id="primary", summary?, start_time?, end_time?, description?, location?)` | Update an existing event. |
| `calendar_delete_event` | `(event_id, calendar_id="primary")` | Delete a calendar event. |
| `calendar_respond_to_event` | `(event_id, response, calendar_id="primary")` | Respond to invitation: `"accepted"`, `"declined"`, `"tentative"`. |
| `calendar_get_freebusy` | `(time_min, time_max, calendars?)` | Get free/busy info. `calendars` is comma-separated IDs. |
| `calendar_get_current_time` | `()` | Get current time for relative scheduling. |
| `calendar_list_colors` | `()` | List available event colors. |
| `calendar_manage_accounts` | `(action)` | Manage Google accounts: `"list"`, `"add"`, `"remove"`. |

**Requires:** `GOOGLE_OAUTH_CREDENTIALS` env var pointing to the OAuth credentials file. Node.js and `npx` must be installed.

---

### SelfModify Tools (7)

Used internally by SelfModifyAgent. Defined in `core/self_agent.py`. **Read** and **list** operations work on any path within the project root. **Write** and **delete** are restricted to `nymeria/tools/`, `nymeria/agents/`, and `nymeria/triggers/sources/`.

| Tool | Signature | Description |
|------|-----------|-------------|
| `self_file_read` | `(file_path: str)` | Read a file from the Nymeria codebase. |
| `self_file_write` | `(file_path: str, content: str)` | Write content to tools/, agents/, or triggers/sources/. Auto-backups. |
| `self_file_list` | `(directory: str = "nymeria/tools")` | List files in a directory. |
| `self_file_delete` | `(file_path: str)` | Delete a file from tools/, agents/, or triggers/sources/. |
| `self_test_import` | `()` | Test that all tools can be imported successfully. |
| `self_reload` | `()` | Reload all tools and agents after making changes. |
| `self_invoke_tool` | `(tool_name: str, arguments_json: str)` | Test a tool by invoking it with JSON arguments. |

**Workflow:** Write code → `self_test_import()` → `self_reload()` → `self_invoke_tool()` → report results.

---

## Optional Tools System

Optional tools are NOT loaded by default. They're available for per-thread enabling via the thread config UI.

**Currently available:** 13 Outlook tools (3 auth + 10 email) — the same tools used internally by OutlookAgent.

**How it works:**
1. `OPTIONAL_TOOLS` in `tools/__init__.py` maps tool names to tool objects: `{t.name: t for t in OUTLOOK_TOOLS}`
2. Per-thread config has an `enabled_tools` list (tool names)
3. During `_build_graph_with_prompt()`, enabled optional tools are added to the thread's tool set
4. Users enable/disable optional tools via the desktop UI thread settings or `PATCH /threads/{id}/config`

---

## Custom Tools

Custom tools extend Nymeria's capabilities without writing Python. Created via the **Desktop UI** (Settings → Tools) or the **REST API**.

### Tool Types

| Type | Description | Use Case |
|------|-------------|----------|
| **HTTP** | Makes REST API calls to external services | Integrate with APIs, webhooks, web services |
| **MCP** | Connects to Model Context Protocol servers | Use existing MCP tools, complex integrations |

### HTTP Tools

HTTP tools make REST API calls with configurable:
- **Method**: GET, POST, PUT, DELETE, PATCH
- **URL**: Supports `${param}` interpolation for dynamic URLs
- **Headers**: Including `${env:VAR_NAME}` for secrets from environment
- **Body Template**: JSON template with parameter placeholders
- **Response Path**: JSONPath to extract specific data from response

**Example: Weather API Tool**
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
    "response_path": "$.data.temperature"
  }
}
```

### MCP Tools

MCP tools connect to external MCP servers via JSON-RPC over stdio.

**Configuration:**
- **Server Command**: Command to start the MCP server (e.g., `npx`, `python`)
- **Server Args**: Command-line arguments
- **Tool Name**: The specific tool exposed by the MCP server
- **Environment Variables**: Variables to pass to the server
- **Idle Timeout**: Server shutdown after inactivity (default: 5 minutes)

**Example: Filesystem MCP Tool**
```json
{
  "id": "read_file_mcp",
  "name": "Read File (MCP)",
  "description": "Read a file using the MCP filesystem server",
  "implementation_type": "mcp",
  "parameters": {
    "path": {
      "type": "string",
      "description": "File path to read",
      "required": true
    }
  },
  "mcp_config": {
    "server_command": "npx",
    "server_args": ["-y", "@anthropic/mcp-server-filesystem", "/allowed/path"],
    "tool_name": "read_file",
    "idle_timeout_seconds": 300
  }
}
```

**MCP Server Lifecycle:**
- Servers start on-demand when the tool is first called
- Servers stay alive for the configured idle timeout
- Multiple tools can share the same MCP server
- Servers are gracefully shutdown when Nymeria stops

### Managing Custom Tools

**Via Desktop UI:**
1. Open Settings → Tools tab
2. Click "+ New Tool" to create
3. Fill in the form (HTTP or MCP configuration)
4. Test the tool with sample parameters
5. Enable/disable tools as needed

**Via REST API:**
```bash
GET /tools/custom              # List all custom tools
POST /tools/custom             # Create a new tool
PUT /tools/custom/{tool_id}    # Update a tool
DELETE /tools/custom/{tool_id} # Delete a tool
POST /tools/custom/{tool_id}/test  # Test a tool
```

**Storage:** Custom tools are stored as JSON files in `data/custom_tools/`, one `.json` per tool ID.

---

## Scheduled TODO Execution

Nymeria operates autonomously 24/7 through **scheduled TODOs** — TODOs with a `scheduled_for` datetime that are automatically executed when due.

### How It Works

1. `todo(task=..., scheduled_for=...)` creates a TODO and registers it in `TodoScheduleDB` (SQLite at `data/todo_schedule.db`).
2. The **Ticker** daemon polls every 5 seconds for due TODOs.
3. When a TODO is due, the ticker sends its `task` text as a prompt to the agent on the TODO's `thread_id`.
4. For recurring TODOs, **any completion** (ticker execution, agent marking done, API, or MCP) auto-reschedules to the next `scheduled_for` based on the recurrence pattern. Use `clear_recurrence` or `todo_delete` to stop.
5. Non-recurring completed TODOs are auto-archived after 7 days (hourly cleanup in the ticker).

**Durable scheduling:** Scheduled TODOs survive application restarts. Missed TODOs are recovered and executed on startup.

**Rate limiting:** Default 50 autonomous executions per hour per user (`MAX_SELF_INVOKES_PER_HOUR`). Uses a sliding window algorithm in `rate_limiter.py`.

---

## Security Levels & Metadata

Tool metadata is defined in `tools/metadata.py`. Each tool has a category, security level, and default enabled state.

### Security Levels

| Level | Default Enabled | Description |
|-------|----------------|-------------|
| **SAFE** | Yes | Always available, no risk |
| **MODERATE** | Yes | Can be disabled by user |
| **SENSITIVE** | **No** | Requires explicit opt-in |

### Tools by Security Level

**SAFE:** `file_read`, `web_search`, `consult`, `memory_save`, `memory_forget`, `memory_list`, `personality_set`, `rag_search`, `todo`, `todo_delete`, `todo_list`, `clear_agent_context`

**MODERATE:** `bash_execute`, `file_write`, `claude_code`, `notify`, `reload_all`

**SENSITIVE:** `self_modify_rollback`

**MODERATE (optional):** `trigger_create`, `trigger_update`, `trigger_delete` — in `OPTIONAL_TOOLS`, not loaded by default

**SAFE (optional):** `trigger_list` — in `OPTIONAL_TOOLS`, not loaded by default

---

## Adding New Tools

### 1. Create a Tool File

```python
# nymeria/tools/my_tool.py
from langchain_core.tools import tool

@tool
def my_tool(param: str) -> str:
    """
    Description shown to the LLM. Be specific about when to use this tool.

    Args:
        param: What this parameter does

    Returns:
        What the tool returns
    """
    # Implementation
    return f"[Success]: Result is {result}"
```

### 2. Export in `__init__.py`

```python
# nymeria/tools/__init__.py
from .my_tool import my_tool

ALL_TOOLS = [
    # ... existing tools
    my_tool,
]
```

### 3. Tool is Automatically Available

The tool is available on next startup, or call `reload_all()` for hot-reload.

---

## Tool Design Guidelines

| Guideline | Example |
|-----------|---------|
| **Clear docstrings** | The LLM uses these to decide when to call the tool |
| **Return strings** | All outputs should be serializable strings |
| **Handle errors gracefully** | Return `"[Error]: ..."`, don't raise exceptions |
| **Prefix results** | Use `[Success]`, `[Error]`, `[Info]` prefixes |
| **Be specific** | One tool = one job |
| **Log operations** | Use `logger.info()` for audit trail |
| **Use InjectedToolArg** | For user/thread context: `config: Annotated[RunnableConfig, InjectedToolArg]` |
