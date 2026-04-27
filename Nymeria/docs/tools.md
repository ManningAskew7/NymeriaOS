# Nymeria Tools Reference

Nymeria has a three-tier tool system: **core tools** always loaded, **dynamic callable thread tools** (one per callable thread), and a large set of **optional tools** available for per-thread enabling. Treat the counts below as approximate only when noted, because the optional surface evolves over time.

## Summary Table

### Core Tools

| # | Tool | Category | Security | Default | Description |
|---|------|----------|----------|---------|-------------|
| 1 | `bash_execute` | Core | MODERATE | On | Execute shell commands |
| 2 | `file_read` | Core | SAFE | On | Read file contents |
| 3 | `file_write` | Core | MODERATE | On | Write content to files |
| 4 | `web_search` | Core | SAFE | On | Search the web via Perplexity |
| 5 | `consult` | Core | SAFE | On | Ask Gemini for a second opinion (OpenRouter) |
| 6 | `profile_save` | Profile | SAFE | On | Save a user memory |
| 7 | `profile_forget` | Profile | SAFE | On | Remove a memory by key |
| 8 | `profile_list` | Profile | SAFE | On | List all saved memories and preferences |
| 9 | `personality_set` | Profile | SAFE | On | Set communication preferences |
| 10 | `rag_search` | Profile | SAFE | On | Semantic search over past conversations |
| 11 | `nym_todo` | TODO | SAFE | On | Create or update a TODO — scheduled TODOs auto-wake the agent |
| 12 | `nym_todo_delete` | TODO | SAFE | On | Delete a TODO permanently |
| 13 | `nym_todo_list` | TODO | SAFE | On | List TODO items |
| 14 | `notepad_write` | Notepad | SAFE | On | Write to thread's persistent notepad |
| 15 | `notepad_read` | Notepad | SAFE | On | Read thread's notepad content |
| 16 | `notepad_edit` | Notepad | SAFE | On | Find-and-replace edit in thread's notepad |
| 17 | `notepad_clear` | Notepad | SAFE | On | Clear thread's notepad |
| 18 | `notify` | Core | MODERATE | On | Send notifications (Telegram/Discord/Slack) |
| 19 | `tool_search` | Core | SAFE | On | Search, enable, and disable tools for the current thread |
| 20 | `list_installed_skills` | Skills | SAFE | On | List Agent Skills installed on disk (all scopes) |
| 21 | `search_skills` | Skills | SAFE | On | Semantic search over installed skills or the Anthropic marketplace (OpenAI embeddings → BM25 → substring fallback) |
| 22 | `install_skill` | Skills | MODERATE | On | Install a skill from `anthropics/skills` into user or global scope |
| 23 | `mcp_search` | MCP | SAFE | On | Search public MCP server registries (official + Smithery) for installable servers |
| 24 | `mcp_install` | MCP | MODERATE | On | Install an MCP server from a paste (Claude Desktop JSON, stdio command, HTTP URL, or registry id) |

> **Skill meta-tool:** A single `Skill(name)` tool is synthesized per-thread at graph-build time when any skills are active — it's not in `ALL_TOOLS`. Its description carries an `<available_skills>` index of `(name, description)` pairs; calling it returns that skill's full SKILL.md body. See `docs/skills.md`.

> **Note:** `claude_code`, `reload_all`, and `self_modify_rollback` are **not** in core `ALL_TOOLS`. They live in `OPTIONAL_TOOLS` (`reload_all` / `self_modify_rollback` via `SUBAGENT_TOOLS`, `claude_code` directly) and are in `ADMIN_ONLY_OPTIONAL_TOOL_NAMES` — admins can enable them per-thread, non-admins are blocked at every enable boundary. See `nymeria/tools/__init__.py` for the canonical lists.

### Optional: Trigger Tools (6)

Not loaded by default. Enable per-thread via thread config, or use through SelfModifyAgent.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `trigger_create` | Trigger | MODERATE | Create an event-driven trigger |
| 2 | `trigger_list` | Trigger | SAFE | List triggers |
| 3 | `trigger_update` | Trigger | MODERATE | Update a trigger |
| 4 | `trigger_delete` | Trigger | MODERATE | Delete a trigger |
| 5 | `trigger_inspect` | Trigger | SAFE | Inspect a trigger — view detail, dry-run test, or execution history |
| 6 | `trigger_sources_info` | Trigger | SAFE | List available trigger sources with config schemas and template variables |

### Optional: Slash Command Tool (1)

Not loaded by default. Enable per-thread to let the agent invoke the same user-facing slash commands that the Discord/Telegram bots expose.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `slash_command` | Self | MODERATE | Run a Nymeria slash command on the current thread (config, env, tools, memory, TODOs, notepad, status). Destructive commands blocked. |

### Optional: _PRV_A Tools (8 _PRV_A-specific + 1)

Google Sheets-based tools for Acme Hardware RFQ processing. All backed by `google_sheets.py` with 5-minute in-memory caching (auto-invalidated after writes). The `outlook_get_attachments` tool (last in the table) is from `OUTLOOK_ATTACHMENT_TOOLS`, not a _PRV_A module — it's placed here as a general-purpose extraction utility.

| # | Tool | Security | Description |
|---|------|----------|-------------|
| 1 | `google_sheets_search` | SAFE | Generic search for any Google Sheet by ID |
| 2 | `google_sheets_append` | MODERATE | Append rows to a Google Sheet (for RFQ tracking) |
| 3 | `google_sheets_update` | MODERATE | Find and update existing rows by search value |
| 4 | `_prv_a_supplier_lookup` | SAFE | Find overseas suppliers by brand(s) from Y/N matrix. Supports multi-brand lookup with coverage indicators. Includes emails, websites, and inline vendor quality ratings. |
| 5 | `_prv_a_vendor_info` | SAFE | Vendor quality ratings, contacts, and notes from past dealings |
| 6 | `_prv_a_product_search` | SAFE | Search the master _PRV_A product catalog. Supports batch part numbers. |
| 7 | `_prv_a_acme_lifecycle` | SAFE | Acme part lifecycle status (Active/Mature/Discontinued/Obsolete) with migration paths. Supports batch part numbers. |
| 8 | `_prv_a_acme_pricelist` | SAFE | Acme Electric part details and list pricing (ex-GST). Supports batch part numbers. |
| 9 | `outlook_get_attachments` | SAFE | Extract text from email attachments (PDF/DOCX via Gemini, Excel via openpyxl, CSV/TXT direct) |

See `docs/_prv_a/setup-guide.md` for full setup instructions, Google Sheet IDs, and configuration.

### Optional: Watchdog Tools (4)

Not loaded by default. Enable per-thread for the Smart Watchdog scheduler.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `activity_feed` | Watchdog | SAFE | Structured activity summary across all threads (user messages, tasks, TODOs, notifications) |
| 2 | `watchdog_dispatch` | Watchdog | MODERATE | Create a TODO on a different thread (cannot self-target) |
| 3 | `watchdog_read_notepad` | Watchdog | SAFE | Read another thread's notepad for state awareness |
| 4 | `watchdog_todo_overview` | Watchdog | SAFE | List all active TODOs across all threads, grouped by thread |

### Optional: Thread Spawning (1)

Not loaded by default. Enable per-thread to let the agent create new conversation threads with scoped config.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `spawn_thread` | Subagent | MODERATE | Create or delete a sidebar thread with custom instructions, tool selection, and LLM overrides. New threads are callable (globally invocable) by default. Create mode optionally dispatches an initial message and blocks until the child responds; delete mode cleans up a previously-spawned thread. |

### Callable Thread Tools (Dynamic)

Any thread with `callable=True` in its thread config becomes a tool that other threads can invoke. There are no hardcoded agents — callable threads are fully configurable via the UI:

- **Model**: Set per-thread via `llm_config.model` in thread settings (inherits global default if not set)
- **Tools**: Enable/disable any optional tools per-thread
- **System prompt**: Custom `system_prompt` or `instructions` per-thread
- **Name**: The tool name equals the thread's sidebar title (synced via `callable_name` in thread config)

Create a callable thread: open thread settings → check "Make Callable" → set a name and description. The thread becomes available as a tool to **the creator's own threads** after `sync_agent_tools()` runs — callables are scoped to their owner (the user who created them) and the `_thread_owners` table determines visibility. Two users can independently create callables with the same `callable_name`; each user's graph binds their own version, and the runtime ownership gate in `agents/tool_factory.py` blocks cross-user invocation.

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
file_write(file_path: str, content: str, encoding: str = "utf-8", create_directories: bool = True, append: bool = False, attach: bool = False)
```

**Parameters:**
- `file_path` (`str`): Absolute or relative path to the file
- `content` (`str`): Content to write
- `encoding` (`str`, default `"utf-8"`): File encoding
- `create_directories` (`bool`, default `True`): Create parent directories if they don't exist
- `append` (`bool`, default `False`): Append to file instead of overwriting
- `attach` (`bool`, default `False`): Deliver the written file back to chat clients (Telegram, Discord, desktop artifact viewer). Only files inside `NYMERIA_WORKSPACE_DIR` (default `/workspace`) are attachable. When attach succeeds, the raw tool result includes an `[attach:/path]` tag for backward compatibility and the API emits a structured `workspace_artifact` SSE event.

**Returns:** Success/error message with character count. When `attach=True` and the file is inside the workspace directory, the raw tool result includes an `[attach:/path]` tag and clients receive a `workspace_artifact` event. If the file is outside the workspace, the write still succeeds but attachment delivery is skipped with an info note.

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

### claude_code (Optional, admin-only)

Invoke Claude Code in headless mode to create, modify, or analyze code. **Not loaded by default** — lives in `OPTIONAL_TOOLS` and is gated by `ADMIN_ONLY_OPTIONAL_TOOL_NAMES` (only admins may enable it).

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

## Profile Tools

Profile memories are **automatically injected** into Nymeria's system prompt. The `profile_list` tool provides an explicit way for models to retrieve exact keys before updating or deleting.

> **Implementation note:** `profile_save`, `profile_forget`, `profile_list`, `personality_set`, and `rag_search` all accept a `config: Annotated[RunnableConfig, InjectedToolArg]` parameter that is automatically injected by LangGraph. The LLM never passes this parameter.

### profile_save

Save a memory about the user to persistent storage.

```python
profile_save(key: str, value: str)
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

### profile_forget

Remove a memory or personality preference by key.

```python
profile_forget(key: str)
```

**Parameters:**
- `key` (`str`): Memory key or personality trait to delete

**Returns:** Confirmation, or error with list of available keys (memories + personality traits) if not found.

**Behavior:** Tries memories first, then personality preferences. Use `profile_list` to see exact keys before calling.

---

### profile_list

List all saved memories and personality preferences for the current user.

```python
profile_list()
```

**Returns:** Formatted list of all memories (key: value) and personality preferences (trait: value), or a message indicating no memories are stored.

**Notes:**
- Use this before `profile_forget` to get exact key names.
- Memories are also auto-injected into the system prompt, but weaker models may struggle to extract exact keys from long prompts.

---

### ~~memory_clear_all~~ (removed)

Removed — a cheap model could hallucinate this call and wipe all user memories irreversibly. Delete memories individually with `profile_forget` instead.

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

**Indexing is automatic.** As of 2026-04, `opt_in.rag_enabled` defaults to `True` for new profiles, and existing profiles are migrated to `True` on first load (one-time, watermarked by `opt_in.rag_migrated`). Conversation turns are indexed in four places, in this order of frequency:

1. **Per turn** — `_index_conversation_turn` runs after every chat turn (`core/agent.py`).
2. **Pre-compact** — manual `/compact`, async auto-compact, and sync auto-compact all flush via `_pre_trim_memory_flush` before clearing messages.
3. **Pre-clear** — `POST /threads/{id}/clear` flushes before deleting checkpoints.
4. **Delete cleanup** — `DELETE /threads/{id}` runs the full thread cascade, including `MemoryIndex.delete_by_thread`, so `rag_search` doesn't surface chunks from deleted threads and thread-bound TODOs/triggers cannot wake the deleted thread again.

To opt out, call `rag_settings(enabled=False)`. The migration watermark prevents re-flipping on subsequent loads.

---

## Notepad Tools

Per-thread persistent notes that survive context compaction. Unlike profile memories (which are global and injected into the system prompt), notepad content is thread-specific and only re-injected after compaction events.

Storage: `data/thread_notes/{thread_id}.md`

> **Implementation note:** All notepad tools accept a `config: Annotated[RunnableConfig, InjectedToolArg]` parameter automatically injected by LangGraph. The `thread_id` is extracted from this config.

### notepad_write

Write to the thread's persistent notepad.

```python
notepad_write(content: str, mode: str = "append")
```

**Parameters:**
- `content` (`str`): Text to write
- `mode` (`str`): `"append"` (default) adds with `\n\n` separator, `"replace"` overwrites entirely

**Returns:** Confirmation with byte count.

**Limits:** 50KB max per notepad.

---

### notepad_read

Read the thread's notepad content.

```python
notepad_read()
```

**Returns:** Current notepad content, or `"[empty]"` if nothing saved.

---

### notepad_clear

Clear the thread's notepad entirely.

```python
notepad_clear()
```

**Returns:** Confirmation message.

---

## TODO Tools

TODOs are the **primary driver for autonomous operation**. Active TODOs are automatically injected into the system prompt. Every TODO must have a `scheduled_for` time — TODOs are for the agent's autonomous work queue, not a general task list.

> **Implementation note:** `todo`, `todo_delete`, and `todo_list` all accept an injected `config` parameter for user/thread identification. The LLM never passes this.
>
> **Migration note:** `todo_add` and `todo_update` were merged into the single `todo` tool. Priority, deadline, blocked status, and the permanent flag have been removed.

### nym_todo

Create or update a TODO item. Omit `todo_id` to create; provide it to update. Scheduled TODOs auto-wake the agent to execute them.

```python
nym_todo(todo_id: Optional[str] = None, task: Optional[str] = None,
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

**Recurring TODOs:** Recurring TODOs **auto-reschedule when marked done** — regardless of whether the ticker executed them or the agent/user marked them done manually. The next `scheduled_for` is calculated from the `recurrence` pattern and the status resets to `pending`. This applies to all completion paths: the `nym_todo` tool, the REST API, and the MCP server. To permanently stop a recurring TODO, use `nym_todo(todo_id=..., clear_recurrence=True)` or `nym_todo_delete`.

**Auto-purge:** Non-recurring completed TODOs are automatically archived after 7 days by the ticker daemon.

---

### nym_todo_delete

Delete a TODO permanently. No archive — immediately removed. Cancels any scheduled execution.

```python
nym_todo_delete(todo_id: str)
```

**Parameters:**
- `todo_id` (`str`): The 8-character TODO ID

**Returns:** Confirmation with deleted task text, or error.

---

### nym_todo_list

List TODO items. Shows active (non-done) by default.

```python
nym_todo_list(filter_status: Optional[str] = None)
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

### tool_search

Search, enable, and disable tools for the current thread. Allows the agent to discover tools it doesn't currently have loaded and activate them.

```python
tool_search(action: str, query: str = "", category: str = "", tools: list[str] = None, ttl: str = "2h", force: bool = False)
```

**Actions:**
- `search` — Search tools by keyword and/or category. Returns up to 15 results with name, description, category, security level, and enabled status (including TTL remaining).
- `enable` — Enable tools by name (`tools` param) or by category (`category` param). In `astream()` (REST/SSE) and `chat()` (MCP/CLI sync path), this triggers an in-turn graph rebuild so the tools are callable in the very next step of the same user message.
- `disable` — Disable tools for the thread (`tools` param). Takes effect on the next agent step. Refuses core tools (`bash_execute`, `file_read`, etc.) unless `force=True`. Mixed batches partially succeed: non-core names are disabled, core names are listed under `[Refused]` with a hint to retry that subset with `force=True`. Disable is non-destructive — it only appends to `disabled_tools`; entries in `enabled_tools` / `temporary_tools` are preserved, so a subsequent `enable` restores the tool's original permanent/TTL state. "Core" here is the hardcoded `ALL_TOOLS` set, which is a **superset** of what the `already_default` classifier bucket calls default-bound (user profile's `default_thread_tools` curates a subset of `ALL_TOOLS`). A tool like `notepad_read` is in both, so it needs `force=True` to disable; but a tool in `ALL_TOOLS` that's absent from `default_thread_tools` is still core-protected even though it isn't default-bound.
- `list_categories` — List all tool categories with tool counts.
- `status` — Show currently enabled/disabled tools for this thread, with TTL remaining per entry.

**Parameters:**
- `action` (`str`): One of: `search`, `enable`, `disable`, `list_categories`, `status`
- `query` (`str`): Keyword to search tool names and descriptions (for `search`)
- `category` (`str`): Category name to filter search or enable all tools in (e.g. `"email"`, `"twitch"`)
- `tools` (`list[str]`): Specific tool names to enable or disable
- `ttl` (`str`): For `enable` only — how long to keep the tools bound before lazy eviction. One of: `"30m"`, `"2h"` (default), `"6h"`, `"24h"`, `"permanent"`. Ignored for other actions.
- `force` (`bool`): For `disable` only — set `True` to allow disabling core tools. Default `False`. Non-core tools are unaffected by this flag.

**Enable response buckets:** every input tool is classified in exactly one bucket, checked in this priority order — (1) `Un-disabled` (was in `disabled_tools`, now removed; if the tool has a preserved `enabled_tools` or `temporary_tools` entry, it is restored AS-IS — the requested `ttl` does NOT apply, so a batch-level TTL can't silently promote/demote an unrelated tool; a fresh entry is only written when there is no preserved state and no default binding), (2) `Already permanent` (in `tc.enabled_tools`; TTL requests are rejected, no demotion), (3) `Already bound (default set)` (in the thread's default-bound set — `ALL_TOOLS` or the user-profile-level `default_thread_tools` override; already callable, no write), (4) `TTL refreshed` (in `tc.temporary_tools`; `expires_at` pushed out), (5) `Promoted to permanent` (in `tc.temporary_tools`, `ttl="permanent"` → moved to `tc.enabled_tools`), (6) `Newly loaded` (none of the above; written fresh to `enabled_tools` or `temporary_tools` depending on `ttl`).

The classifier sources its default-bound set from the same place as graph-build (`agent._build_graph_with_prompt`: `profile.tool_preferences.default_thread_tools` if set, else `{t.name for t in ALL_TOOLS}`). Tools that live in `ALL_TOOLS` but are excluded from the user's `default_thread_tools` list are correctly treated as optional (priority-6 newly-loaded) rather than already-bound. Note: the bucket is called `Already bound (default set)` — not "core" — to avoid conflating it with the `disable` guard's "core" protection, which uses the broader `ALL_TOOLS` list.

**`disabled_tools` is authoritative in graph-build.** The graph-build pipeline is: start with the default-bound set, filter out `disabled_tools`, then add extras from `enabled_tools ∪ live_temporary_tools` — BUT extras are also filtered by `disabled_tools` before merging. So a tool listed in both `enabled_tools` and `disabled_tools` is unbound (disable wins). This lets `disable` be non-destructive: it only appends to `disabled_tools` and leaves `enabled_tools` / `temporary_tools` alone. An `enable` on that same tool just removes it from `disabled_tools`; the preserved permanent/TTL entry comes back automatically. Without this rule, `disable` would have to destructively mutate `enabled_tools` to actually disable an overlapping tool, and a disable→enable round-trip would silently strip the permanent badge.

**Status display filters disabled tools from the enabled sections.** Because `disabled_tools` is authoritative, a tool that has a preserved `enabled_tools` or `temporary_tools` entry while ALSO being in `disabled_tools` is currently unbound. The `status` and `search` renderers suppress such tools from the `Enabled (permanent)` / `Enabled (TTL)` sections and annotate them in the `Disabled` section with `(preserved: permanent)` or `(preserved: Xm left)`, so the user can still see what will round-trip back on un-disable without seeing the same tool in two places.

#### In-turn auto-continue

When the agent calls `tool_search(action="enable", tools=[...])` during a turn, the harness:

1. Persists the enablement to the thread config (with TTL) and invalidates the cached graph.
2. Finishes the current graph invocation normally.
3. Emits a `tool_reload` SSE event (`{type: "tool_reload", tools, ttl, ttl_seconds}`).
4. Builds a fresh graph with the new tools bound to the LLM.
5. Injects an internal resume message (`internal_type="tool_reload_resume"`) and drives the new graph against it, streaming into the same SSE connection.

To the client this looks like one continuous turn: no extra `done` event, no separate user message. The thread lock stays held the whole time. The loop is capped at `AgentCore.MAX_TOOL_RELOADS_PER_TURN` (default `3`) rebuilds per user turn to bound token usage. Once the cap is hit, `tool_search(action="enable")` detects it, stops returning `Command(goto=END)`, and instead returns a plain string whose body includes a `[Reload cap hit]` notice — the agent can still respond in-turn, and the new binding takes effect on the next user message. This prevents an orphaned `tool_result` with no LLM follow-up (symptom: the stream looks like it froze because the last enable's `Command` ended the graph but the reload loop was already exhausted).

Both `astream()` (REST/SSE) and `chat()` (MCP/CLI sync path) honor the auto-continue. The legacy sync `stream()` path used by callable thread execution persists the enablement for the next turn but does not auto-continue; it clears any unconsumed pending reload when the stream exits so a stale Tool Binding event cannot attach to a later unrelated turn.

#### TTL and eviction

Each enablement (other than `ttl="permanent"`) gets an `expires_at` timestamp stored in `ThreadConfig.temporary_tools`. At the start of every new turn, `_build_graph_with_prompt` calls `_resolve_temporary_tools(tc)` which:

1. Drops entries whose `expires_at` has passed.
2. Persists the cleaned config back to disk.
3. Returns the still-live set for inclusion in the tool list.

Eviction never happens mid-invocation, so a tool that was bound at the start of a graph run is callable for the whole run — there are no surprise eviction errors. Calling `enable` on a tool already in `temporary_tools` refreshes `expires_at`; calling `enable` with `ttl="permanent"` promotes the entry into `enabled_tools` (which has no expiry and is also what the UI/API writes to). Calling `disable` removes from both buckets immediately (next-message effect).

Pick the shortest TTL that covers your task. `2h` is a sensible default for multi-step tasks; `30m` for one-shots; `6h`/`24h` for sustained workflows; `permanent` only if the tool should remain as a standing capability on the thread.

---

## Agent Management / Self-Modify Tools

### reload_all

Reload all tools, agents, and trigger sources. Call after SelfModifyAgent creates or modifies code, or after manual file edits.

```python
reload_all()
```

**Returns:** Count of reloaded tools and trigger sources.

**Availability:** Present in `SUBAGENT_TOOLS` / optional tooling, not in the always-loaded core `ALL_TOOLS` list.

**Important:** Due to how LangGraph works, newly created tools are not available in the same conversation turn. They work on the next user message.

---

### self_modify_rollback

Rollback a file to its previous version from a SelfModifyAgent backup.

```python
self_modify_rollback(file_path: str)
```

**Parameters:**
- `file_path` (`str`): Path to file to rollback (e.g., `"nymeria/tools/my_tool.py"`)

**Returns:** Success or error message.

**Security:** **SENSITIVE** — disabled by default. Requires explicit opt-in via user tool preferences or per-thread config.

**Availability:** This is not an always-loaded core tool. It is surfaced through self-modify or optional tool paths.

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

### trigger_inspect

Inspect a trigger in one of three modes: view full configuration, dry-run with sample data, or fetch execution history.

```python
trigger_inspect(trigger_id: str, action: str = "detail", limit: int = 10)
```

**Parameters:**
- `trigger_id` (`str`): The 8-char trigger ID to inspect
- `action` (`str`, default `"detail"`): One of:
  - `"detail"` — Full trigger configuration, health status, conditions, pending events, thread binding
  - `"test"` — Dry-run with sample event data; renders the action template and reports whether conditions would pass. Does **not** fire the trigger.
  - `"history"` — Recent execution history (status, duration, error messages)
- `limit` (`int`, default `10`, max `50`): Number of executions to return for `action="history"`

**Returns:** Formatted trigger details, test results, or execution history.

**Examples:**
```python
trigger_inspect("a1b2c3d4")                          # detail
trigger_inspect("a1b2c3d4", action="test")           # dry-run
trigger_inspect("a1b2c3d4", action="history", limit=5)
```

---

### trigger_sources_info

Get a formatted catalog of all available trigger sources with their config fields, template variables, and example configs. Useful for LLM-assisted trigger creation.

```python
trigger_sources_info()
```

**Parameters:** None.

**Returns:** Formatted text listing each source with:
- Name, description, category
- Config fields with types, defaults, and required flags
- Template variables available for action templates
- Example config

---

## Slash Command Tool (Optional)

Gives the agent a single dispatch tool that invokes the same user-facing slash commands exposed by the Discord and Telegram bots — so the agent can inspect and change its own backend (LLM model, tool set, memories, TODOs, env vars, notepad) without dedicated per-setting tools bloating the tool list.

> **Note:** Not loaded by default. Lives in `OPTIONAL_TOOLS` — enable per-thread via thread config UI or `PATCH /threads/{id}/config {"enabled_tools": ["slash_command"]}`.

### slash_command

Run a Nymeria slash command on the agent's own thread.

```python
slash_command(command: str)
```

**Parameters:**
- `command` (`str`): The slash command string (with or without a leading `/`). Values with spaces may be quoted.

**How to use:** Tell the agent to call `/help` first. The help output is the source of truth for syntax — the tool's own description only lists a handful of examples to keep the tool schema small.

**Example commands:**
- `/help` — list every supported command
- `/status` — model, context, tools, tasks summary
- `/config set llm_model claude-opus-4-6` — change global model
- `/env get PERPLEXITY_API_KEY` — fetch unmasked secret
- `/memory save color "deep blue"` — save a user memory
- `/tools enable browser` — turn on a category on this thread
- `/todos add Check logs | 2h | daily` — scheduled repeating TODO
- `/notepad write replace:new notepad contents` — overwrite the thread notepad

**Blocked commands:** `/ask`, `/stop`, `/clear`, `/compact`, `/restart`, `/start` — these would interrupt or destroy the current conversation and are rejected before any API call.

**Returns:** Plain-text result prefixed with `[Success]`, `[Error]`, or `[Info]`.

**Runtime behavior:** Works in both normal conversation turns and autonomous scheduled TODO runs. Nymeria's ticker uses the synchronous `agent.stream()` path, so `slash_command` provides both sync and async invocation modes even though the underlying dispatcher talks to the local API asynchronously.

**Requirements:**
- `NYMERIA_API_URL` — defaults to `http://api:8000` inside Docker or `http://localhost:8000` outside.
- Authenticates with `NYMERIA_SERVICE_TOKEN` (admin service token) plus `X-Nymeria-Act-As: <caller_user_id>` so each invocation runs under the requesting user. The legacy shared `NYMERIA_API_KEY` was retired — see `docs/accounts.md`.

**Security note:** The tool runs in-process against the local API as the calling user (via act-as routing). `/env get` returns unmasked secrets and is admin-only at the API layer — non-admin callers will get 403 if they try to invoke admin-gated slash commands like `/env_get`, `/restart`, or `/config_*`.

**Implementation:** See `nymeria/tools/slash_command.py` (parser + denylist + tool entry point) and `nymeria/triggers/slash_dispatcher.py` (command → API-method routing and plain-text formatting). Mirrors the Telegram bot's command handlers but emits plain text instead of HTML.

---

## Watchdog Tools (Optional)

Tools for the Smart Watchdog — an intelligent scheduler thread that observes system activity and dispatches work to other threads. Not loaded by default; enable per-thread via thread config.

### activity_feed

Get a structured activity summary across all threads since a given time window.

```python
activity_feed(minutes_ago: int = 10)
```

**Parameters:**
- `minutes_ago` (`int`): Look-back window in minutes (default 10)

**Returns:** Structured text report grouped by thread showing user messages, autonomous tasks, TODO changes, and notifications. Returns "No activity" if the window is empty.

**Data source:** Reads from the persisted activity log (`data/activity/{user_id}.json`). Only as complete as what gets logged — user messages, TODO state changes, autonomous task execution, and notifications are all captured.

### watchdog_dispatch

Create a TODO on a different thread. Cannot target the calling thread.

```python
watchdog_dispatch(target_thread_id: str, task: str, scheduled_for: str = "now", notes: str = "")
```

**Parameters:**
- `target_thread_id` (`str`): Thread ID to dispatch the TODO to (must differ from caller)
- `task` (`str`): Clear, specific description of what the target thread should do
- `scheduled_for` (`str`): When to fire — `"now"`, `"30s"`, `"5m"`, `"1h"`, `"1d"`, or `"YYYY-MM-DD HH:MM"`
- `notes` (`str`): Supporting context for the target thread

**Returns:** Confirmation with the created TODO ID, or error if self-targeting or limit reached.

**Implementation:** `nymeria/tools/watchdog_dispatch.py`. Wraps `TodoManager.add()` with a cross-thread guard. Logs activity as `WATCHDOG_NUDGE`.

### watchdog_read_notepad

Read another thread's notepad to understand what it's currently focused on.

```python
watchdog_read_notepad(target_thread_id: str)
```

**Parameters:**
- `target_thread_id` (`str`): Thread ID whose notepad to read

**Returns:** Notepad content, or message indicating the notepad is empty.

### watchdog_todo_overview

List all active TODOs across all threads, grouped by thread. Shows thread assignment, schedule, and recurrence for each TODO.

```python
watchdog_todo_overview()
```

**Returns:** All active TODOs grouped by thread with status icons, schedule times, and recurrence info. Use this before dispatching to avoid creating duplicate TODOs.

**Implementation:** All watchdog tools live in `nymeria/tools/watchdog_dispatch.py`.

---

## Thread Spawning (Optional)

### spawn_thread

Create or delete a conversation thread with scoped configuration. Two modes via the `action` parameter: `"create"` (default) and `"delete"`. Spawned threads appear in the desktop sidebar inside a **"Spawned by Nymeria"** folder so they stay separate from user-created threads.

```python
spawn_thread(
    title: Optional[str] = None,
    instructions: Optional[str] = None,
    optional_tools: Optional[List[str]] = None,
    tool_categories: Optional[List[str]] = None,
    disabled_tools: Optional[List[str]] = None,
    make_callable: bool = True,
    llm_provider: Optional[str] = None,
    llm_model: Optional[str] = None,
    llm_temperature: Optional[float] = None,
    llm_max_tokens: Optional[int] = None,
    llm_extended_thinking: Optional[bool] = None,
    llm_reasoning_effort: Optional[str] = None,
    initial_message: Optional[str] = None,
    action: str = "create",
    delete_thread_id: Optional[str] = None,
)
```

**Create mode (`action="create"`, default):**

- `title` (required for create): User-visible thread title. Truncated to 80 chars.
- `instructions`: Extra system-prompt instructions **APPENDED** to `soul.md` (max 5000 chars). Cannot replace the base personality. Also used as the callable tool's description if provided.
- `optional_tools`: List of optional tool names to enable (e.g. `["sticky_note", "browser_navigate"]`). Core tools are inherited automatically — only list extras.
- `tool_categories`: List of categories (e.g. `["email", "browser"]`) to bulk-enable every optional tool in that category. Merged with `optional_tools`.
- `disabled_tools`: List of core tool names to EXCLUDE from the new thread.
- `make_callable` (default `True`): If `True`, the new thread is registered as a callable tool with an auto-derived name (`spawned_{slug}_{rand8}`) and ownership is **claimed for the spawning user** in `thread_owners`. Threads owned by that same user (including the parent) can invoke it; threads owned by any other user cannot — the runtime gate in `agents/tool_factory.py` rejects cross-user invocations. Set `False` for a single-use thread.
- `llm_*`: Optional LLM overrides. Omit to inherit global settings.
- `initial_message`: If provided, dispatches this message and **blocks** until the child responds. The child's response becomes part of this tool's output.

**Create returns:**
- Preamble with the new `thread_id` (`spawned-{slug}-{rand8}`).
- If `make_callable=True`: the generated callable tool name (e.g. `spawned_research_a3f21c9d`) the parent can invoke later.
- If `initial_message` provided: the child's response text appended.
- A reminder of the `action="delete"` call needed to remove the thread.

**Delete mode (`action="delete"`):**

- `delete_thread_id` (required for delete): The spawned thread's ID (must start with `"spawned-"`).
- Only the **calling thread** (the original spawn parent, tracked via `platform_meta.spawn_parent`) can delete a given spawned thread. If the stored spawn_parent is empty (e.g. an older spawn without lineage), any thread may delete it.
- Cleans up: metadata, config, checkpoints (SQLite or Postgres), notepad, and — if the thread was callable — unregisters the tool globally via `sync_agent_tools()`.
- Publishes a `thread_deleted` sync event so all connected clients remove it from their sidebars.

**Delete returns:** `[Deleted]: thread_id=spawned-...` on success.

**Safety limits (create only):**
- **Spawn depth**: capped at 3 by default (`NYMERIA_MAX_SPAWN_DEPTH` env). Depth stored in `platform_meta.spawn_depth`.
- **Rate limit**: 10 spawns per parent per hour (`NYMERIA_MAX_SPAWNS_PER_HOUR` env).
- **Abort cascade**: parent→child invocation is registered so stopping the parent stops its children.

**Frontend behavior:** When the `thread_created` sync event arrives with a `spawned-` thread_id, the desktop client lazily creates a "Spawned by Nymeria" folder and files the thread there. `thread_deleted` events trigger removal from the sidebar (and the folder).

**System prompt rule:** The tool only exposes the *append* path (`instructions`). It cannot set `system_prompt` (which would replace `soul.md` entirely).

**Implementation:** `nymeria/tools/spawn_thread.py`. Mirrors `thread_agent_executor.invoke()` for live event streaming (child activity appears in the autonomous pane). Delete path mirrors `DELETE /threads/{id}` from `triggers/api.py`.

**Non-blocking (deferred):** Fire-and-forget spawning is not currently supported. Would need cross-thread TODO creation (extending `nym_todo` to target other threads) or a thread mailbox channel. Today's blocking-only mode covers the common "delegate a task, get the answer" use case.

---

## Callable Thread Tools (Dynamic)

Any thread with `callable=True` in its thread config becomes a callable tool — there are no hardcoded agent names or fixed configurations. Each callable thread is fully configurable via the UI:

- **Name**: The tool name equals the thread's sidebar title (synced via `callable_name` in thread config)
- **Model**: Set per-thread via `llm_config.model` (inherits global default if not set)
- **Tools**: Enable/disable any optional tools per-thread
- **System prompt**: Custom `system_prompt` or `instructions` per-thread

Create a callable thread: open thread settings → check "Make Callable" → set a name and description. The callable becomes available as a tool to **threads owned by the same user** after `sync_agent_tools()` runs. Other users do not see the callable in their tool list, and the runtime ownership gate rejects any invocation attempt by a non-owner. Admins can act-as the owning user via `X-Nymeria-Act-As` to test or trigger another user's callable.

The tool signature for any callable thread is:

```python
ThreadName(task: str) -> str
```

The `task` parameter is the instruction. An injected `config` parameter provides user/thread context. Callable threads are created dynamically by `agents/tool_factory.py` via `create_callable_thread_tool()`, which wraps `thread_agent_executor` in a LangChain `BaseTool`. The factory also handles circular call detection to prevent deadlock.

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

### Outlook Tools (18)

Used internally by OutlookAgent. Also available as **optional tools** for per-thread enabling. Defined in `tools/outlook_auth.py` (4 auth), `tools/outlook_email.py` (13 email), and `tools/outlook_attachments.py` (1 attachment).

**Authentication tools:**

| Tool | Signature | Description |
|------|-----------|-------------|
| `outlook_auth_start` | `()` | Start Microsoft OAuth device code flow. Returns URL and code. |
| `outlook_auth_complete` | `()` | Complete auth after user signs in. Polls Microsoft (up to 5 min). |
| `outlook_auth_clear` | `(account_id?)` | Clear one saved Microsoft account by ID, or all Microsoft accounts plus any pending device-code flow when omitted. |
| `outlook_list_authenticated_accounts` | `()` | List all authenticated Microsoft accounts with IDs. |

**Email tools:**

| Tool | Signature | Description |
|------|-----------|-------------|
| `outlook_list_emails` | `(account_id?, limit=10, folder="inbox", unread_only=False)` | List recent emails with preview and thread ID. Limit max 50. |
| `outlook_get_email` | `(email_id?, email_ids?, account_id?)` | Get full email details including body and attachment metadata. Batch via comma-separated IDs. |
| `outlook_search_emails` | `(query?, queries?, sender?, to?, subject?, folder?, category?, days_back=0, has_attachments=False, thread_id?, kql?, account_id?, limit=10)` | Search emails with filters. Results include body preview (120 chars) and thread ID. Without `days_back`, results ranked by relevance not date. Use `thread_id` to pull full conversation chain. Use `kql` for raw KQL queries (OR logic, etc). Use `category` to find tagged emails. |
| `outlook_send_email` | `(to, subject, body, account_id?, cc?, bcc?, is_html=False)` | Send a new email. |
| `outlook_reply_email` | `(email_id, body, account_id?, reply_all=False)` | Reply to an email (sends immediately). |
| `outlook_draft_reply` | `(email_id, body, reply_all=False, is_html=False, account_id?)` | Create an unsent reply draft that preserves the email thread. Staff reviews and sends manually. |
| `outlook_create_draft` | `(to, subject, body, account_id?, cc?, bcc?, is_html=False)` | Create a standalone draft without sending. |
| `outlook_edit_draft` | `(draft_id, body?, subject?, to?, cc?, bcc?, is_html=False, account_id?)` | Edit an existing draft. Only provided fields are updated. Works on drafts from create_draft or draft_reply. |
| `outlook_delete_email` | `(email_id, account_id?, permanent=False)` | Move to trash or permanently delete. |
| `outlook_mark_email` | `(email_id, is_read, account_id?)` | Mark email as read or unread. |
| `outlook_move_email` | `(email_id, folder, account_id?)` | Move email to a folder. |
| `outlook_forward_email` | `(email_id, to, comment?, account_id?)` | Forward an email. |
| `outlook_set_category` | `(email_id, category, action="add", account_id?)` | Add or remove a category tag on an email. Use to tag emails for processing ("Nymeria") and clear after done. |
| `outlook_get_attachments` | `(email_id, skip?)` | Download and extract text from all email attachments. CSV/TXT decoded directly, Excel via openpyxl, PDF/DOCX/images via Gemini AI. Optional `skip` to ignore irrelevant attachments by name. |

**Folder names** (case-insensitive):
- `outlook_list_emails` accepts: `inbox`, `sent`/`sentitems`, `drafts`, `deleted`/`deleteditems`, `junk`/`junkemail`, `archive`
- `outlook_search_emails` accepts same folders, or omit for all mail
- `outlook_move_email` additionally accepts: `trash` (→ deleteditems), `spam` (→ junkemail)

---

### Calendar Tools (15)

Native Python Google Calendar API client. Defined in `tools/calendar_auth.py` (4 auth) and `tools/calendar.py` (11 event). Uses `google-api-python-client` for direct API calls with agent-guided OAuth flow.

**Authentication tools:**

| Tool | Signature | Description |
|------|-----------|-------------|
| `calendar_auth_start` | `()` | Start Google OAuth flow. Returns authorization URL for the user. |
| `calendar_auth_complete` | `(redirect_url?)` | Complete auth after browser sign-in. Accepts optional redirect URL for manual fallback. |
| `calendar_auth_clear` | `(account_id?)` | Clear one saved Google Calendar account by ID, or all Calendar accounts plus any pending Calendar OAuth flow when omitted. |
| `calendar_list_authenticated_accounts` | `()` | List authenticated Google accounts with token status. |

**Event tools:**

| Tool | Signature | Description |
|------|-----------|-------------|
| `calendar_list_calendars` | `(account_id?)` | List all available Google calendars. |
| `calendar_list_events` | `(calendar_id="primary", max_results=10, time_min?, time_max?, account_id?)` | List events from a calendar. |
| `calendar_get_event` | `(event_id, calendar_id="primary", account_id?)` | Get full event details. |
| `calendar_search_events` | `(query, calendar_id="primary", max_results=10, account_id?)` | Search events by text. |
| `calendar_create_event` | `(summary, start_time, end_time, calendar_id="primary", description?, location?, attendees?, timezone?, account_id?)` | Create a new event. Supports all-day (date-only) and timed events. |
| `calendar_update_event` | `(event_id, calendar_id="primary", summary?, start_time?, end_time?, description?, location?, account_id?)` | Update an existing event (patch — only sends changed fields). |
| `calendar_delete_event` | `(event_id, calendar_id="primary", account_id?)` | Delete a calendar event. |
| `calendar_respond_to_event` | `(event_id, response, calendar_id="primary", account_id?)` | Respond to invitation: `"accepted"`, `"declined"`, `"tentative"`. |
| `calendar_get_freebusy` | `(time_min, time_max, calendars?, account_id?)` | Get free/busy info. `calendars` is comma-separated IDs. |
| `calendar_get_current_time` | `()` | Get current time in ISO 8601 (no API call — local system time). |
| `calendar_list_colors` | `(account_id?)` | List available event colors. |

**Requires:** `GOOGLE_OAUTH_CREDENTIALS` env var pointing to the OAuth Desktop App credentials JSON from Google Cloud Console. Tokens are stored per Nymeria user at `data/auth_tokens/<user_id>/google_calendar.json` with auto-refresh. Node.js/npx are **not** required.

---

### Google Docs/Drive/Sheets Auth Tools (4)

The Google Docs, Drive, and Sheets tools share one OAuth cache at `data/auth_tokens/<user_id>/google_docs.json`.

| Tool | Signature | Description |
|------|-----------|-------------|
| `google_docs_auth_start` | `()` | Start Google Docs/Drive/Sheets OAuth flow. Returns authorization URL for the user. |
| `google_docs_auth_complete` | `(redirect_url?)` | Complete auth after browser sign-in. Accepts optional redirect URL for manual fallback. |
| `google_docs_auth_clear` | `(account_id?)` | Clear one saved Google Docs account by ID, or all Docs/Drive/Sheets accounts plus any pending OAuth flow when omitted. |
| `google_docs_list_accounts` | `()` | List authenticated Google accounts for Docs/Drive/Sheets with token status. |

---

### SelfModify Tools (8)

Used internally by SelfModifyAgent. Defined in `core/self_agent.py`. **Read** and **list** operations work on any path within the project root. **Write** and **delete** are restricted to `nymeria/tools/`, `nymeria/agents/`, and `nymeria/triggers/sources/`.

| Tool | Signature | Description |
|------|-----------|-------------|
| `self_modify_instructions` | `()` | Return the agent's system prompt (instructions for self-modification) |
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

**Currently available (representative categories):**
- Outlook tools: 4 auth + 13 email + 1 attachment = 18 total
- Trigger tools: 4
- Browser tools: 9
- Calendar tools: 4 auth + 11 event = 15 total
- Self-modify tools: 8
- Subagent tools (reload/rollback): 2
- Google Docs tools: 4 auth + 17 document = 21 total
- Google Sheets / _PRV_A tools: 3 base + 5 _PRV_A = 8 total
- Twitch tools: 22
- Watchdog tools: `activity_feed`, `watchdog_dispatch`, `watchdog_read_notepad`, `watchdog_todo_overview` = 4
- Utility tools: `claude_code`, `sticky_note`, `hello_test` = 3

**How it works:**
1. `OPTIONAL_TOOLS` in `tools/__init__.py` maps tool names to tool objects
2. Per-thread config has an `enabled_tools` list (tool names)
3. The profile-level `default_thread_tools` list is the default-bound core set for each thread; an empty list means no core tools
4. During `_build_graph_with_prompt()`, enabled optional tools are added to the thread's tool set
5. Users enable or disable optional tools via thread settings or `PATCH /threads/{id}/config`

The desktop/mobile Thread Settings tools tab mirrors this split: the first list only shows tools in `default_thread_tools`, and the optional/MCP sections show tools outside that default set.

**Important:** `OPTIONAL_TOOLS` currently includes more than just integrations. It also contains tools like `claude_code`, `sticky_note`, `hello_test`, `reload_all`, and `self_modify_rollback`.

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

Representative examples:

**SAFE:** `file_read`, `web_search`, `consult`, `profile_save`, `profile_forget`, `profile_list`, `personality_set`, `rag_search`, `todo`, `todo_delete`, `todo_list`, `notepad_write`, `notepad_read`, `notepad_edit`, `notepad_clear`

**MODERATE:** `bash_execute`, `file_write`, `claude_code`, `notify`, many trigger/email/calendar/browser actions

**SENSITIVE:** self-modify file mutation and rollback tools

For the precise current registry, check `nymeria/tools/metadata.py`, which is the source of truth.

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
