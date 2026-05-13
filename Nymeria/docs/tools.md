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
| 6 | `memory_add` | Profile | SAFE | On | Save a memory. `scope="global"` (keyed user-profile fact) or `scope="thread"` (per-thread notepad). Empty content deletes. |
| 7 | `memory_edit` | Profile | SAFE | On | Surgical find/replace within an existing memory. Empty `replace` deletes the matched text. |
| 8 | `memory_read` | Profile | SAFE | On | Get one keyed memory, list all, or substring-filter via `query`. |
| 9 | `personality_set` | Profile | SAFE | On | Set communication preferences |
| 10 | `rag_search` | Profile | SAFE | On | Semantic search over past conversations |
| 11 | `nym_todo` | TODO | SAFE | On | Create or update a TODO — scheduled TODOs auto-wake the agent |
| 12 | `nym_todo_delete` | TODO | SAFE | On | Delete a TODO permanently |
| 13 | `nym_todo_list` | TODO | SAFE | On | List TODO items |
| 14 | `notify` | Core | MODERATE | On | Send in-app and external notifications |

> **Skill meta-tool:** A single `Skill(name)` tool is synthesized per-thread at graph-build time when any skills are active — it's not in `ALL_TOOLS`. Its description carries an `<available_skills>` index of `(name, description)` pairs; calling it returns that skill's full SKILL.md body. Skill Kits can additionally declare `metadata.nymeria.required_tools`; activation strictly binds those tools with a TTL before resuming the same turn. See `docs/skills.md`.

> **Capability expansion:** Tool discovery/enabling, MCP management, skill management, API probing, and Skill Kit authoring are no longer default tools. The bundled `self-improve` Skill Kit is enabled by default and binds `tool_search`, `tool_enable`, `manage_mcp`, `skill_manage`, `api_discover`, `http_request`, and `skill_kit_create` only when the agent activates it.

> **Note:** `claude_code`, `reload_all`, and `self_modify_rollback` are **not** in core `ALL_TOOLS`. They live in `OPTIONAL_TOOLS` (`reload_all` / `self_modify_rollback` via `RUNTIME_ADMIN_TOOLS`, `claude_code` directly) and are in `ADMIN_ONLY_OPTIONAL_TOOL_NAMES` — admins can enable them per-thread, non-admins are blocked at every enable boundary. See `nymeria/tools/__init__.py` for the canonical lists.

> **Tool output guard:** After any tool executes, Nymeria truncates oversized `ToolMessage` content before it is stored in thread history. `TOOL_OUTPUT_MAX_CHARS` defaults to `100000`; larger outputs keep the first ~75k and last ~25k characters with a marker showing the original and omitted sizes.

> **CLIProxy OAuth note:** Installed server tools keep the safe dynamic namespace `mcp__<server>__<tool>`. Nymeria-owned helper tools must avoid the `mcp_<name>`, `mcp.<name>`, and `mcp/<name>` namespaces because Claude OAuth classifies those as third-party MCP apps. The consolidated facade is named `manage_mcp`; legacy helpers remain `search_mcp` and `install_mcp_server` for compatibility. The observed probe matrix is documented in `docs/cliproxy.md`.

### Optional: Trigger Tools (2)

Not loaded by default. Enable per-thread via thread config, or use through SelfModifyAgent.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `trigger_config` | Trigger | MODERATE | Create, update, enable/disable, or delete event triggers |
| 2 | `trigger_info` | Trigger | SAFE | List triggers, inspect/test/history for one trigger, or show source schemas |

### Optional: Slash Command Tool (1)

Not loaded by default. Enable per-thread to let the agent invoke the same user-facing slash commands that the Discord/Telegram bots expose.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `slash_command` | Self | MODERATE | Run a Nymeria slash command on the current thread (config, env, tools, memory, TODOs, notepad, status). Destructive commands blocked. |

### Optional: Capability Expansion and Authoring

Not loaded by default. The normal path is to activate `Skill(name="self-improve")`,
which binds the facades below with a TTL.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `tool_search` | Core | SAFE | Search available tools |
| 2 | `tool_enable` | Core | MODERATE | Enable, disable, and inspect current-thread tool bindings |
| 3 | `manage_mcp` | MCP | MODERATE | Search, preview, install, and inspect MCP servers |
| 4 | `skill_manage` | Skills | MODERATE | List, search, install, enable, disable, and inspect skills |
| 5 | `http_request` | Core | MODERATE | Make a one-off HTTP request to a documented API endpoint |
| 6 | `api_discover` | Core | MODERATE | Discover OpenAPI/Swagger metadata for an API base URL |
| 7 | `skill_kit_create` | Custom | MODERATE | Create HTTP tools and package durable Skill Kits |
| 8 | `tool_create` | Custom | MODERATE | Compatibility low-level HTTP tool authoring |
| 9 | `skill_config` | Custom | MODERATE | Compatibility low-level Skill Kit authoring |
| 10 | `search_mcp` / `install_mcp_server` | MCP | SAFE/MODERATE | Compatibility low-level MCP helpers |
| 11 | `list_installed_skills` / `search_skills` / `install_skill` | Skills | SAFE/MODERATE | Compatibility low-level skill helpers |

### Optional: Private B Tools (4)

Not loaded by default. Enable per-thread when the agent needs to manage Example University workload data from the LMS/Moodle. Configuration lives per user in `data/auth_tokens/<user_id>/_prv_b.json`; env fallbacks are `_PRV_B_CALENDAR_URL`, `_PRV_B_RSS_FEEDS`, `_PRV_B_MOODLE_BASE_URL`, and `_PRV_B_MOODLE_TOKEN`. See `docs/_prv_b.md`.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `_prv_b_auth` | Private B | MODERATE | Configure/inspect the LMS auth; actions: `status`, `setup_guide`, `start_mobile_token_flow`, `parse_mobile_redirect`, `configure_calendar`, `configure_rss`, `configure_moodle_token`, `clear` |
| 2 | `_prv_b_calendar` | Private B | MODERATE | Read the Moodle calendar `.ics` export URL; actions: `configure`, `status`, `clear`, `list`, `get`, `search` |
| 3 | `_prv_b_rss` | Private B | MODERATE | Read the LMS forum or announcement RSS feeds; actions: `configure`, `status`, `clear`, `list`, `get`, `search` |
| 4 | `_prv_b_moodle` | Private B | MODERATE | Read Moodle mobile API data from a `moodle_mobile_app` token; actions: `configure`, `status`, `clear`, `site_info`, `courses`, `course_contents`, `course_module`, `assignments`, `upcoming_events`, `grades`, `forums`, `forum_discussions`, `discussion_posts` |

`_prv_b_auth(start_mobile_token_flow)` returns an the LMS mobile-app launch URL. The user completes the LMS/Microsoft MFA in a browser and gives Nymeria the resulting `moodlemobile://token=...` redirect via `parse_mobile_redirect`; Nymeria then stores the decoded Moodle `wstoken` for the read tools. The tool does not store the user's MQ password or TOTP secret.

### Optional: File Editing (1)

Not loaded by default. Enable per-thread when the agent needs precise text edits instead of full-file rewrites.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `file_edit` | Core | MODERATE | Exact, all-or-nothing edits to existing text files |

### Optional: Image Generation (1)

Not loaded by default. Enable per-thread, or promote to Core in the Desktop global Tools settings. The tool writes generated images under `NYMERIA_WORKSPACE_DIR/image-generation/`, returns a workspace artifact via `[attach:/path]`, and stores only small artifact metadata in chat history. On the next reasoning step, Nymeria hydrates recent generated images into native vision input for supported chat providers: Anthropic vision models and OpenAI/OpenRouter models using `OPENAI_API_MODE=responses`. Other provider modes still see the file path and artifact.

API keys follow the existing provider-key pattern: set `OPENAI_API_KEY` for OpenAI GPT Image models and `GEMINI_API_KEY` for Gemini/Nano Banana models. The Desktop edit dialog configures provider/model/output options, not secret storage.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `image_generate` | Image | MODERATE | Generate a new image from a prompt using OpenAI `gpt-image-*` or Gemini Nano Banana models, attach it to the chat, and expose it to vision-capable follow-up reasoning |

Configurable options:

- `provider`: `openai` or `gemini`
- OpenAI: `openai_model`, `openai_size`, `openai_quality`, `openai_output_format`, `openai_moderation`
- Gemini: `gemini_model`, `gemini_aspect_ratio`, `gemini_image_size`
- `native_context_enabled`: whether supported chat models should inspect generated images natively on the next LLM call

### Optional: _PRV_A and Sheets Tools (8 + 1 attachment)

Google Sheets-based tools for Acme Hardware RFQ processing. Generic `google_sheets_*` tools use the current user's Google OAuth. The dedicated `_prv_a_*` reference tools live in the `nymeria/plugins/_prv_a/` package and use the app-level `_PRV_A_SERVICE_ACCOUNT_FILE` service account with 5-minute in-memory caching, so _PRV_A lookups do not depend on whichever user is authenticated for Google Docs. Products, Acme, and Vendor tools share a batch-search helper (`plugins/_prv_a/sheet_lookup.py`); Acme and Supplier have domain-specific search logic. The `outlook_get_attachments` tool (last in the table) is from `OUTLOOK_ATTACHMENT_TOOLS`, not a _PRV_A module; it's placed here as a general-purpose extraction utility.

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

Create a callable thread: open thread settings → Agent → check "Make Callable" → set a name and description. On desktop, the thread row's Agent shortcut opens this tab directly. The thread becomes available as a tool to **the creator's own threads** after `sync_agent_tools()` runs — callables are scoped to their owner (the user who created them) and the `_thread_owners` table determines visibility. Two users can independently create callables with the same `callable_name`; each user's graph binds their own version, and the runtime ownership gate in `agents/tool_factory.py` blocks cross-user invocation.

Callable tools default to blocking `mode="ask"`, which returns the target thread's final answer. Use `mode="handoff"` to transfer work to the target thread without waiting; the caller receives only a dispatch receipt while the target thread streams through its normal autonomous output channels.

Callable teams can scope which callable threads a thread sees. When a thread has `callable_team_id`, graph building includes only the owner's callable threads with the same team id. Unteamed threads keep the existing owner-wide callable visibility for backward compatibility. The desktop sidebar has a folder/team organization toggle and a bulk Team action for creating teams from selected threads.

The frontend header count uses `GET /threads/{thread_id}/callable-tools`, which mirrors runtime visibility for that caller thread: ownership, team scoping, self-exclusion, disabled tools, and name conflicts.

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
- `attach` (`bool`, default `False`): Deliver the written file back to chat clients (Telegram, Discord, desktop/mobile artifact viewers). Only files inside `NYMERIA_WORKSPACE_DIR` (default `/workspace`) are attachable. When attach succeeds, the raw tool result includes an `[attach:/path]` tag for backward compatibility and the API emits a structured `workspace_artifact` SSE event.

**Returns:** Success/error message with character count. When `attach=True` and the file is inside the workspace directory, the raw tool result includes an `[attach:/path]` tag and clients receive a `workspace_artifact` event. If the file is outside the workspace, the write still succeeds but attachment delivery is skipped with an info note.

**Protected paths:** Writes to `nymeria/core/`, `nymeria/config/`, `nymeria/triggers/`, `nymeria/gateway/`, and `nymeria/__init__.py` are blocked. Use the SelfModifyAgent for those directories.

---

### file_edit (Optional)

Precisely edit an existing text file with exact, all-or-nothing operations. This is not loaded by default; enable it per-thread before use.

```python
file_edit(file_path: str, edits: list[dict], encoding: str = "utf-8", dry_run: bool = False, expected_sha256: Optional[str] = None, max_diff_chars: int = 20000)
```

**Parameters:**
- `file_path` (`str`): Absolute or relative path to an existing file
- `edits` (`list[dict]`): Ordered edit operations. Each operation is applied to the in-memory result of prior operations.
- `encoding` (`str`, default `"utf-8"`): File encoding
- `dry_run` (`bool`, default `False`): Return validation and diff without writing
- `expected_sha256` (`Optional[str]`, default `None`): Optional SHA-256 of the current file bytes; mismatches fail before edits are evaluated
- `max_diff_chars` (`int`, default `20000`): Maximum unified diff characters to return; `0` suppresses the diff

**Operations:**
- `replace`: requires `old_text`; writes `new_text` in its place
- `delete`: requires `old_text`; removes the match
- `insert_before`: requires `old_text`; inserts `new_text` before the match
- `insert_after`: requires `old_text`; inserts `new_text` after the match
- `replace_range`: requires `start_line`, `end_line`, and `old_text`; replaces the 1-based inclusive line range with `new_text` only if `old_text` exactly equals the selected range

**Matching rules:** `old_text` must be exact and non-empty. If `occurrence` is omitted, `old_text` must match exactly once. If `occurrence` is provided, it is 1-based and selects that exact match. Fuzzy matching is intentionally not used.

**Returns:** JSON with `ok`, `dry_run`, `file_path`, `edits_applied`, `original_sha256`, `new_sha256`, `changed`, `diff`, and `diff_truncated`; errors include a structured `error.type`, message, and `edit_index` when relevant.

**Safety:** Edits are all-or-nothing and written atomically. The tool does not create backups. It preserves the existing file mode and dominant newline style for inserted/replacement text.

**Limits and protected paths:** Same 10 MB text-file limit and protected Nymeria paths as `file_write`.

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

## Memory Tools

Three unified primitives — `memory_add`, `memory_edit`, `memory_read` — cover both global user-profile facts and per-thread notepad content. The `scope` argument selects which store:

- `scope="global"` — keyed entries in the user's profile, **automatically injected** into Nymeria's system prompt across every future thread. Storage: `data/users/{user_id}/profile.json`.
- `scope="thread"` — free-form markdown notepad for the active thread, re-injected after context compaction. Storage: `data/thread_notes/{thread_id}.md`.

Empty `content` (in `memory_add`) or empty `replace` whose result empties the entry (in `memory_edit`) deletes cleanly: profile rows are popped, notepad files are unlinked. There is no separate `memory_forget` because the storage layer treats blank-as-delete, so edit-to-blank leaves no zombie entries.

> **Implementation note:** `memory_add`, `memory_edit`, `memory_read`, `personality_set`, and `rag_search` accept an `Annotated[RunnableConfig, InjectedToolArg]` parameter that LangGraph injects automatically. The LLM never passes it.

### memory_add

Save a memory. Creates a new entry or overwrites an existing one.

```python
memory_add(scope: str, content: str, key: Optional[str] = None)
```

**Parameters:**
- `scope` (`"global"` | `"thread"`): which store to write to.
- `content` (`str`): the memory text. Empty string deletes.
- `key` (`str`, required for `scope="global"`): identifier for the profile entry. Ignored for `scope="thread"`.

**Examples:**
```python
memory_add(scope="global", key="prefers_typescript", content="Yes")
memory_add(scope="global", key="timezone", content="Australia/Sydney")
memory_add(scope="thread", content="Working on auth refactor; deadline Friday.")
memory_add(scope="global", key="prefers_typescript", content="")   # deletes the entry
memory_add(scope="thread", content="")                             # deletes the notepad
```

**Behavior:**
- `scope="global"` upserts into `UserProfile.memories` and re-indexes in the RAG store if RAG is enabled.
- `scope="thread"` overwrites the notepad (replace semantics; for append-style writes, read-then-add).
- Notepad max size: 50 KB. Profile max entries: 100. Profile values are truncated to 1000 chars.

---

### memory_edit

Surgical find/replace within an existing memory.

```python
memory_edit(scope: str, find: str, replace: str = "", key: Optional[str] = None)
```

**Parameters:**
- `scope` (`"global"` | `"thread"`).
- `find` (`str`): exact substring to locate (first occurrence).
- `replace` (`str`, default `""`): replacement text. Empty string deletes the matched substring.
- `key` (`str`, required for `scope="global"`): which profile entry to edit.

**Examples:**
```python
memory_edit(scope="global", key="job_title", find="Engineer", replace="Senior Engineer")
memory_edit(scope="thread", find="deadline Friday", replace="deadline Monday")
memory_edit(scope="thread", find="obsolete bullet point\n", replace="")   # delete the line
```

**Behavior:**
- If the resulting value is empty, the entry/notepad is removed.
- For long profile values use `memory_add` to overwrite — `memory_edit` shines for thread-notepad surgical edits.

---

### memory_read

Read memory: get one entry, list everything, or substring-filter.

```python
memory_read(scope: str, key: Optional[str] = None, query: Optional[str] = None)
```

**Parameters:**
- `scope` (`"global"` | `"thread"`).
- `key` (`str`, global only): fetch a single profile memory by key.
- `query` (`str`): substring filter (case-insensitive). For semantic search use `rag_search`.

**Examples:**
```python
memory_read(scope="global")                              # list all memories + personality
memory_read(scope="global", key="timezone")              # get one
memory_read(scope="global", query="prefers")             # filter by substring
memory_read(scope="thread")                              # full notepad
memory_read(scope="thread", query="deadline")            # only matching notepad lines
```

**Notes:**
- Profile memories are auto-injected into the system prompt, but weaker models may struggle to extract exact keys from long prompts — `memory_read(scope="global")` gives an explicit listing.

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

To opt out, use the RAG settings API or the frontend settings UI. The migration watermark prevents re-flipping on subsequent loads.

---

## TODO Tools

TODOs are the **primary driver for autonomous operation**. Active TODOs are automatically injected into the system prompt. Every TODO must have a `scheduled_for` time — TODOs are for the agent's autonomous work queue, not a general task list.

> **Implementation note:** `nym_todo`, `nym_todo_delete`, and `nym_todo_list` all accept an injected `config` parameter for user/thread identification. The LLM never passes this.
>
> **Migration note:** legacy `todo`, `todo_delete`, and `todo_list` config names are migrated to `nym_todo`, `nym_todo_delete`, and `nym_todo_list`. Older `todo_add` and `todo_update` flows were merged into the single `nym_todo` tool. Priority, deadline, blocked status, and the permanent flag have been removed.

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

**Thread scope:** Creates TODOs on the current thread. Updates only find TODOs that already belong to the current thread; a TODO ID from another thread is treated as not found.

**Statuses:** `pending` (default), `in_progress`, `done`. Use `nym_todo(todo_id=..., status="done")` to complete a TODO.

**Recurring TODOs:** Recurring TODOs **auto-reschedule when marked done** — regardless of whether the ticker executed them or the agent/user marked them done manually. The next `scheduled_for` is calculated from the `recurrence` pattern and the status resets to `pending`. This applies to all completion paths: the `nym_todo` tool, the REST API, and the MCP server. To permanently stop a recurring TODO, use `nym_todo(todo_id=..., clear_recurrence=True)` or `nym_todo_delete`.

**Auto-purge:** Completed TODOs remain visible to `nym_todo_list(filter_status="done")` and `GET /todos?filter_status=done` until the ticker cleanup removes them from `data/todos/{user_id}.json`. The retention is controlled by `TODO_AUTO_ARCHIVE_DAYS` (default 7 days, range 1-30). There is no separate completed-TODO archive file; use activity/RAG history for historical outcome lookup after cleanup.

---

### nym_todo_delete

Delete a TODO permanently. No archive — immediately removed. Cancels any scheduled execution.

```python
nym_todo_delete(todo_id: str)
```

**Parameters:**
- `todo_id` (`str`): The 8-character TODO ID

Only deletes TODOs that belong to the current thread. A TODO ID from another thread is treated as not found.

**Returns:** Confirmation with deleted task text, or error.

---

### nym_todo_list

List TODO items for the current thread. Shows active (non-done) by default.

```python
nym_todo_list(filter_status: Optional[str] = None)
```

**Parameters:**
- `filter_status` (`Optional[str]`): `"pending"`, `"in_progress"`, `"done"`, or `"all"`

`filter_status="all"` includes all statuses for the current thread only. It does not list TODOs from other threads; use explicit dashboard/API views or `watchdog_todo_overview` for cross-thread TODOs.

**Returns:** Formatted list sorted by: status (in_progress first, then pending, then done), then scheduled time, then creation date.

**Status icons:** `[ ]` pending, `[>]` in_progress, `[x]` done.

---

## Notification Tool

### notify

Send notifications to the in-app notification center and messaging platforms
(Telegram, Discord, Slack, Teams).

```python
notify(message: str, platform: Literal["auto", "desktop", "telegram", "discord", "slack", "teams"] = "auto")
```

**Parameters:**
- `message` (`str`): The message text to send
- `platform` (`Literal["auto", "desktop", "telegram", "discord", "slack", "teams"]`, default `"auto"`): Target platform. `"auto"` creates an in-app notification and tries all configured platforms.

**Returns:** Success/error message.

**Requires:** Platform-specific credentials in `.env`:
- Desktop/in-app: no external credentials
- Telegram: bound Telegram chat for the current thread, or `TELEGRAM_BOT_TOKEN` + `TELEGRAM_DEFAULT_CHAT_ID` fallback
- Discord: `DISCORD_WEBHOOK_URL`
- Slack: `SLACK_WEBHOOK_URL`
- Teams: `TEAMS_TEAM_ID` + `TEAMS_CHANNEL_ID` plus Microsoft auth

**Behavior in auto mode:** Creates a notification-center row unless the thread disables in-app notifications, then tries all configured external platforms. If the current thread is a Telegram thread or is bound to a Telegram chat, Telegram delivery is routed through that chat; otherwise Telegram falls back to the configured default chat. If any destination succeeds, returns the success messages (failures are not reported in mixed outcomes). If all fail, returns all errors.

**Architecture:** Platform senders, notification-level helpers, and composite dispatch functions live in `core/notification_dispatch.py`. The `notify` tool, the ticker (scheduled TODO completions), the API chat endpoint (autonomous completions), and the watchdog (stale TODO alerts) all delegate to this shared module rather than owning independent notification logic. `create_autonomous_notification()` combines in-app notification creation with FCM push in a single call gated by the per-thread `in_app_notification_level` setting.

### tool_search

Search available tools by keyword/category. This is search-only; mutations live
in `tool_enable`.

```python
tool_search(query: str = "", category: str = "", top_k: int = 15, include_status: bool = True)
```

**Parameters:**
- `query` (`str`): Keyword to search tool names and descriptions.
- `category` (`str`): Optional category filter (e.g. `"email"`, `"twitch"`).
- `top_k` (`int`): Result count, default 15 and capped at 50.
- `include_status` (`bool`): Include current-thread enabled/disabled annotations.

### tool_enable

Enable, disable, or inspect current-thread tool bindings. This is normally
available after the agent activates `Skill(name="self-improve")`.

```python
tool_enable(action: str, tools: list[str] = None, category: str = "", ttl: str = "2h", force: bool = False)
```

**Actions:**
- `enable` — Enable tools by name (`tools`) or by category (`category`). In `astream()` (REST/SSE and sync-worker bridge callers) and `chat()` (MCP final-string path), this triggers an in-turn graph rebuild so the tools are callable in the very next step of the same user message.
- `disable` — Disable tools for the thread (`tools`). Takes effect on the next agent step. Refuses core tools (`bash_execute`, `file_read`, etc.) unless `force=True`. Mixed batches partially succeed: non-core names are disabled, core names are listed under `[Refused]` with a hint to retry that subset with `force=True`. Disable is non-destructive — it only appends to `disabled_tools`; entries in `enabled_tools` / `temporary_tools` are preserved, so a subsequent `enable` restores the tool's original permanent/TTL state. "Core" here is the hardcoded `ALL_TOOLS` set, which is a **superset** of what the `already_default` classifier bucket calls default-bound (user profile's `default_thread_tools` curates a subset of `ALL_TOOLS`).
- `list_categories` — List all tool categories with tool counts.
- `status` / `inspect` — Show currently enabled/disabled tools for this thread, with TTL remaining per entry.

**Parameters:**
- `action` (`str`): One of: `enable`, `disable`, `list_categories`, `status`.
- `tools` (`list[str]`): Specific tool names to enable or disable.
- `category` (`str`): Category name to enable all tools in.
- `ttl` (`str`): For `enable` only — how long to keep tools bound before lazy eviction. One of: `"30m"`, `"2h"` (default), `"6h"`, `"24h"`, `"permanent"`.
- `force` (`bool`): For `disable` only — set `True` to allow disabling core tools. Default `False`.

**Enable response buckets:** every input tool is classified in exactly one bucket, checked in this priority order — (1) `Un-disabled` (was in `disabled_tools`, now removed; if the tool has a preserved `enabled_tools` or `temporary_tools` entry, it is restored AS-IS — the requested `ttl` does NOT apply, so a batch-level TTL can't silently promote/demote an unrelated tool; a fresh entry is only written when there is no preserved state and no default binding), (2) `Already permanent` (in `tc.enabled_tools`; TTL requests are rejected, no demotion), (3) `Already bound (default set)` (in the thread's default-bound set — `ALL_TOOLS` or the user-profile-level `default_thread_tools` override; already callable, no write), (4) `TTL refreshed` (in `tc.temporary_tools`; `expires_at` pushed out), (5) `Promoted to permanent` (in `tc.temporary_tools`, `ttl="permanent"` → moved to `tc.enabled_tools`), (6) `Newly loaded` (none of the above; written fresh to `enabled_tools` or `temporary_tools` depending on `ttl`).

The classifier sources its default-bound set from the same place as graph-build (`agent._build_graph_with_prompt`: `profile.tool_preferences.default_thread_tools` if set, else `{t.name for t in ALL_TOOLS}`). Tools that live in `ALL_TOOLS` but are excluded from the user's `default_thread_tools` list are correctly treated as optional (priority-6 newly-loaded) rather than already-bound. Note: the bucket is called `Already bound (default set)` — not "core" — to avoid conflating it with the `disable` guard's "core" protection, which uses the broader `ALL_TOOLS` list.

**`disabled_tools` is authoritative in graph-build.** The graph-build pipeline is: start with the default-bound set, filter out `disabled_tools`, then add extras from `enabled_tools ∪ live_temporary_tools` — BUT extras are also filtered by `disabled_tools` before merging. So a tool listed in both `enabled_tools` and `disabled_tools` is unbound (disable wins). This lets `disable` be non-destructive: it only appends to `disabled_tools` and leaves `enabled_tools` / `temporary_tools` alone. An `enable` on that same tool just removes it from `disabled_tools`; the preserved permanent/TTL entry comes back automatically. Without this rule, `disable` would have to destructively mutate `enabled_tools` to actually disable an overlapping tool, and a disable→enable round-trip would silently strip the permanent badge.

**Status display filters disabled tools from the enabled sections.** Because `disabled_tools` is authoritative, a tool that has a preserved `enabled_tools` or `temporary_tools` entry while ALSO being in `disabled_tools` is currently unbound. The `status` and `search` renderers suppress such tools from the `Enabled (permanent)` / `Enabled (TTL)` sections and annotate them in the `Disabled` section with `(preserved: permanent)` or `(preserved: Xm left)`, so the user can still see what will round-trip back on un-disable without seeing the same tool in two places.

### manage_mcp

Search, preview, install, and inspect MCP servers through the capability
expansion path.

```python
manage_mcp(action: str, query: str = "", source: str = "", name: str = "", confirmed: bool = False, config_values: dict = {}, ttl: str = "2h", auto_enable_thread: bool = True)
```

Actions are `search`, `preview`, `install`, and `inspect`/`status`/`list`.
Successful installs reload MCP server tools, enable discovered
`mcp__<server>__<tool>` tools on the current thread when
`auto_enable_thread=true`, and queue a same-turn reload with
`source="mcp_install"`. Installing MCP servers remains admin-only at execution
time because stdio servers can launch local commands.

### skill_manage

List, search, install, enable, disable, or inspect Agent Skills.

```python
skill_manage(action: str, query: str = "", name: str = "", source: str = "installed", scope: str = "user", activate_current_thread: bool = False)
```

Actions are `list`, `search`, `install`, `enable`, `disable`, and `inspect`.
Marketplace installs default to user scope; global scope requires admin.
When a skill is installed or enabled on the current thread and a graph rebuild
is needed, reload metadata uses `source="skill_install"`.

#### In-turn auto-continue

When the agent calls `tool_enable(action="enable", tools=[...])` during a turn, the enable result explicitly tells the agent to stop after that tool result. It should not write a final answer, explain the enablement, or attempt to call the newly enabled tool in the same graph invocation. The harness then:

1. Persists the enablement to the thread config (with TTL) and invalidates the cached graph.
2. Finishes the current graph invocation normally.
3. Emits a `tool_reload` SSE event (`{type: "tool_reload", tools, ttl, ttl_seconds, source, skill_name, reason}`).
4. Builds a fresh graph with the new tools bound to the LLM.
5. Injects an internal resume message (`internal_type="tool_reload_resume"`) and drives the new graph against it, streaming into the same SSE connection.

To the client this looks like one continuous turn: no extra `done` event, no separate user message. The thread lock stays held the whole time. The loop is capped at `AgentCore.MAX_TOOL_RELOADS_PER_TURN` rebuilds per user turn to bound token usage. Once the cap is hit, `tool_enable(action="enable")` and Skill Kit activation stop returning `Command(goto=END)` and instead return a plain string whose body includes a `[Reload cap hit]` notice — the agent can still respond in-turn, and the new binding takes effect on the next user message. This prevents an orphaned `tool_result` with no LLM follow-up (symptom: the stream looks like it froze because the last enable's `Command` ended the graph but the reload loop was already exhausted).

`astream()` (REST/SSE and sync-worker bridge callers) and `chat()` (MCP final-string path) honor the auto-continue. The streaming path emits `tool_reload` and then drives the fresh post-reload graph through the same live event conversion, so resumed `thinking`, `tool_call`, `tool_result`, `workspace_artifact`, and `response` chunks remain visible in the same turn. Scheduled TODOs, triggers, callable threads, spawned threads, and the CLI consume `astream()` through `core/stream_bridge.py`, which keeps async-only tools such as `tool_create` available outside regular chat. The bridge uses one process-local asyncio loop for synchronous callers, and async graph caches plus provider SDK HTTP pools are loop-local so FastAPI-loop chat and bridge-loop callable calls do not share loop-bound transports. Autonomous callers use `stream_and_collect()` for shared response/thinking collection, error propagation, and iteration-limit tracking before publishing their own completion payloads. `chat()` remains non-streaming and returns only the final string.

#### TTL and eviction

Each enablement (other than `ttl="permanent"`) gets an `expires_at` timestamp stored in `ThreadConfig.temporary_tools`. At the start of every new turn, `_build_graph_with_prompt` calls `_resolve_temporary_tools(tc)` which:

1. Drops entries whose `expires_at` has passed.
2. Persists the cleaned config back to disk.
3. Returns the still-live set for inclusion in the tool list.

Eviction never happens mid-invocation, so a tool that was bound at the start of a graph run is callable for the whole run — there are no surprise eviction errors. Calling `enable` on a tool already in `temporary_tools` refreshes `expires_at`; calling `enable` with `ttl="permanent"` promotes the entry into `enabled_tools` (which has no expiry and is also what the UI/API writes to). Calling `disable` adds the name to `disabled_tools` without deleting preserved permanent/TTL state, so a later enable restores that state.

Pick the shortest TTL that covers your task. `2h` is a sensible default for multi-step tasks; `30m` for one-shots; `6h`/`24h` for sustained workflows; `permanent` only if the tool should remain as a standing capability on the thread.

---

## Agent Management / Self-Modify Tools

### reload_all

Reload all tools, agents, and trigger sources. Call after SelfModifyAgent creates or modifies code, or after manual file edits.

```python
reload_all()
```

**Returns:** Count of reloaded tools and trigger sources.

**Availability:** Present in `RUNTIME_ADMIN_TOOLS` / optional tooling, not in the always-loaded core `ALL_TOOLS` list.

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

### trigger_config

Create, update, enable/disable, or delete event triggers.

```python
trigger_config(
    action: str,
    trigger_id: Optional[str] = None,
    name: Optional[str] = None,
    source_type: Optional[str] = None,
    action_type: Optional[str] = None,
    action_config: Optional[dict] = None,
    source_config: Optional[dict] = None,
    cooldown_seconds: Optional[int] = None,
    conditions: Optional[list] = None,
    enabled: Optional[bool] = None,
)
```

**Actions:**
- `create` — Create a new trigger, bound to the current thread.
- `update` — Patch an existing trigger's display name, enabled state, source/action config, cooldown, or conditions.
- `delete` — Delete a trigger permanently.

**Parameters:**
- `action` (`str`): `"create"`, `"update"`, or `"delete"`
- `trigger_id` (`Optional[str]`): Required for `update` and `delete`
- `name` (`str`): Human-friendly trigger name (e.g., `"Wake-up morning briefing"`)
- `source_type` (`str`): Event source type. Use `"webhook"` for HTTP push triggers. Call `trigger_info(action="sources")` to see available sources.
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
- `conditions` (`Optional[list]`): Filter conditions; pass `[]` on update to clear conditions
- `enabled` (`Optional[bool]`): Enable or disable a trigger on update

**Returns:** Success or error message. Create returns the trigger ID and webhook URL for webhook sources.

**Examples:**
```python
trigger_config(
    action="create",
    name="Wake-up briefing",
    source_type="webhook",
    action_type="agent_prompt",
    action_config={"prompt_template": "User woke up at {fired_at}. Create morning briefing."},
)

trigger_config(action="update", trigger_id="a1b2c3d4", enabled=False)
trigger_config(action="delete", trigger_id="a1b2c3d4")
```

---

### trigger_info

List triggers, inspect one trigger, dry-run test one trigger, fetch execution history, or list trigger source schemas.

```python
trigger_info(
    action: str = "list",
    trigger_id: Optional[str] = None,
    enabled_only: bool = False,
    current_thread_only: bool = False,
    limit: int = 10,
)
```

**Actions:**
- `list` — List trigger summaries.
- `detail` — Show one trigger's configuration, health, conditions, pending events, and thread binding.
- `test` — Dry-run one trigger with sample event data. Does not fire the trigger.
- `history` — Show recent execution history for one trigger.
- `sources` — Show available trigger source types, config fields, template variables, and examples.

**Parameters:**
- `action` (`str`, default `"list"`): `"list"`, `"detail"`, `"test"`, `"history"`, or `"sources"`
- `trigger_id` (`Optional[str]`): Required for `detail`, `test`, and `history`
- `enabled_only` (`bool`, default `False`): If `True`, only show enabled triggers
- `current_thread_only` (`bool`, default `False`): If `True`, only show triggers bound to the current thread
- `limit` (`int`, default `10`, max `50`): Number of executions for `action="history"`

**Returns:** Formatted trigger summaries, trigger details, test output, execution history, or source catalog.

**Examples:**
```python
trigger_info(action="list", current_thread_only=True)
trigger_info(action="detail", trigger_id="a1b2c3d4")
trigger_info(action="test", trigger_id="a1b2c3d4")
trigger_info(action="history", trigger_id="a1b2c3d4", limit=5)
trigger_info(action="sources")
```

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

**Runtime behavior:** Works in both normal conversation turns and autonomous scheduled TODO runs. Some tool callers still need a synchronous return value, so `slash_command` provides both sync and async invocation modes even though the underlying dispatcher talks to the local API asynchronously.

**Requirements:**
- `NYMERIA_API_URL` — defaults to `http://api:8000` inside Docker or `http://localhost:8000` outside.
- Authenticates with `NYMERIA_SERVICE_TOKEN` (admin service token) plus `X-Nymeria-Act-As: <caller_user_id>` so each invocation runs under the requesting user. The legacy shared `NYMERIA_API_KEY` was retired — see `docs/accounts.md`.

**Security note:** The tool runs in-process against the local API as the calling user (via act-as routing). `/env get` returns unmasked secrets and is admin-only at the API layer — non-admin callers will get 403 if they try to invoke admin-gated slash commands like `/env_get`, `/restart`, or `/config_*`.

**Implementation:** See `nymeria/tools/slash_command.py` (parser + denylist + tool entry point) and `nymeria/triggers/slash_dispatcher.py` (command → API-method routing and plain-text formatting). Mirrors the Telegram bot's command handlers but emits plain text instead of HTML.

---

## HTTP/API and Skill Authoring Tools (Optional)

General-purpose API primitives and authoring tools for one-off integration work, reusable HTTP tools, and generated Skill Kits. These are not loaded by default; normally load `Skill(name="self-improve")`, then enable per-thread with `tool_enable` when needed.

### http_request

Make a single HTTP request and return structured JSON.

```python
http_request(
    method: str,
    url: str,
    headers: Optional[dict] = None,
    query: Optional[dict] = None,
    body: Optional[Any] = None,
    timeout_seconds: int = 30,
    follow_redirects: bool = True,
    response_format: str = "auto",
    max_response_chars: int = 20000,
)
```

**Parameters:**
- `method` (`str`): `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, or `OPTIONS`
- `url` (`str`): Absolute `http://` or `https://` URL
- `headers` (`dict`, optional): Request headers
- `query` (`dict`, optional): Query parameters
- `body` (`Any`, optional): JSON-serializable body; strings are sent as raw content
- `timeout_seconds` (`int`, default `30`): Clamped to 1-300 seconds
- `follow_redirects` (`bool`, default `True`): Follow redirects
- `response_format` (`str`, default `"auto"`): `"auto"`, `"json"`, or `"text"`
- `max_response_chars` (`int`, default `20000`): Body truncation limit, clamped to 1-200000

**Returns:** JSON with `tool_version`, `ok`, `http_ok`, `format_ok`, request method/url, response status/final URL/selected headers/elapsed time, policy metadata, body metadata, and error details. `ok` means the HTTP status was successful and the requested response format was satisfied. `http_ok` only reflects the HTTP status. `format_ok` is false when, for example, `response_format="json"` was requested but the response body was not JSON. Validation/network/policy errors set `http_ok` and `format_ok` to `null` because no HTTP response body was parsed.

**Body shape:** Complete JSON responses return `body_type="json"` and `body` as an object/list. Complete text returns `body_type="text"` and `body` as a string. Truncated responses keep `body` as `null` and put the returned excerpt in `body_preview`, so agents do not confuse truncated JSON text for a complete parsed object. JSON parse attempts include `json_parse_ok`; failed forced-JSON parsing includes `parse_error`. Binary responses set `body_type="binary"`, `body_omitted=true`, and describe the omitted payload in `body_preview`.

**Network policy:** HTTP tools are public-internet-only by default. The runtime blocks loopback, private, link-local, reserved, unspecified, multicast, and metadata targets, including hostnames that resolve to those addresses. Internal/local access requires server-side `HTTP_INTERNAL_ALLOWLIST` entries; the model cannot opt into it per request. Blocked requests return `error.type="blocked_network_target"` and a `policy` object with the reason.

Redirects are followed manually by default so every hop is policy-checked. Redirects to blocked targets are refused before the target is requested. HTTPS-to-HTTP redirects are blocked unless `HTTP_ALLOW_HTTPS_TO_HTTP_REDIRECT=true`. Successful redirect-following responses include `redirect_chain`. When `follow_redirects=false`, 3xx responses return `error.type="http_redirect"` instead of the generic `http_status`; if a `Location` header is present, the error includes both `location` and an absolute `redirect_url`.

Response headers are limited to operational headers such as content type, content length, date, server, etag, location, retry-after, and rate-limit headers.

**Example:**

```json
{
  "method": "POST",
  "url": "https://api.example.com/v1/items",
  "headers": {"Authorization": "Bearer ..."},
  "query": {"source": "nymeria"},
  "body": {"name": "Test item"}
}
```

### api_discover

Probe an API base URL for OpenAPI/Swagger metadata and summarize the spec.

```python
api_discover(
    base_url: str,
    docs_url: Optional[str] = None,
    timeout_seconds: int = 20,
    max_response_chars: int = 50000,
)
```

**Discovery order:** optional `docs_url`, then common paths including `/openapi.json`, `/openapi.yaml`, `/swagger.json`, `/swagger.yaml`, `/api-docs`, `/v3/api-docs`, `/docs/openapi.json`, and `/.well-known/openapi.json`. HTML docs are scanned for direct OpenAPI/Swagger/API-doc links and Swagger UI config scripts such as `swagger-initializer.js`; those scripts are inspected for spec URLs before falling back to generic hints.

**Returns:** JSON with `tool_version`, `found`, `spec_url`, `spec_format`, a summary containing title/version/servers/path count/sample methods/security scheme names, every tried URL, and hints when no spec is found. Discovery requests use the same HTTP egress policy as `http_request`.

**Relationship to custom HTTP tools:** `http_request` is the ad hoc primitive for one-off API calls. Custom HTTP tools remain the reusable connector mechanism: they store parameterized URL/header/body templates in `data/custom_tools/` and expose a named tool after setup. Custom HTTP tools use the same egress policy and audit redaction as `http_request`.

### tool_create

Draft, test, and publish reusable HTTP tools from inside an agent conversation.

```python
tool_create(
    action: str,
    tool_id: str = "",
    name: str = "",
    description: str = "",
    parameters: Optional[dict] = None,
    http_config: Optional[dict] = None,
    draft_id: str = "",
    sample_params: Optional[dict] = None,
    ttl: str = "2h",
)
```

**Actions:**
- `draft` — Save or update a per-user draft in `data/tool_drafts/{user_id}/`. Requires `tool_id`, `description`, `parameters`, and `http_config`.
- `test` — Execute a saved draft with `sample_params` and record whether the request succeeded.
- `publish` — Save a successfully tested draft into the global `data/custom_tools/` registry, reload custom tools, and enable the new tool on the current thread.
- `list` — Show this user's drafts plus globally published custom tools without exposing request headers or bodies.
- `delete` — Delete this user's draft only. It does not delete a globally published tool.

**Publish semantics:** Published tools are global registry entries, so any user can discover and enable them later. They are not added to `default_thread_tools` and are not enabled by default for other users or threads. The publishing thread gets the new tool enabled with a TTL (`30m`, `2h`, `6h`, `24h`, or `permanent`; default `2h`) using the same in-turn auto-reload path as `tool_enable(action="enable")`, but reload metadata uses `source="tool_create"` and `reason="tool_published"`.

**V1 limits:** Only `implementation_type="http"` is supported. Agent-created tools reject inline secrets and `${env:...}` references. Sensitive headers are allowed only when their value uses a credential-vault reference like `${credential:cred_id.value}`.

**Credential vault auth:** Authenticated custom HTTP tools should use `${credential:<credential_id>.<field>}` references in headers, query params, URLs, or bodies instead of raw values. Runtime execution resolves the reference server-side, checks the credential's allowed target, and audits the use without returning secret material to the agent.

**Audit and deferred production safety:** HTTP tool calls append redacted HTTP events to the audit log when `AUDIT_LOG_ENABLED=true`. Raw bearer/API-key-like values are best-effort redacted and custom HTTP `${env:VAR}` usage records the variable names, not the values. Credential-vault usage records credential IDs, not plaintext values. Future hardening still needs stronger per-user rate-limit budgets per task, pagination helpers, and policy hooks for actions that send messages, delete data, spend money, modify production systems, post publicly, or change infrastructure.

### auth_manager

Agent-safe credential management facade. Optional tool, disabled by default.

```python
auth_manager(
    action: str,
    credential_id: str = "",
    provider: str = "",
    kind: str = "api_key",
    name: str = "",
    target_type: str = "",
    target_id: str = "",
    binding_name: str = "",
    binding_id: str = "",
    metadata: Optional[dict] = None,
    required_fields: Optional[list[str]] = None,
)
```

Actions: `list`, `status`, `request_setup`, `bind`, `unbind`, `test`, `disable`.
The tool returns credential metadata only. It can manage user-owned credentials
but cannot alter system credentials. It never returns plaintext secrets,
ciphertext, or partial key material.

### skill_config

Draft, validate, publish, list, and delete Nymeria Skills and Skill Kits from inside an agent conversation.

```python
skill_config(
    action: str,
    name: str = "",
    description: str = "",
    body: str = "",
    allowed_tools: Optional[list[str] | str] = None,
    required_tools: Optional[list[str] | str] = None,
    tool_ttl: str = "2h",
    draft_id: str = "",
    scope: str = "user",
    overwrite: bool = False,
    activate_current_thread: bool = True,
)
```

**Actions:**
- `draft` — Validate and save a per-user draft in `data/skill_drafts/{user_id}/`.
- `validate` — Validate inline fields or a saved draft without publishing.
- `publish` — Write a validated `SKILL.md` to `data/skills/users/{user_id}/` or admin-only `data/skills/global/`, reload skills, and by default enable it on the current thread.
- `list` — Show this user's drafts plus installed skills visible to the user.
- `delete` — With `scope="draft"`, delete a draft. With `scope="user"` or `scope="global"`, uninstall that skill scope; global delete requires admin.

**V1 limits:** `skill_config` writes only `SKILL.md`. It cannot create scripts, assets, references, or arbitrary paths. It rejects body text that includes YAML frontmatter; agents pass `name`, `description`, `allowed_tools`, `required_tools`, and `tool_ttl` as structured parameters.

**Skill Kit dependency checks:** `required_tools` are validated before any publish write. Unknown, unloadable, or admin-blocked tools fail strictly. Publishing a user skill that would shadow an existing bundled/global skill is rejected; replacing an existing generated skill requires `overwrite=true` and the existing directory must contain only `SKILL.md`.

**Same-turn activation:** When `activate_current_thread=true`, publish adds the skill name to `ThreadConfig.enabled_skills`, reloads the skill manager, invalidates graph caches, and queues a same-turn `tool_reload` with `source="skill_config"` and `reason="skill_published"`. The event may have an empty `tools` list because the reload refreshes the `Skill` meta-tool index rather than binding a new normal tool.

### skill_kit_create

Preferred facade for durable capability authoring from `self-improve`.

```python
skill_kit_create(
    action: str,
    name: str = "",
    description: str = "",
    body: str = "",
    required_tools: Optional[list[str] | str] = None,
    draft_id: str = "",
    tool_id: str = "",
    parameters: Optional[dict] = None,
    http_config: Optional[dict] = None,
    sample_params: Optional[dict] = None,
)
```

Actions:
- `draft`, `validate`, `publish`, `package`, `list` — Skill Kit lifecycle.
- `draft_http_tool`, `test_http_tool`, `publish_http_tool` — guided HTTP tool
  creation before packaging a Skill Kit around it.

`publish`/`package` use the Skill publish reload path with
`source="skill_kit_create"` and `reason="skill_kit_created"`. Publishing an
HTTP tool through this facade enables the new tool on the current thread with
`source="skill_kit_create"` and `reason="http_tool_published_for_skill_kit"`.

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
- **Rate limit**: 10 spawns per parent per hour (`NYMERIA_MAX_SPAWNS_PER_HOUR` env). Process-local (resets on API restart); this is intentional for single-process deployments since restart breaks any active spawn loop and the depth limit is the hard guard against recursion.
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

Create a callable thread: open thread settings → Agent → check "Make Callable" → set a name and description. On desktop, the thread row's Agent shortcut opens this tab directly. The callable becomes available as a tool to **threads owned by the same user** after `sync_agent_tools()` runs. Other users do not see the callable in their tool list, and the runtime ownership gate rejects any invocation attempt by a non-owner. Admins can act-as the owning user via `X-Nymeria-Act-As` to test or trigger another user's callable.

The tool signature for any callable thread is:

```python
ThreadName(
    task: str,
    mode: Literal["ask", "handoff"] = "ask",
    scheduled_for: Optional[str] = None,
    if_busy: Literal["queue", "error"] = "queue",
) -> str
```

The `task` parameter is the instruction. `mode="ask"` preserves the legacy behavior: the caller waits and receives the target thread's final response. `mode="handoff"` starts an autonomous run in the target thread and returns `[HandedOff]` metadata (`handoff_id`, target thread, and optional TODO id) without returning the target's final output.

For handoffs, `scheduled_for` can delay execution with values such as `"30s"`, `"5m"`, `"1h"`, `"1d"`, or `"YYYY-MM-DD HH:MM"`; delayed handoffs are stored as scheduled TODOs on the target thread. Omit `scheduled_for` for immediate handoff. For immediate handoffs, `if_busy="queue"` lets the target thread wait for its lock in the background, while `if_busy="error"` returns a busy response if the target is already running. In blocking ask mode, `if_busy="error"` performs a best-effort busy check before waiting.

Handoff prompts include source-thread metadata so the target thread can call the original callable thread later if useful. There is no automatic completion callback. If the target thread is bound to Telegram and `telegram_autonomous_delivery="full"`, its handoff output is delivered through Telegram like other autonomous output.

An injected `config` parameter provides user/thread context. Callable threads are created dynamically by `agents/tool_factory.py` via `create_callable_thread_tool()`, which wraps `thread_agent_executor` in a LangChain `BaseTool`. The factory handles circular call detection for blocking asks; non-blocking handoffs intentionally skip the wait graph so a child can hand work back to its caller. The factory also performs a runtime team-visibility check so stale cached graphs cannot invoke callables outside the caller's team.

### Browser Tools (9)

Used internally by BrowserAgent. Defined in `tools/browser.py`.

| Tool | Signature | Description |
|------|-----------|-------------|
| `browser_navigate` | `(url: str)` | Navigate to an absolute `http://` or `https://` URL. Falls back to requests+BeautifulSoup if Playwright unavailable. |
| `browser_click` | `(selector: str)` | Click element by CSS selector or `text=` selector. |
| `browser_type` | `(selector: str, text: str)` | Type text into an input field. |
| `browser_get_content` | `(include_links: bool = True)` | Get page text content and optionally links. |
| `browser_screenshot` | `()` | Take a screenshot. Returns a data URI preview string (first 100 chars of base64 + total length). |
| `browser_scroll` | `(direction: str = "down", amount: int = 500)` | Scroll page up or down by pixel amount. |
| `browser_press_key` | `(key: str)` | Press a keyboard key (e.g., `"Enter"`, `"Tab"`). |
| `browser_close` | `()` | Close the browser and reset the thread. |
| `browser_status` | `()` | Check Playwright availability and browser state. |

**Architecture:** All browser operations run on a dedicated `BrowserThread` to satisfy Playwright's single-thread requirement. Operations are queued and results retrieved via thread-safe queues. The browser persists between calls until explicitly closed.

**Fallback mode:** Set `BROWSER_FORCE_FALLBACK=true` in `.env` to skip Playwright entirely and use requests+BeautifulSoup for navigation and content extraction. Fallback HTTP requests verify TLS certificates by default; set `BROWSER_VERIFY_SSL=false` only in trusted environments with known TLS interception.

---

### Outlook Tools (18)

Used internally by OutlookAgent. Also available as **optional tools** for per-thread enabling. Defined in `tools/outlook_auth.py` (4 auth), `tools/outlook_email.py` (13 email), and `tools/outlook_attachments.py` (1 attachment).
Microsoft tokens are stored with the shared cache I/O helpers in
`tools/auth_cache_utils.py` at `data/auth_tokens/<user_id>/microsoft.json`.

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
| `outlook_get_attachments` | `(email_id, skip?, account_id?)` | Download and extract text from all email attachments. CSV/TXT decoded directly, Excel via openpyxl, PDF/DOCX/images via Gemini AI. Optional `skip` to ignore irrelevant attachments by name. |

**Folder names** (case-insensitive):
- `outlook_list_emails` accepts: `inbox`, `sent`/`sentitems`, `drafts`, `deleted`/`deleteditems`, `junk`/`junkemail`, `archive`
- `outlook_search_emails` accepts same folders, or omit for all mail
- `outlook_move_email` additionally accepts: `trash` (→ deleteditems), `spam` (→ junkemail)

---

### Gmail Auth Tools (4)

Optional Google Gmail OAuth tools for MCP auth bridging. They use the same
Google OAuth factory as Calendar and Docs, but request Gmail scopes and store
tokens at `data/auth_tokens/<user_id>/google_gmail.json`.

After `gmail_auth_complete` succeeds, Nymeria exports a google-auth-library
compatible token file to `data/auth_tokens/<user_id>/mcp/gmail/credentials.json`.
Managed MCP install/retry/create automatically applies this file as
`GMAIL_CREDENTIALS_PATH` for `@gongrzhe/server-gmail-autoauth-mcp` and applies
`GOOGLE_OAUTH_CREDENTIALS` as `GMAIL_OAUTH_PATH`. Existing Calendar or Docs
Google tokens are exported only if they already include the Gmail scopes; most
older tokens will require `gmail_auth_start` because Google scopes are fixed at
consent time.

| Tool | Signature | Description |
|------|-----------|-------------|
| `gmail_auth_start` | `()` | Start Google Gmail OAuth flow. Returns authorization URL for the user. |
| `gmail_auth_complete` | `(redirect_url?)` | Complete auth after browser sign-in and export MCP credentials. |
| `gmail_auth_clear` | `(account_id?)` | Clear one saved Gmail account by ID, or all Gmail accounts plus any pending Gmail OAuth flow when omitted. |
| `gmail_list_accounts` | `()` | List authenticated Google accounts for Gmail with verified token/scopes status. Invalid refresh tokens are pruned. |

---

### Calendar Tools (15)

Native Python Google Calendar API client. Defined in `tools/calendar_auth.py` (4 auth) and `tools/calendar.py` (11 event). Uses `google-api-python-client` for direct API calls with agent-guided OAuth flow.
Calendar auth tools are generated from the shared Google OAuth factory in
`tools/auth_cache_utils.py`; Calendar API calls use the same module's
credential refresh and request wrapper.

**Authentication tools:**

| Tool | Signature | Description |
|------|-----------|-------------|
| `calendar_auth_start` | `()` | Start Google OAuth flow. Returns authorization URL for the user. |
| `calendar_auth_complete` | `(redirect_url?)` | Complete auth after browser sign-in. Accepts optional redirect URL for manual fallback. |
| `calendar_auth_clear` | `(account_id?)` | Clear one saved Google Calendar account by ID, or all Calendar accounts plus any pending Calendar OAuth flow when omitted. |
| `calendar_list_authenticated_accounts` | `()` | List authenticated Google accounts with verified token/scopes status. Invalid refresh tokens are pruned. |

**Event tools:**

| Tool | Signature | Description |
|------|-----------|-------------|
| `calendar_list_calendars` | `(account_id?)` | List all available Google calendars. |
| `calendar_list_events` | `(calendar_id="primary", max_results=10, time_min?, time_max?, account_id?)` | List events from a calendar. Output includes full event IDs for chaining into `calendar_get_event`. |
| `calendar_get_event` | `(event_id, calendar_id="primary", account_id?)` | Get full event details. |
| `calendar_search_events` | `(query, calendar_id="primary", max_results=10, account_id?)` | Search events by text. Output includes full event IDs for chaining into `calendar_get_event`. |
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

The Google Docs, Drive, and Sheets tools share one OAuth cache at `data/auth_tokens/<user_id>/google_docs.json`. Account-list output verifies scopes and refreshability before presenting an account as usable.
The auth tools are generated by the same `tools/auth_cache_utils.py` Google
OAuth factory used by Calendar. Docs and Drive API calls share its credential
refresh and request wrapper, and generic Sheets uses the Docs/Drive/Sheets
credential cache.

| Tool | Signature | Description |
|------|-----------|-------------|
| `google_docs_auth_start` | `()` | Start Google Docs/Drive/Sheets OAuth flow. Returns authorization URL for the user. |
| `google_docs_auth_complete` | `(redirect_url?)` | Complete auth after browser sign-in. Accepts optional redirect URL for manual fallback. |
| `google_docs_auth_clear` | `(account_id?)` | Clear one saved Google Docs account by ID, or all Docs/Drive/Sheets accounts plus any pending OAuth flow when omitted. |
| `google_docs_list_accounts` | `()` | List authenticated Google accounts for Docs/Drive/Sheets with verified token/scopes status. Invalid refresh tokens are pruned. |

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
- Trigger tools: 2
- Browser tools: 9
- Calendar tools: 4 auth + 11 event = 15 total
- Self-modify tools: 8
- Subagent tools (reload/rollback): 2
- Google Docs tools: 4 auth + 17 document = 21 total
- Google Sheets / _PRV_A tools: 3 base + 5 _PRV_A = 8 total
- Twitch tools: 22
- Watchdog tools: `activity_feed`, `watchdog_dispatch`, `watchdog_read_notepad`, `watchdog_todo_overview` = 4
- Utility tools: `claude_code`, `sticky_note`, `tool_search`, `tool_enable`, `manage_mcp`, `skill_manage`, `http_request`, `api_discover`, `tool_create`, `skill_config`, `skill_kit_create` plus the admin-only diagnostic `hello_test` used for dynamic-load validation

**How it works:**
1. `OPTIONAL_TOOLS` in `tools/__init__.py` maps tool names to tool objects
2. Per-thread config has an `enabled_tools` list (tool names)
3. The profile-level `default_thread_tools` list is the default-bound core set for each thread; an empty list means no core tools
4. During `_build_graph_with_prompt()`, enabled optional tools are added to the thread's tool set
5. Users enable or disable optional tools via thread settings or `PATCH /threads/{id}/config`

The desktop/mobile Thread Settings UI mirrors this split: the Tools tab shows non-MCP tools from `default_thread_tools` plus non-MCP optional tools, while the MCP tab shows MCP-discovered tools. Default MCP tools can be disabled per thread; non-default MCP tools can be enabled per thread. Tool discovery is role-filtered; `hello_test` remains in `OPTIONAL_TOOLS` for admin/test validation but is hidden from non-admin search/listing surfaces and rejected by non-admin enable paths.

**Important:** `OPTIONAL_TOOLS` currently includes more than just integrations. It also contains tools like `claude_code`, `sticky_note`, `reload_all`, and `self_modify_rollback`.

---

## Custom Tools

Custom tools extend Nymeria's capabilities without writing Python. Created via the **Desktop UI** (Settings → Tools), the **REST API**, or the agent-facing `tool_create` workflow for public unauthenticated HTTP tools.

### Tool Types

| Type | Description | Use Case |
|------|-------------|----------|
| **HTTP** | Makes REST API calls to external services | Integrate with APIs, webhooks, web services |
| **MCP** | Connects to Model Context Protocol servers | Use existing MCP tools, complex integrations |

### HTTP Tools

HTTP tools make REST API calls with configurable:
- **Method**: GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS
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

MCP tools connect to external MCP servers via JSON-RPC over stdio or HTTP.

The MCP paste installer accepts Claude Desktop JSON, bare stdio commands, HTTP/SSE URLs, npm package pages, PyPI package pages, Git repository URLs, registry ids, bundle URLs, and uploaded `.mcpb`/`.dxt`/`.zip` bundles. The desktop installer previews the plan before running anything, asks for confirmation before Git/local-path/bundle installs, and saves failed installs as disabled drafts with logs so they can be retried. Sensitive pasted config values are encrypted with `NYMERIA_SECRETS_KEY`.

Managed MCP servers and their discovered tools are configured from the dedicated **Settings → MCP** tab on desktop and mobile. Legacy user-created MCP custom tools remain under **Settings → Tools** with the other custom tools.

Known MCP auth presets are applied during install, retry, create, and update.
For `@gongrzhe/server-gmail-autoauth-mcp`, Nymeria wires
`GMAIL_OAUTH_PATH` from `GOOGLE_OAUTH_CREDENTIALS` and
`GMAIL_CREDENTIALS_PATH` from the current user's Gmail MCP credential export.

For stdio servers, Nymeria launches the command from the backend process. In Docker, that means paths and Python/Node dependencies must exist inside the `nymeria-api` container, and host services should normally be referenced as `host.docker.internal:<port>` rather than `localhost:<port>`. Startup and discovery failures include the server process's recent stderr when available.

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
- Multiple tools can share the same MCP server through the process-wide `MCPServerManager`
- Managed MCP server tools and legacy custom MCP tools use the same shared manager and connection pool
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
GET /tools/custom/export       # Export custom tools
POST /tools/custom/import      # Import custom tools
PUT /tools/custom/{tool_id}    # Update a tool
DELETE /tools/custom/{tool_id} # Delete a tool
POST /tools/custom/{tool_id}/test  # Test a tool
```

**Storage:** Custom tools are stored as JSON files in `data/custom_tools/`, one `.json` per tool ID.

**Agent-created tools:** `tool_create(action="publish")` writes the same JSON definition format into `data/custom_tools/`, then reloads the custom-tool loader and registers metadata so `tool_search(query=...)` can find the new tool. Agent-created tools are global but remain opt-in per thread.

### HexStrike MCP Sidecar

HexStrike AI is available as an optional Kali-based sidecar through
`docker-compose.hexstrike.yml`. It exposes a curated subset of upstream
HexStrike tools over Streamable HTTP at `http://hexstrike-mcp:8889/mcp` for
Nymeria containers and `http://localhost:8889/mcp` for local MCP clients. See
`docs/hexstrike-mcp.md` for build, registration, and allowlist details.

---

## Scheduled TODO Execution

Nymeria operates autonomously 24/7 through **scheduled TODOs** — TODOs with a `scheduled_for` datetime that are automatically executed when due.

### How It Works

1. `nym_todo(task=..., scheduled_for=...)` creates a TODO and registers it in `TodoScheduleDB` (SQLite at `data/todo_schedule.db`).
2. The **Ticker** daemon polls every 5 seconds for due TODOs.
3. When a TODO is due, the ticker sends its `task` text as a prompt to the agent on the TODO's `thread_id`.
4. For recurring TODOs, **any completion** (ticker execution, agent marking done, API, or MCP) auto-reschedules to the next `scheduled_for` based on the recurrence pattern. Use `clear_recurrence` or `nym_todo_delete` to stop.
5. Completed TODOs are removed from the active TODO JSON list after `TODO_AUTO_ARCHIVE_DAYS` (default 7) by hourly cleanup in the ticker. They are not moved to a separate archive file.

**Durable scheduling:** Scheduled TODOs survive application restarts. Missed TODOs are recovered and executed on startup.

**Concurrency:** Scheduled TODOs and trigger actions run through the ticker's autonomous worker pool (`MAX_CONCURRENT_AUTONOMOUS`, default 5). Poll-based trigger source checks and hourly TODO archival run in a separate housekeeping pool so slow source I/O does not consume autonomous workers.

---

## Security Levels & Metadata

Tool metadata is generated in `tools/metadata.py` from the registered LangChain
tool objects. Descriptions come from the tool docstrings; `metadata.py` owns the
policy fields that cannot be inferred from a docstring: category, security
level, default-enabled state, and config schemas.

### Security Levels

| Level | Description |
|-------|-------------|
| **SAFE** | Read-only or low-risk behavior |
| **MODERATE** | Mutates external/local state or performs broader actions |
| **SENSITIVE** | Code/runtime mutation or similarly high-risk behavior |

Default availability is separate from security level: tools in `ALL_TOOLS` are
enabled for new threads by default, and tools in `OPTIONAL_TOOLS` are opt-in by
default even when they are classified `SAFE`.

### Tools by Security Level

Representative examples:

**SAFE:** `file_read`, `web_search`, `consult`, `memory_add`, `memory_edit`, `memory_read`, `personality_set`, `rag_search`, `nym_todo`, `nym_todo_delete`, `nym_todo_list`

**MODERATE:** `bash_execute`, `file_write`, `file_edit`, `claude_code`, `notify`, `http_request`, `api_discover`, `tool_create`, many trigger/email/calendar/browser actions

**SENSITIVE:** self-modify file mutation and rollback tools

For the precise current registry, call `get_all_tool_metadata()` or check
`nymeria/tools/metadata.py` for the generated metadata policy.

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
Metadata is generated automatically from the registered tool object, so adding a
tool to `ALL_TOOLS` or `OPTIONAL_TOOLS` is enough to get a metadata entry.
Use clear docstrings: the first paragraph becomes the discovery description.

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
