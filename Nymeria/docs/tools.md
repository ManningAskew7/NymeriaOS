# Nymeria Tools Reference

Nymeria has a three-tier tool system: **core tools** always loaded, **dynamic callable thread tools** (one per callable thread), and a large set of **optional tools** available for per-thread enabling. The code-owned registry has 19 core tools in `ALL_TOOLS` and roughly 1,260 optional tools in `OPTIONAL_TOOLS` (the count drifts as integrations land; regenerate `tools-index.md` for the live total).

## Summary Table

### Core Tools

| # | Tool | Category | Security | Default | Description |
|---|------|----------|----------|---------|-------------|
| 1 | `bash_execute` | Core | MODERATE | On | Execute shell commands in the backend environment |
| 2 | `file_read` | Core | SAFE | On | Read file contents |
| 3 | `file_write` | Core | MODERATE | On | Write content to files; optional workspace confinement is available |
| 4 | `web_search_perplexity` | Web Search | SAFE | Opt-in | Search the web via Perplexity (Sonar); opt-in `WEB_SEARCH_SERVICE_TOOLS` group |
| 4b | `web_search_tavily` | Web Search | SAFE | Opt-in | Ranked-source web retrieval via Tavily; opt-in `WEB_SEARCH_INTEGRATION_TOOLS` group |
| 4c | `web_search_exa_ai` | Web Search | SAFE | Opt-in | Neural/semantic web retrieval via Exa (highlights); opt-in `WEB_SEARCH_INTEGRATION_TOOLS` group |
| 4d | `web_search_firecrawl` | Web Search | SAFE | Opt-in | Ranked-source web search via Firecrawl (snippet-only); opt-in `WEB_SEARCH_INTEGRATION_TOOLS` group |
| 4e | `web_search_brave` | Web Search | SAFE | Opt-in | Ranked-source web search via Brave's independent index; opt-in `WEB_SEARCH_INTEGRATION_TOOLS` group |
| 4f | `web_search_searxng` | Web Search | SAFE | Opt-in | Keyless metasearch via a self-hosted SearXNG instance; opt-in `WEB_SEARCH_INTEGRATION_TOOLS` group |
| 4g | `fetch_url_nymeria` | Web Search | SAFE | Opt-in | Free, SSRF-gated page fetch + readable extraction (markdown/PDF), optional distill; opt-in `WEB_FETCH_TOOLS` group |
| 5 | `consult` | Core | SAFE | On | Ask Gemini for a second opinion (OpenRouter) |
| 6 | `memory_add` | Profile | SAFE | On | Save a memory. `scope="global"` (keyed user-profile fact) or `scope="thread"` (per-thread notepad). Empty content deletes. |
| 7 | `memory_edit` | Profile | SAFE | On | Surgical find/replace within an existing memory. Empty `replace` deletes the matched text. |
| 8 | `memory_read` | Profile | SAFE | On | Get one keyed memory, list all, or substring-filter via `query`. |
| 9 | `personality_set` | Profile | SAFE | On | Set communication preferences |
| 10 | `rag_search` | Profile | SAFE | On | Hybrid memory search (RRF over vector + BM25, optional date-anchor bias) with time + thread provenance |
| 11 | `nym_todo` | TODO | SAFE | On | Create or update a TODO  -  scheduled TODOs auto-wake the agent |
| 12 | `nym_todo_delete` | TODO | SAFE | On | Delete a TODO permanently |
| 13 | `nym_todo_list` | TODO | SAFE | On | List TODO items |
| 14 | `notify` | Core | MODERATE | On | Send in-app and external notifications |
| 15 | `slash_command` | Self | MODERATE | On | Run registered Nymeria slash commands on the current thread. Destructive commands are blocked for the agent. |
| 16 | `auth_inspect` | Credentials | SAFE | On | Inspect credential metadata and OAuth account state without exposing secrets |
| 17 | `auth_cleanup` | Credentials | MODERATE | On | Disable stale or unwanted user-owned credentials, dry-run by default for bulk cleanup |
| 18 | `auth_bindings` | Credentials | MODERATE | On | Bind or unbind credentials to runtime targets |
| 19 | `request_credential` | Credentials | SAFE | On | Ask the user to provide a missing credential through a frontend prompt |

> **Skill meta-tool:** A single `Skill(name)` tool is synthesized per-thread at graph-build time when any skills are active  -  it's not in `ALL_TOOLS`. Its description carries an `<available_skills>` index of `(name, description)` pairs; calling it returns that skill's full SKILL.md body. Skill Kits can additionally declare `metadata.nymeria.required_tools`; activation strictly binds those tools with a TTL before resuming the same turn. Users can activate non-internal markdown skills with `/skill <name> [prompt]` and Skill Kits with `/kit <name> [ttl] [prompt]`. See `docs/skills.md`.

> **Capability expansion:** Tool discovery/enabling, MCP management, skill management, API probing, and Skill/Skill Kit authoring are no longer default tools. The bundled `self-improve` skill is a text-only guidance skill (it binds no tools); it holds the capability-expansion operating philosophy plus a router to four focused, default-on Skill Kits that each bind their tools only when activated: `tool-management` (`tool_search`, `tool_manage`, `tool_create`, `api_discover`, `http_request`), `skill-management` (`skill_manage`, `skill_write`, `skill_edit`), `mcp-management` (`manage_mcp`), and `credential-management` (`auth_inspect`, `auth_cleanup`, `auth_bindings`, `request_credential`, which are core/always-on tools, so this kit is mainly guidance).

> **Note:** `bash_execute` is a core tool for personal-assistant effectiveness and relies on the backend deployment boundary for sandboxing. `claude_code`, `reload_all`, and `self_modify_rollback` live in `OPTIONAL_TOOLS` and are admin-only optional tools. See `nymeria/tools/__init__.py` for the canonical lists.

> **Tool output guard:** After any tool executes, Nymeria truncates oversized `ToolMessage` content before it is stored in thread history. `TOOL_OUTPUT_MAX_CHARS` defaults to `100000`; larger outputs keep the first ~75k and last ~25k characters with a marker showing the original and omitted sizes.

> **CLIProxy OAuth note:** Installed server tools keep the safe dynamic namespace `mcp__<server>__<tool>`. Nymeria-owned helper tools must avoid the `mcp_<name>`, `mcp.<name>`, and `mcp/<name>` namespaces because Claude OAuth classifies those as third-party MCP apps. The consolidated facade is named `manage_mcp`; legacy helpers remain `search_mcp` and `install_mcp_server` for compatibility.

### Optional: Trigger Tools (2)

Not loaded by default. Enable per-thread via thread config, or use through SelfModifyAgent.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `trigger_config` | Trigger | MODERATE | Create, update, enable/disable, or delete event triggers |
| 2 | `trigger_info` | Trigger | SAFE | List triggers, inspect/test/history for one trigger, or show source schemas |

### Optional: Capability Expansion and Authoring

Not loaded by default. The normal path is to activate the matching focused Skill
Kit, which binds its facades below with a TTL: `tool-management` for
`tool_search`/`tool_manage`/`tool_create`/`api_discover`/`http_request`,
`skill-management` for `skill_manage`/`skill_write`/`skill_edit`, and
`mcp-management` for `manage_mcp`. `Skill(name="self-improve")` itself binds no
tools; it provides the operating philosophy and routes to these kits.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `tool_search` | Core | SAFE | Search available tools |
| 2 | `tool_manage` | Core | MODERATE | Enable, disable, prune, and inspect current-thread tool bindings |
| 3 | `manage_mcp` | MCP | MODERATE | Search, preview, install, and inspect MCP servers |
| 4 | `skill_manage` | Skills | MODERATE | List, search, install, enable, disable, prune, and inspect skills |
| 5 | `http_request` | Core | MODERATE | Make a one-off HTTP request to a documented API endpoint |
| 6 | `api_discover` | Core | MODERATE | Discover OpenAPI/Swagger metadata for an API base URL |
| 7 | `tool_create` | Custom | MODERATE | Draft, test, publish, hot-load, and enable reusable HTTP or Python tools |
| 8 | `skill_write` | Custom | MODERATE | Write a full SKILL.md package with optional scripts and required tools |
| 9 | `skill_edit` | Custom | MODERATE | Edit an existing SKILL.md body/frontmatter/tool bindings |
| 10 | `search_mcp` / `install_mcp_server` | MCP | SAFE/MODERATE | Compatibility low-level MCP helpers |
| 11 | `list_installed_skills` / `search_skills` / `install_skill` | Skills | SAFE/MODERATE | Compatibility low-level skill helpers |

### Optional: Integration And Service Tools

Not loaded by default. Nymeria registers more than a thousand optional integration and service tools across utility, public-info, media, community, HR, device, marketing, developer, build/CI, file storage, AWS, business, productivity, work-tracking, project-management, data, CRM, messaging, commerce, monitoring, enrichment, chat-platform, Google, and Microsoft Graph modules. The exhaustive generated inventory lives in `tools-index.md`; this page keeps only the architectural notes and hand-maintained special cases so the reference does not drift.

### Optional: File Editing (1)

Not loaded by default. Enable per-thread when the agent needs precise text edits instead of full-file rewrites.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `file_edit` | Core | MODERATE | Exact, all-or-nothing edits to existing text files |

### Optional: Image Generation (5)

One opt-in tool per provider (the `image_gen_*` suite), so users can search for and swap between providers individually. Not loaded by default. Enable per-thread, or promote to Core in the Desktop global Tools settings. Each tool writes generated images under `NYMERIA_WORKSPACE_DIR/image-generation/`, returns a workspace artifact via `[attach:/path]`, and stores only small artifact metadata in chat history. On the next reasoning step, Nymeria hydrates recent generated images into native vision input for supported chat providers: Anthropic vision models and OpenAI/OpenRouter models using `OPENAI_API_MODE=responses`. Other provider modes still see the file path and artifact. Native-vision replay is skipped for images over 8 MB or outside png/jpeg/webp/gif (large 4K outputs still attach to the chat).

API keys resolve credential vault first, then settings, then env (the same pattern as the `web_search_*` suite): `OPENAI_API_KEY`, `GEMINI_API_KEY`, `BFL_API_KEY`, `REPLICATE_API_KEY` (or `REPLICATE_API_TOKEN`), and `FAL_API_KEY` (or `FAL_KEY`), or a vault credential bound to `native_tool:image_gen_<provider>`. Note: when `OPENAI_API_KEY` is a CLIProxy gatekeeper value (`cpx-*`), supply a genuine OpenAI key through the vault for `image_gen_openai`.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `image_gen_openai` | Image | MODERATE | Flagship OpenAI GPT Image (`gpt-image-2`); best all-round prompt adherence and photorealism. Synchronous |
| 2 | `image_gen_gemini` | Image | MODERATE | Google Gemini Nano Banana Pro (`gemini-3-pro-image`); best in-image text, infographics, and 4K. Synchronous |
| 3 | `image_gen_flux` | Image | MODERATE | Black Forest Labs FLUX.2 flagship photorealism and multi-reference editing. Async submit + poll |
| 4 | `image_gen_replicate` | Image | MODERATE | Budget host (FLUX.1 schnell or Z-Image Turbo) via Replicate, ~10-20x cheaper. Uses `Prefer: wait` + poll fallback |
| 5 | `image_gen_fal` | Image | MODERATE | Budget host (Z-Image Turbo or FLUX.1 schnell) via fal.ai, fast and cheap. Synchronous endpoint |

Generation options are per-call tool arguments, not stored config: every tool takes `prompt` and `output_name`; `image_gen_openai` adds `size`/`quality`/`output_format`/`model`; `image_gen_gemini` adds `aspect_ratio`/`image_size`/`model`; `image_gen_flux`/`image_gen_replicate` add `aspect_ratio`/`model`/`seed`; `image_gen_fal` adds `image_size`/`model`/`seed`. The aggregator tools' `model` argument selects the hosted model (`flux-schnell` or `z-image-turbo`).

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
| 1 | `spawn_thread` | Subagent | MODERATE | Create or delete a sidebar thread with custom instructions, tool selection, optional TTL, and LLM overrides. New threads are callable (globally invocable) by default. Create mode optionally dispatches an initial message and blocks until the child responds; delete mode cleans up a previously-spawned thread. |

#### `spawn_thread` ergonomic params

Four knobs sit on top of the raw thread config to make spawning fast and intentful:

- `tool_queries: List[str]`: free-text intents (e.g. `["research", "browser automation"]`) resolved via the semantic tool search index. The top matches for each query are merged into the child's enabled tools. Faster than enumerating exact tool names; mix freely with `optional_tools` and `tool_categories`.
- `tool_query_top_k: int = 8`: max matches kept per query before deduping across queries.
- `include_core_tools: bool = True`: when `False`, the child skips `ALL_TOOLS` and gets only the explicitly resolved/selected set. Useful for focused sub-agents that should not have memory writes, sub-spawning, file IO, or other broad capabilities.
- `ttl_hours: Optional[int] = None`: single TTL knob. `None` is a permanent thread; any positive integer marks the thread temporary so the worker ticker auto-deletes it after that many hours without activity. Activity is refreshed on each turn and each callable invocation.

`instructions` (system-prompt text appended to soul.md) and `prompt` (initial message dispatched to the child and blocked on) are unchanged. The return preamble includes a `[Resolved tools]: name (score <- "query"), ...` line when `tool_queries` was used so the parent can verify what was actually attached.

Example: spin a short-lived focused researcher and have it return a single answer.

```python
spawn_thread(
    title="Web researcher",
    tool_queries=["web search", "browser navigation"],
    ttl_hours=4,
    include_core_tools=False,
    prompt="Find the latest pricing for Anthropic API tiers.",
)
```

### Callable Thread Tools (Dynamic)

Any thread with `callable=True` in its thread config becomes a tool that other threads can invoke. There are no hardcoded agents  -  callable threads are fully configurable via the UI:

- **Model**: Set per-thread via `llm_config.model` in thread settings (inherits global default if not set)
- **Tools**: Enable/disable any optional tools per-thread
- **System prompt**: Custom `system_prompt` or `instructions` per-thread
- **Name**: The tool name equals the thread's sidebar title (synced via `callable_name` in thread config)

Create a callable thread: open thread settings → Agent → check "Make Callable" → set a name and description. On desktop, the thread row's Agent shortcut opens this tab directly. The thread becomes available as a tool to **the creator's own threads** after `sync_agent_tools()` runs  -  callables are scoped to their owner (the user who created them) and the `_thread_owners` table determines visibility. Two users can independently create callables with the same `callable_name`; each user's graph binds their own version, and the runtime ownership gate in `agents/tool_factory.py` blocks cross-user invocation, including admin-as-admin. Admins must use `X-Nymeria-Act-As` for the callable owner when testing or triggering another user's callable.

Callable tools default to blocking `mode="ask"`, which returns the target thread's final answer. Use `mode="handoff"` to transfer work to the target thread without waiting; the caller receives only a dispatch receipt while the target thread streams through its normal autonomous output channels.

Callable teams can scope which callable threads a thread sees. When a thread has `callable_team_id`, graph building includes only the owner's callable threads with the same team id. Unteamed threads keep the existing owner-wide callable visibility for backward compatibility. The desktop sidebar has a folder/team organization toggle and a bulk Team action for creating teams from selected threads.

The frontend header count uses `GET /threads/{thread_id}/callable-tools`, which mirrors runtime visibility for that caller thread: ownership, team scoping, self-exclusion, disabled tools, and name conflicts.

---

## System Tools

### bash_execute

Execute shell commands in the backend environment. In Docker deployments, the container is the primary sandbox boundary.

```python
bash_execute(command: str, working_directory: Optional[str] = None, timeout_seconds: int = 120, run_in_background: bool = False)
```

At runtime, Nymeria detects the backend platform, available shells, container status, process cwd, and default tool cwd. The agent sees those facts in the `bash_execute` description before it formats commands. When `working_directory` is omitted, commands run from `settings.project_root`. Relative `working_directory` values resolve from that same default cwd. Directory state is not preserved across calls.

**Parameters:**
- `command` (`str`): Shell command to execute
- `working_directory` (`Optional[str]`, default `None`): Directory to run the command in. Relative paths resolve from the detected default tool cwd.
- `timeout_seconds` (`int`, default `120`): Maximum execution time in seconds
- `run_in_background` (`bool`, default `False`): Launch as a detached background process and return immediately.

**Returns:** Command output (stdout + stderr combined) or error message. Non-zero exit codes are appended. Output truncated at 50,000 characters.

When `run_in_background=True` is called from an agent thread, stdout and stderr are captured to secure temp files and the return value includes `job_id`, `pid`, and both file paths. When the process exits, Nymeria submits a completion prompt to the same thread with the exit code and the last 4KB of stdout/stderr. If that thread is busy, the prompt is queued and absorbed at the next sub-turn boundary. If the thread is idle, the prompt runs as an autonomous turn and streams through the normal autonomous event path. Temp files persist until manually removed. The watcher is process-local; if the backend restarts before the process exits, no completion notification is sent. Calls without agent thread/user context keep the legacy PID-only fire-and-forget behavior.

**Security:** MODERATE  -  runs commands without an in-process sandbox. Use deployment-level containment for untrusted workloads.

---

### file_read

Read the contents of a file.

```python
file_read(file_path: str, encoding: str = "utf-8", max_lines: Optional[int] = None)
```

Relative paths resolve from the same detected default tool cwd used by `bash_execute`, normally `settings.project_root`. Shell `cd` commands do not affect file tool paths.

**Parameters:**
- `file_path` (`str`): Absolute or relative path to the file. Relative paths resolve from the detected default tool cwd.
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
- `file_path` (`str`): Absolute or relative path to the file. Relative paths resolve from the detected default tool cwd.
- `content` (`str`): Content to write
- `encoding` (`str`, default `"utf-8"`): File encoding
- `create_directories` (`bool`, default `True`): Create parent directories if they don't exist
- `append` (`bool`, default `False`): Append to file instead of overwriting
- `attach` (`bool`, default `False`): Deliver the written file back to chat clients (Telegram, Discord, desktop/mobile artifact viewers). When attach succeeds, the raw tool result includes an `[attach:/path]` tag for backward compatibility and the API emits a structured `workspace_artifact` SSE event.

**Returns:** Success/error message with character count. When `attach=True`, the raw tool result includes an `[attach:/path]` tag for files inside `NYMERIA_WORKSPACE_DIR`, and clients receive a `workspace_artifact` event.

**Optional confinement:** Set `NYMERIA_CONFINE_FILE_TO_WORKSPACE=true` to reject write targets outside `NYMERIA_WORKSPACE_DIR`. Relative write paths still resolve from the detected default tool cwd, so pass an absolute path inside the workspace when using confinement for generated artifacts.

**Protected paths:** Writes to `nymeria/core/`, `nymeria/config/`, `nymeria/triggers/`, `nymeria/gateway/`, and `nymeria/__init__.py` are blocked. Use the SelfModifyAgent for those directories.

---

### file_edit (Optional)

Precisely edit an existing text file with exact, all-or-nothing operations. This is not loaded by default; enable it per-thread before use.

```python
file_edit(file_path: str, edits: list[dict], encoding: str = "utf-8", dry_run: bool = False, expected_sha256: Optional[str] = None, max_diff_chars: int = 20000)
```

**Parameters:**
- `file_path` (`str`): Absolute or relative path to an existing file. Relative paths resolve from the detected default tool cwd.
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

**Limits and protected paths:** Same 10 MB text-file limit and protected Nymeria paths as `file_write`. Set `NYMERIA_CONFINE_FILE_TO_WORKSPACE=true` to reject edit targets outside `NYMERIA_WORKSPACE_DIR`.

---

### ~~file_list~~ (removed)

**Deprecated.** Removed from `ALL_TOOLS` and `TOOL_METADATA`. For trusted maintenance threads, an admin may enable `bash_execute` and use shell listing commands instead.

---

### web_search_perplexity

Search the web using the Perplexity API. Opt-in tool in the
`WEB_SEARCH_SERVICE_TOOLS` group (enable per thread), not a core tool.

```python
web_search_perplexity(query: str = "", queries: str = "", search_depth: Optional[str] = None, max_sources: Optional[int] = None)
```

**Parameters:**
- `query` (`str`): Single search query
- `queries` (`str`): Multiple queries separated by `" | "` (pipe with spaces); takes precedence over `query`, max 10 per call
- `search_depth` (`Optional[str]`): `"quick"` (sonar), `"standard"` (sonar-pro), or `"deep"` (sonar-deep-research). Defaults to `settings.perplexity_search_model`.
- `max_sources` (`Optional[int]`): Maximum sources to cite (1-10, default 5)

**Returns:** Search results with a numbered `**Sources:**` list (batch mode adds `=== Query N/M: ... ===` headers); errors as `[Error]: ...`.

**Requires:** a Perplexity credential, resolved credential vault -> `PERPLEXITY_API_KEY` setting/env. The agent can self-provision via `request_credential(provider="perplexity", bind_target="native_tool:web_search_perplexity")`.

**Timeouts:** 60s for quick/standard, 180s for deep research. Max tokens: 2000 for quick/standard, 4000 for deep.

---

### web_search_tavily

Search the web using Tavily, an agent-optimized retrieval API. Returns a ranked
list of sources rather than a synthesized answer (use `web_search_perplexity`
for an answer). Opt-in tool in the `WEB_SEARCH_INTEGRATION_TOOLS` group (enable
per thread), not a core tool.

```python
web_search_tavily(query: str = "", queries: str = "", search_depth: Optional[str] = None, topic: Optional[str] = None, max_results: Optional[int] = None, time_range: Optional[str] = None, include_domains: str = "", exclude_domains: str = "", include_answer: Optional[str] = None)
```

**Parameters (all agent-controlled per query):**
- `query` (`str`): Single search query
- `queries` (`str`): Multiple queries separated by `" | "` (pipe with spaces); takes precedence over `query`, max 10 per call
- `search_depth` (`Optional[str]`): `"basic"` (1 credit, default) or `"advanced"` (2 credits, deeper/more relevant)
- `topic` (`Optional[str]`): `"general"` (default), `"news"`, or `"finance"` (an index router, not a query term)
- `max_results` (`Optional[int]`): Sources to return per query (1-20, default 5)
- `time_range` (`Optional[str]`): Restrict by recency: `"day"`, `"week"`, `"month"`, or `"year"`
- `include_domains` (`str`): Comma-separated domains to restrict results to
- `exclude_domains` (`str`): Comma-separated domains to exclude
- `include_answer` (`Optional[str]`): `"basic"` or `"advanced"` to prepend a short LLM answer (off by default; adds no extra credits)

Hard defaults (not exposed): `include_raw_content`, `include_images`, `auto_parameters` are off; `country`/`chunks_per_source` use API defaults.

**Returns:** Ranked sources as `N. <title>\n   <url>  (score) · <date>\n   <snippet>`. With `include_answer`, an `**Answer:**` block precedes a `**Sources:**` list. Batch mode adds `=== Query N/M: ... ===` headers. Errors as `[Error]: ...`.

**Requires:** a Tavily credential, resolved credential vault -> `TAVILY_API_KEY` setting/env. The agent can self-provision via `request_credential(provider="tavily", bind_target="native_tool:web_search_tavily")`. Free tier: 1,000 credits/month.

**Timeouts:** 30s basic, 60s advanced.

### web_search_exa_ai

Search the web using Exa, a neural/semantic retrieval API. Finds conceptually
related pages that plain keyword search misses and returns the most
query-relevant highlight sentences from each page. Returns ranked sources, not a
synthesized answer (use `web_search_perplexity` for an answer). Opt-in tool in
the `WEB_SEARCH_INTEGRATION_TOOLS` group (enable per thread), not a core tool.

```python
web_search_exa_ai(query: str = "", queries: str = "", search_type: Optional[str] = None, num_results: Optional[int] = None, category: Optional[str] = None, start_published_date: str = "", end_published_date: str = "", include_domains: str = "", exclude_domains: str = "")
```

**Parameters (all agent-controlled per query):**
- `query` (`str`): Single search query
- `queries` (`str`): Multiple queries separated by `" | "` (pipe with spaces); takes precedence over `query`, max 10 per call
- `search_type` (`Optional[str]`): `"auto"` (default, Exa picks the mode), `"fast"` (lower latency), or `"instant"` (fastest)
- `num_results` (`Optional[int]`): Sources to return per query (1-100, default 5); more than 10 costs extra credits
- `category` (`Optional[str]`): Restrict to a content type: `"news"`, `"research paper"`, `"company"`, `"financial report"`, `"people"`, or `"personal site"`. `"company"`/`"people"` ignore date filters and `exclude_domains`
- `start_published_date` (`str`): Only pages published on or after this ISO date (`YYYY-MM-DD`)
- `end_published_date` (`str`): Only pages published on or before this ISO date (`YYYY-MM-DD`)
- `include_domains` (`str`): Comma-separated domains to restrict results to
- `exclude_domains` (`str`): Comma-separated domains to exclude

Hard defaults (not exposed): `highlights` are always on (bundled free, used as the snippet, capped at ~1000 chars per result so output stays source-sized); full-page `text` and per-result `summary` are off (a dedicated fetch tool covers full pages; summaries cost extra credits); the agentic `deep`/`deep-reasoning` modes and image/subpage/extras payloads are off.

**Returns:** Ranked sources as `N. <title>\n   <url>  (score) · <date>\n   <highlights>`. Batch mode adds `=== Query N/M: ... ===` headers. Errors as `[Error]: ...`.

**Requires:** an Exa credential, resolved credential vault -> `EXA_API_KEY` setting/env. The agent can self-provision via `request_credential(provider="exa", bind_target="native_tool:web_search_exa_ai")`. Free tier: 1,000 requests/month; search bundles text + highlights for the first 10 results.

**Timeout:** 30s.

---

### web_search_firecrawl

Search the web using Firecrawl. Returns a ranked list of sources (title, URL,
snippet); this is the snippet-only search mode and does not scrape result pages.
Use `web_search_perplexity` for a synthesized answer, and a dedicated page-fetch
tool to pull the full body of a specific page. Opt-in tool in the
`WEB_SEARCH_INTEGRATION_TOOLS` group (enable per thread), not a core tool.

```python
web_search_firecrawl(query: str = "", queries: str = "", limit: Optional[int] = None, time_range: Optional[str] = None, sources: str = "", categories: str = "", include_domains: str = "", exclude_domains: str = "")
```

**Parameters (all agent-controlled per query):**
- `query` (`str`): Single search query
- `queries` (`str`): Multiple queries separated by `" | "` (pipe with spaces); takes precedence over `query`, max 10 per call
- `limit` (`Optional[int]`): Sources to return per query (1-100, default 5)
- `time_range` (`Optional[str]`): Restrict by recency: `"hour"`, `"day"`, `"week"`, `"month"`, or `"year"`
- `sources` (`str`): Comma-separated result types: `"web"` (default) and/or `"news"`
- `categories` (`str`): Comma-separated content-type filters: `"github"`, `"research"`, or `"pdf"`
- `include_domains` (`str`): Comma-separated domains to restrict results to; cannot be combined with `exclude_domains` (they are mutually exclusive, so `exclude_domains` is dropped when both are set)
- `exclude_domains` (`str`): Comma-separated domains to exclude

Hard defaults (not exposed): `scrapeOptions` is never set, so result pages are not scraped (scraping every hit costs roughly 6x the credits and overlaps the page-fetch tool's role; full page bodies belong to that dedicated tool). The `images` source, geo `location`/`country`, and the `enhanced` proxy are off.

**Returns:** Ranked sources as `N. <title>[ [news]]\n   <url> · <date>\n   <snippet>`. News results are tagged `[news]`. Batch mode adds `=== Query N/M: ... ===` headers. Errors as `[Error]: ...`.

**Requires:** a Firecrawl credential, resolved credential vault -> `FIRECRAWL_API_KEY` setting/env. The agent can self-provision via `request_credential(provider="firecrawl", bind_target="native_tool:web_search_firecrawl")`. Free tier: 1,000 credits/month; search costs 2 credits per 10 results.

**Timeout:** 30s.

---

### web_search_brave

Search the web using Brave Search, which runs on Brave's own independent index
(not a Google/Bing reseller). Returns a ranked list of sources (title, URL,
snippet). Use `web_search_perplexity` for a synthesized answer, and a dedicated
page-fetch tool to pull the full body of a specific page. Opt-in tool in the
`WEB_SEARCH_INTEGRATION_TOOLS` group (enable per thread), not a core tool.

```python
web_search_brave(query: str = "", queries: str = "", count: Optional[int] = None, time_range: Optional[str] = None, sources: str = "", include_domains: str = "", exclude_domains: str = "")
```

**Parameters (all agent-controlled per query):**
- `query` (`str`): Single search query (keep under ~400 chars / 50 words, including any domain filters)
- `queries` (`str`): Multiple queries separated by `" | "` (pipe with spaces); takes precedence over `query`, max 10 per call
- `count` (`Optional[int]`): Sources to return per query (1-20, default 5)
- `time_range` (`Optional[str]`): Restrict by recency: `"day"`, `"week"`, `"month"`, or `"year"`; also accepts an explicit `"YYYY-MM-DDtoYYYY-MM-DD"` range
- `sources` (`str`): Comma-separated result types: `"web"` (default), `"news"`, and/or `"discussions"` (forum threads)
- `include_domains` (`str`): Comma-separated domains to restrict results to. Brave has no native domain filter, so these are applied as `site:` operators in the query
- `exclude_domains` (`str`): Comma-separated domains to exclude (applied as `NOT site:` operators)

Hard defaults (not exposed): `text_decorations=false` (no `<strong>` markup in snippets); `extra_snippets` off (every result already carries a concise `description`; extra_snippets only adds raw RAG-style excerpts, more tokens); `safesearch`, `country`, `search_lang`, `ui_lang` left at Brave defaults; `goggles`, `summary`, and `offset` (pagination) off.

**Returns:** Ranked sources as `N. <title>[ [news]]\n   <url> · <date>\n   <snippet>`. News and discussion results are tagged. Batch mode adds `=== Query N/M: ... ===` headers. Errors as `[Error]: ...`.

**Requires:** a Brave credential, resolved credential vault -> `BRAVE_API_KEY` setting/env. The agent can self-provision via `request_credential(provider="brave", bind_target="native_tool:web_search_brave")`. Brave removed its free tier on 2026-02-12, so a paid Search plan is required (about $5 per 1,000 queries, with a $5 monthly credit auto-applied).

**Timeout:** 15s.

---

### web_search_searxng

Search the web using a self-hosted SearXNG instance. SearXNG is a keyless
metasearch engine: it queries many upstream engines (DuckDuckGo, Brave, Google,
Mojeek, Wikipedia, and more) and returns a single merged, relevance-ranked list
of sources (title, URL, snippet). Use `web_search_perplexity` for a synthesized
answer, and a dedicated page-fetch tool to pull the full body of a specific
page. Opt-in tool in the `WEB_SEARCH_INTEGRATION_TOOLS` group (enable per
thread), not a core tool. This replaced the older `searxng_search` utility tool.

```python
web_search_searxng(query: str = "", queries: str = "", count: Optional[int] = None, time_range: Optional[str] = None, sources: str = "", include_domains: str = "", exclude_domains: str = "")
```

**Parameters (all agent-controlled per query):**
- `query` (`str`): Single search query
- `queries` (`str`): Multiple queries separated by `" | "` (pipe with spaces); takes precedence over `query`, max 10 per call
- `count` (`Optional[int]`): Sources to return per query (1-20, default 5). SearXNG returns a full page of merged results; this keeps the top-ranked `count` (a display cap on an already-ranked page, not a fetch directive)
- `time_range` (`Optional[str]`): Restrict by recency: `"day"`, `"week"`, `"month"`, or `"year"` (maps 1:1 to SearXNG's own values)
- `sources` (`str`): Comma-separated result categories: `"general"` (default), `"news"`, and/or `"science"`
- `include_domains` (`str`): Comma-separated domains to restrict results to. SearXNG has no native domain filter, so these are applied as `site:` operators in the query
- `exclude_domains` (`str`): Comma-separated domains to exclude (applied as `-site:` operators)

Hard defaults (not exposed): `format=json`; `pageno=1` (no pagination); `safesearch=1` (moderate); `language` and `engines` left to the instance configuration.

**Returns:** Ranked sources as `N. <title>[ [news]]\n   <url> · <date>\n   <snippet>`. Non-general categories are tagged. Batch mode adds `=== Query N/M: ... ===` headers. Errors as `[Error]: ...`.

**Requires:** a running SearXNG instance with JSON output enabled (`search.formats` must include `json`). The base URL is resolved credential vault (provider `searxng`, field `base_url`) -> `searxng_base_url` setting -> `SEARXNG_BASE_URL` env. The bundled Docker sidecar (the `search` compose profile) ships JSON pre-enabled and defaults `SEARXNG_BASE_URL` to `http://searxng:8080`. No API key (SearXNG is keyless). Note: SearXNG is blocked from datacenter IPs for some engines, but the instance's own egress IP governs that, not Nymeria.

**Timeout:** 20s.

---

### fetch_url_nymeria

Fetch a web page or PDF by URL and return its readable content. This is the free,
in-process member of a fetch family (`fetch_url_<provider>`); hosted members
(`fetch_url_firecrawl`, ...) land later as separate opt-in tools for the cases
free extraction cannot match (hard anti-bot). Where the `web_search_*` tools find
URLs and return snippets, this tool reads the full body of a specific page.

The fetch is gated by the shared HTTP egress policy (`core/http_policy.py`): the
agent supplies an arbitrary URL, so every request and redirect hop is validated
against the SSRF rules (no loopback, private, link-local, or metadata targets)
with DNS pinning. Content is extracted with Trafilatura (primary, emits markdown)
and a readability-lxml + markdownify fallback; PDFs go through pypdf.

```python
fetch_url_nymeria(url: str = "", urls: str = "", extract: str = "markdown", distill: str = "", max_length: int = 8000)
```

**Parameters (agent-controlled):**
- `url` (`str`): Single URL to fetch
- `urls` (`str`): Multiple URLs separated by `" | "` (pipe) or commas; takes precedence over `url`, max 10 per call
- `extract` (`str`): `"markdown"` (default, preserves structure) or `"text"` (plain prose)
- `distill` (`str`): Leave empty to return the full readable page. Provide an instruction (e.g. `"pricing tiers and limits"`) to have a secondary LLM read the page and return only that, instead of the full text
- `max_length` (`int`): Max characters of content returned (clamped 500-50000, default 8000); long pages are truncated when `distill` is empty

Hard defaults (not exposed): granular httpx timeouts (connect 10s, read 25s), an honest `User-Agent`, a 10 MB fetch guard, redirect handling and DNS pinning via the egress policy, and the extraction cascade order.

**Returns:** A short header (title, source URL, redirect note) followed by the content. Batch mode adds `=== URL N/M: <url> ===` headers. Failures are returned as `[Error]: <reason>` strings (blocked by egress policy, HTTP code, timeout, unsupported content type, or could-not-extract), never raised.

**Distill step:** when `distill` is non-empty, uses a dedicated, optional model resolved from the global settings `fetch_summary_provider` / `fetch_summary_model` / `fetch_summary_base_url` (env `FETCH_SUMMARY_PROVIDER` / `FETCH_SUMMARY_MODEL` / `FETCH_SUMMARY_BASE_URL`). A small local model works well (no tool calling needed). When unset, it falls back to the main agent model.

**Scope:** v1 is static and server-rendered pages plus PDFs. JavaScript-only pages may return little content (a hosted fetch provider or the future browser tier handles those). No API key (the in-process path is keyless).

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
- `model` (`Optional[str]`, default `None`): Model alias  -  `"gemini-3-pro"` (default), `"gemini-2.5-pro"`, or `"gemini-2.5-flash"`

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

Invoke Claude Code in headless mode to create, modify, or analyze code. **Not loaded by default**  -  lives in `OPTIONAL_TOOLS` and is gated by `ADMIN_ONLY_OPTIONAL_TOOL_NAMES` (only admins may enable it).

```python
claude_code(prompt: str, working_dir: Optional[str] = None, model: str = "sonnet",
            allow_edit: bool = True, allow_bash: bool = True, timeout: int = 300)
```

**Parameters:**
- `prompt` (`str`): The coding task or question
- `working_dir` (`Optional[str]`, default `None`): Directory to run in. Defaults to the backend process working directory.
- `model` (`str`, default `"sonnet"`): Model  -  `"sonnet"`, `"opus"`, or `"haiku"`
- `allow_edit` (`bool`, default `True`): Allow Claude Code to edit files
- `allow_bash` (`bool`, default `True`): Allow Claude Code to run commands.
- `timeout` (`int`, default `300`): Timeout in seconds

**Returns:** Claude Code's response or error message. Output truncated at 50,000 characters.

**Requires:** `claude` CLI binary in PATH (install with `npm install -g @anthropic-ai/claude-code`).

**Tools passed to Claude Code:** Always includes `Read`. Adds `Edit` if `allow_edit=True`, `Bash` if `allow_bash=True` and the environment escape hatch is enabled.

---

## Memory Tools

Three unified primitives  -  `memory_add`, `memory_edit`, `memory_read`  -  cover both global user-profile facts and per-thread notepad content. The `scope` argument selects which store:

- `scope="global"`  -  keyed entries in the user's profile, **automatically injected** into Nymeria's system prompt across every future thread as explicitly untrusted JSONL data records. Storage: `data/users/{user_id}/profile.json`.
- `scope="thread"`  -  free-form markdown notepad for the active thread, re-injected after context compaction. Storage: `data/thread_notes/{thread_id}.md`.

Empty `content` (in `memory_add`) or empty `replace` whose result empties the entry (in `memory_edit`) deletes cleanly: profile rows are popped, notepad files are unlinked. There is no separate `memory_forget` because the storage layer treats blank-as-delete, so edit-to-blank leaves no zombie entries.

Global profile memories and per-thread notepads have an aggregate character budget. The global default is `MEMORY_CHAR_LIMIT=8000`; a thread can override the notepad limit from thread settings. Writes that would grow past the effective budget fail with a memory-full error telling the agent to consolidate or remove older memories. Deletes and shrinking edits are still allowed when existing data is already over the limit.

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
memory_add(scope="global", key="timezone", content="America/New_York")
memory_add(scope="thread", content="Working on auth refactor; deadline Friday.")
memory_add(scope="global", key="prefers_typescript", content="")   # deletes the entry
memory_add(scope="thread", content="")                             # deletes the notepad
```

**Behavior:**
- `scope="global"` upserts into `UserProfile.memories` and re-indexes in the RAG store if RAG is enabled.
- `scope="thread"` overwrites the notepad (replace semantics; for append-style writes, read-then-add).
- Memory max size: `MEMORY_CHAR_LIMIT` characters by default, with optional per-thread notepad overrides. Profile max entries: 100. Profile values are truncated to 1000 chars.
- Prompt injection guard: profile values are rendered as data, not Markdown instructions; embedded commands, role changes, and tool requests must not be followed by the model.

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
- For long profile values use `memory_add` to overwrite  -  `memory_edit` shines for thread-notepad surgical edits.

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
- Profile memories are auto-injected into the system prompt, but weaker models may struggle to extract exact keys from long prompts  -  `memory_read(scope="global")` gives an explicit listing.

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

Search your own memory (past conversations, saved memories, completed TODOs) using hybrid retrieval over the per-user `MemoryIndex` (sqlite-vec vectors + FTS5 BM25). This is the deliberate, agent-driven retrieval surface: the agent calls it when it needs recall. (Auto-injecting RAG chunks into hidden prompt context is not default behavior.)

```python
rag_search(query: str, max_results: int = 5, thread_id: str | None = None,
           since: str | None = None, until: str | None = None,
           around: str | None = None)
```

**Parameters:**
- `query` (`str`): What to search for.
- `max_results` (`int`, default `5`): Maximum results (clamped 1-10).
- `thread_id` (`str`, optional): Restrict to a single source thread.
- `since` / `until` (`str`, optional): ISO date/datetime bounds on event time, e.g. `"2026-05-01"` or `"2026-05-01T09:00:00"`. These are hard bounds (out-of-range chunks are excluded).
- `around` (`str`, optional): A date to softly bias results toward (for "that report from last spring"), parsed by `parse_anchor_string` into a Gaussian-with-floor time window. Unlike `since`/`until` it never excludes a strong match from another time. Honored only when `settings.rag_anchor_enabled` (default on); ignored otherwise.

**Ranking:** hybrid fusion is Reciprocal Rank Fusion (`settings.rag_fusion_method`, `rrf` default or legacy `weighted`). Two soft-multipliers refine the order after fusion, never filtering: a per-chunk_type recency factor (`settings.rag_recency_enabled`, default off) with deliberately long half-lives (memory 1825d, conversation 730d, todo 730d, tool 365d); it defaults off because eval found the multiplier demoted older evidence and dragged hybrid below the BM25 floor, and reconciliation already handles staleness; and a prose-priority factor (`settings.rag_prose_priority_enabled`, default on, strength `rag_prose_priority_weight` default 0.4) that demotes a chunk by the share of it that is templated tool-result text, so user/assistant prose ranks above tool output. The query text is sanitized into a safe FTS5 MATCH expression, so punctuation and operator words (apostrophes, `AND`/`OR`, quotes) no longer cause a silent empty result. Near-duplicate results are then collapsed (`settings.rag_dedup_enabled`, default on, token-set Jaccard at/above `rag_dedup_threshold`, default 0.9) so overlapping-window chunks and live-vs-flush twins do not occupy several of the top-k slots. On a selective `thread_id`/`since`/`until` search the vector candidate pool is widened (vec0 cannot filter in SQL), so an in-scope semantic match is not crowded out by nearer out-of-scope chunks. When the agent passes `around`, a date-anchor recall branch joins the fusion as a third RRF term (weight `settings.rag_anchor_weight`, default 0.5, vs 1.0 for vector/BM25) and a soft time-multiplier (lower-bounded at `settings.rag_anchor_floor`, default 0.4) biases toward on-date chunks without filtering, so a strongly-relevant chunk far from the date keeps at least 40% of its fused score.

**Returns:** a `now:` anchor header plus numbered entries. Each entry shows `[chunk_type]`, a relative age and the event time, a 0-1 relevance, the source thread title + `thread_id` (saved memories show `saved memory (global)`), and a content snippet (truncated to `settings.rag_result_max_chars`, default 1000). The `thread_id` lets the agent follow the source: call it if it is a bound callable thread, or open it by id. A trailing `[retrieval]` line reports the live stack so the configured embedder/reranker can be confirmed working in testing: which embedder produced the query vector (`provider:model@dimsd`) and whether the vector branch actually ran (`vector live, N cand`) or degraded to BM25 (`vector unavailable -> BM25 lexical only`), which reranker applied (`reranked by voyage:rerank-2.5 [applied (reordered)]`, or `rerank off`, or `misconfigured (...)` when a managed reranker is enabled without a key/model), and the pool depth (`N retrieved -> rerank top M -> K returned`). The vector/branch facts come from `MemoryIndex._last_search_diag` (what actually ran this call), not just configured values. `[No Results]`, `[RAG Disabled]`, and `[Error]` cover the other cases. Results are filtered by the user's RAG preferences (`include_conversations`, `include_memories`, `include_todos`); **`include_memories` defaults to off** because saved memories are already loaded into the agent's context, so returning them here would be redundant (opt back in via RAG settings).

**Tool-call results are indexed:** conversation-turn indexing embeds a templated summary of the turn's tool calls and results (`Tools used: ...`), with the raw results kept in chunk metadata, so prior tool activity is retrievable, not just user/assistant prose. Tools that read FROM the index (`rag_search`, `memory_read`, any `rag_*`) are excluded from that embedded summary: their results are content already in the corpus, so re-embedding them would inject near-duplicate chunks (the index eating its own search results). The call name is still recorded in `metadata["tool_names"]` for provenance; only the duplicate content is dropped.

**Optional quality boosts (off by default):** `settings.rag_contextual_enabled` prepends an LLM-written context blurb to each chunk before embedding (Anthropic contextual retrieval; one LLM call per chunk at ingest). `settings.rag_rerank_enabled` reranks the top `rag_rerank_top_n` fused candidates with an LLM listwise rerank before truncating to `max_results`. Both fall back to the non-LLM behavior on any error (`core/rag_quality.py`). Measure ranking changes with `tools/rag_eval.py`: predicate-based probes scored by hit_rate@k / MRR / precision@k / nDCG@k, with a seeded BM25-only mode (no key, CI-safe) and a live mode (`--db`/`--probes`) plus `--compare` for A/B-ing configs. The harness can also A/B embedding models (`--embedding-model`/`--embedding-base-url`/`--embedding-input-type`, `--retrieval-mode all` for per-branch rows) and second-stage rerankers (`--rerank-model`/`--rerank-provider` for a local cross-encoder or a managed API). A reranker A/B (private notes: `docs/private/rag/rag-eval-notes.md`) found the LLM listwise reranker is the weakest reranking option, but the wider verdict is corpus-dependent. On clean LongMemEval prose a specialised CPU cross-encoder (Ettin-32m int8, local + free) lifts hybrid ndcg@5 +0.050 and the top rerank APIs (Cohere `rerank-v4.0-pro` +0.069, ZeroEntropy `zerank-2` +0.058) more, with the embedding model only marginal. On a real heterogeneous memory corpus this inverts: the local cross-encoder did NOT transfer (+0.008, and it degraded strong embeddings), while the embedding model became the dominant lever (bge-small to voyage-4-large +0.186 on the pure-vector branch, concentrated in markdown/code/structured chunks). So the production choice is unsettled: a strong API reranker (Cohere) is the robust option, and on real content upgrading the embedding model and going vector-first beats reranking. Do not ship the local cross-encoder as a blanket default.

**Indexing is automatic.** As of 2026-04, `opt_in.rag_enabled` defaults to `True` for new profiles, and existing profiles are migrated to `True` on first load (one-time, watermarked by `opt_in.rag_migrated`). Conversation turns are indexed in four places, in this order of frequency:

1. **Per turn**  -  `_index_conversation_turn` runs after every chat turn (`core/agent.py`).
2. **Pre-compact**  -  manual `/compact`, async auto-compact, and sync auto-compact all flush via `_pre_trim_memory_flush` before clearing messages.
3. **Pre-clear**  -  `POST /threads/{id}/clear` flushes before deleting checkpoints.
4. **Delete cleanup**  -  `DELETE /threads/{id}` runs the full thread cascade, including `MemoryIndex.delete_by_thread`, so `rag_search` doesn't surface chunks from deleted threads and thread-bound TODOs/triggers cannot wake the deleted thread again.

To opt out, use the RAG settings API or the frontend settings UI. The migration watermark prevents re-flipping on subsequent loads.

---

## TODO Tools

TODOs are the **primary driver for autonomous operation**. Active TODOs are automatically injected into the system prompt. Every TODO must have a `scheduled_for` time  -  TODOs are for the agent's autonomous work queue, not a general task list.

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
- `task` (`str`): Task description  -  **required** for create, optional for update
- `scheduled_for` (`str`): When to execute  -  **required** for create, optional for update. Formats:
  - Relative: any positive seconds/minutes/hours/days/weeks duration, e.g. `"45s"`, `"17m"`, `"3h"`, `"2d"`, `"1w"`
  - Absolute: `"YYYY-MM-DD HH:MM[:SS]"` or `"YYYY-MM-DDTHH:MM[:SS]"` (user timezone)
  - ISO with timezone: `"YYYY-MM-DDTHH:MM:SSZ"` or `"YYYY-MM-DDTHH:MM:SS-04:00"`
- `status` (`Optional[str]`): `"pending"`, `"in_progress"`, or `"done"` (update only)
- `notes` (`Optional[str]`): Add or update notes (max 1000 characters)
- `recurrence` (`Optional[str]`): Interval as a canonical duration string  -  `Nm`, `Nh`, `Nd`, `Nw` (or `Ns` with a 60s minimum). Examples: `"5m"`, `"2h"`, `"1d"`, `"1w"`. Legacy preset names are also accepted on input and normalised: `"hourly"`, `"daily"`, `"weekly"`, `"monthly"`, `"5min"`, `"10min"`, `"15min"`, `"30min"`.
- `clear_schedule` (`bool`, default `False`): Remove scheduled time (update only)
- `clear_recurrence` (`bool`, default `False`): Remove recurrence pattern (update only)

**Returns:** Confirmation with TODO ID and details, or error.

**Limits:** 50 active TODOs per user (`MAX_TODOS` in `TodoList`).

**Thread scope:** Creates TODOs on the current thread. Updates only find TODOs that already belong to the current thread; a TODO ID from another thread is treated as not found.

**Statuses:** `pending` (default), `in_progress`, `done`. Use `nym_todo(todo_id=..., status="done")` to complete a TODO.

**Recurring TODOs:** Recurring TODOs **auto-reschedule when marked done**  -  regardless of whether the ticker executed them or the agent/user marked them done manually. The next `scheduled_for` is calculated from the prior scheduled fire time, not the later completion time, so a task scheduled hourly for `10:00` moves to `11:00` even if the agent marks it done at `10:03`. If Nymeria was offline long enough to miss intervals, it skips forward to the next future slot. The status resets to `pending`. This applies to all completion paths: the `nym_todo` tool, the REST API, and the MCP server. To permanently stop a recurring TODO, use `nym_todo(todo_id=..., clear_recurrence=True)` or `nym_todo_delete`.

**Auto-purge:** Completed TODOs remain visible to `nym_todo_list(filter_status="done")` and `GET /todos?filter_status=done` until the ticker cleanup removes them from `data/todos/{user_id}.json`. The retention is controlled by `TODO_AUTO_ARCHIVE_DAYS` (default 7 days, range 1-30). There is no separate completed-TODO archive file; use activity/RAG history for historical outcome lookup after cleanup.

---

### nym_todo_delete

Delete a TODO permanently. No archive  -  immediately removed. Cancels any scheduled execution.

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

Send a notification to the user through their configured channels.

```python
notify(message: str, profile: Optional[str] = None)
```

**Parameters:**
- `message` (`str`): The message text to send.
- `profile` (`Optional[str]`): Name of a destination profile to route through. Omit to use the per-thread or per-user default profile.

**Returns:** A status string summarising which destinations succeeded or failed.

**Resolution order for the profile:**
1. `profile` arg on the call (per-call override).
2. `ThreadConfig.notification_profile` (per-thread override; set in the Chat-App tab of thread settings).
3. `UserProfile.preferences['notifications']['default_profile']` (user-level default; set in Settings -> Notifications).
4. The built-in `"default"` profile, auto-seeded from any existing global env config (TELEGRAM_BOT_TOKEN, DISCORD_WEBHOOK_URL, etc.).

**Audit-log behaviour:** Every call ALSO writes a row in the in-app notification feed regardless of which external destinations were used. The row carries `delivered_to`, `attempted`, and `errors` so the desktop sidebar can render badges showing where the notification went and which destinations failed.

**Backward compatibility:** A legacy `platform=` keyword is still accepted (with a deprecation log line) to keep older trigger configs and prompts working without edits. Map: `telegram` -> destination `telegram-default`, etc. New code should use `profile` exclusively.

**Architecture:** Destinations (concrete delivery targets) and profiles (named bundles of destinations) live in the SQLite-backed `NotificationDestinationsRepo` (`core/notification_destinations.py`). Channel types (telegram, discord, slack, teams, webhook, fcm, email_outlook) live in a registry in `core/notification_channels.py`. See [`notifications.md`](notifications.md) for the full reference.

### tool_search

Search available tools by keyword/category. This is search-only; mutations live
in `tool_manage`.

```python
tool_search(query: str = "", category: str = "", top_k: int = 15, include_status: bool = True)
```

**Parameters:**
- `query` (`str`): Keyword to search tool names and descriptions.
- `category` (`str`): Optional category filter (e.g. `"email"`, `"slack"`).
- `top_k` (`int`): Result count, default 15 and capped at 50.
- `include_status` (`bool`): Include current-thread enabled/disabled annotations.

`tool_search` uses the shared backend search service behind
`GET /users/{user_id}/tools/search`. The indexed catalog includes core tools,
optional tools, MCP-discovered metadata, custom tool metadata, and callable
thread tools visible from the current `thread_id`. Developer-only diagnostic
tools are hidden from non-admin users. Admin-only tools remain discoverable but
return enable hints that make the admin requirement explicit.

Ranking tries embeddings first when `EMBEDDING_API_KEY`, `EMBEDDING_BASE_URL`,
and `EMBEDDING_MODEL` are configured. CLIProxy gatekeeper keys such as
`cpx-*` are rejected for embeddings. If semantic search is unavailable, the
service falls back to BM25, then fuzzy matching, then substring matching. Each
result includes status and an exact enable/disable hint such as
`/tools enable browser_open`.

The same backend ranking is used by one-shot command searches:
- CLI/plain slash: `/tools <query>` and `/tools search <query>`
- Discord: `/tools search query:<text>`
- Telegram: `/tools_search <query>`

Desktop and mobile typeahead filtering stays local for responsiveness, but uses
the shared frontend `utils/toolSearch.ts` fuzzy scorer over tool names, snake
case tokens, categories, descriptions, tags, and implementation/type fields.

## Service Integration Tools (Optional)

Credential-aware native tools use the existing encrypted credential vault. Save
connections in Settings > Connections with these provider names and fields:
`wolfram_alpha.app_id`, `searxng.base_url`, `nasa.api_key`,
`openweathermap.api_key`, optional `npm.registry_url` / `npm.token`,
`google_books.api_key`, `youtube.api_key`, `spotify.access_token` or
`spotify.client_id` plus `spotify.client_secret`, `reddit.access_token` or
`reddit.refresh_token` plus app credentials, `discourse.base_url`,
`medium.access_token`, `bamboohr.api_key` plus `bamboohr.subdomain`,
`beeminder.auth_token`, `clockify.api_key`, and `harvest.access_token` plus
`harvest.account_id`, `oura.access_token`, `strava.access_token`,
`homeassistant.base_url` plus `homeassistant.access_token`, and
`philips_hue.access_token` plus `philips_hue.username`,
`activecampaign.api_key` plus `activecampaign.api_url`,
`convertkit.api_secret`, `getresponse.api_key`, and
`mailerlite.api_key`, `actionnetwork.api_key`, `autopilot.api_key`,
`egoi.api_key`, `vero.auth_token`, `customerio.tracking_site_id` plus
`customerio.tracking_api_key` and optional `customerio.app_api_key`,
`iterable.api_key`, `posthog.api_key`, and `segment.write_key`, plus
`github.access_token`, `gitlab.access_token`, `graphql.endpoint` plus optional
GraphQL auth fields, and `totp.secret`. GitHub credentials can also provide
`base_url` / `api_base_url` / `server`; GitLab credentials can provide
`base_url` / `server`. Build/CI credentials use `circleci.api_key`,
`travisci.api_token`, and `jenkins.base_url` plus `jenkins.username` and
`jenkins.api_key`. Data-table credentials include `adalo.api_key` plus
`adalo.app_id`, `bubble.api_token` plus `bubble.app_name`, and
`cockpit.base_url` plus `cockpit.access_token`. File-storage credentials use `dropbox.access_token`,
`nextcloud.webdav_url` plus either basic auth or an access token, and
`s3.access_key_id` plus `s3.secret_access_key`. AWS service credentials use
`aws.access_key_id` plus `aws.secret_access_key`, with optional
`aws.session_token`, `aws.region`, and `aws.endpoint_url`; existing S3/AWS
connections can also be reused for AWS service tools. Business-service credentials use `bitly.access_token`,
`brandfetch.api_key`, `marketstack.api_key`, `deepl.api_key`,
`lingvanex.api_key`, `apitemplate.api_key`, `onesimple.api_token`,
`paddle.vendor_id` plus `paddle.vendor_auth_code`, `profitwell.access_token`,
`tapfiliate.api_key`, `magento.access_token` plus `magento.host`, and
`unleashed.api_id` plus `unleashed.api_key`, `quickbooks.access_token` plus
`quickbooks.realm_id`, and `xero.access_token` plus `xero.tenant_id`; DeepL can also
use `deepl.api_plan = free` for the free endpoint. Productivity credentials use
`todoist.api_key`, `trello.api_key`, `trello.api_token`, and
`microsoft_graph.access_token`. Bookmark/link
credentials use `raindrop.access_token`, `yourls.url`, and either
`yourls.signature` or `yourls.username` plus `yourls.password`. Work-tracking
credentials use `asana.access_token` and `linear.api_key`. IT support credentials
use `freshservice.api_key` plus `freshservice.domain`, `servicenow.access_token`
or `servicenow.username` plus `servicenow.password`, and `zammad.token` plus
`zammad.base_url`. Chat credentials use `telegram.bot_token`,
`webex.access_token`, and `whatsapp.access_token` with optional
`whatsapp.business_account_id` and `whatsapp.phone_number_id`. Sales CRM credentials use `salesforce.access_token` plus
`salesforce.instance_url`, `zoho_crm.access_token`, `freshworks_crm.api_key`
plus `freshworks_crm.domain`, `salesmate.session_token` plus
`salesmate.link_name`, and `pipedrive.api_token` or `pipedrive.access_token`.
Relationship CRM
credentials use `copper.api_key` plus `copper.email`, `agilecrm.email` plus
`agilecrm.api_key` and `agilecrm.subdomain`, `monica.access_token`,
`affinity.api_key`, and `keap.access_token`.
Lead-enrichment credentials use `clearbit.api_key`, `uplead.api_key`,
`dropcontact.api_key`, `humantic.api_key`, `lonescale.api_key`, and
`uproc.email` plus `uproc.api_key`. Data-table credentials use
`baserow.token`, `nocodb.api_token`, `coda.access_token`, `grist.api_key`,
`supabase.base_url` plus `supabase.service_role`, `quickbase.hostname` plus
`quickbase.user_token`, `seatable.api_token`, `stackby.api_key`, and
`kobotoolbox.api_token`.
Transform credentials use
`crypto.hmac_secret`, `crypto.private_key`, `jwt.secret`, `jwt.private_key`,
`jwt.public_key`, and optional `jwt.algorithm`. Scope a
credential to one tool with `allowed target = native_tool:<tool_name>`, share it
across native tools with `native_tool:*`, or leave the target blank when it is
safe for any target. Tool responses never expose plaintext credential values.

### calculator

Evaluate a safe arithmetic expression locally.

```python
calculator(expression: str)
```

Supported operators are `+`, `-`, `*`, `/`, `//`, `%`, `**`, and parentheses.
Supported functions include `sqrt`, `sin`, `cos`, `tan`, `log`, `round`, `min`,
and `max`; constants are `pi`, `e`, and `tau`.

### wikipedia_search

Search Wikipedia and return article summaries.

```python
wikipedia_search(query: str, top_k_results: int = 3, language: str = "en", max_chars: int = 4000)
```

Requires the Python `langchain-community` and `wikipedia` packages from
`requirements.txt`.

### wolfram_alpha_query

Query Wolfram|Alpha for computational facts and calculations.

```python
wolfram_alpha_query(query: str)
```

Uses vault provider `wolfram_alpha` fields `app_id`, `appid`, or `value`, then
falls back to `WOLFRAM_ALPHA_APP_ID`. Also requires the Python
`langchain-community` and `wolframalpha` packages from `requirements.txt`.

### Transform Utility Tools

This batch includes local transform tools that do not call external services:
- `datetime_current(...)`, `datetime_add(...)`, `datetime_subtract(...)`, `datetime_format(...)`, `datetime_between(...)`, `datetime_extract(...)`, and `datetime_round(...)` for timezone-aware date/time work.
- `crypto_hash_text(text, algorithm?, encoding?)` and `crypto_generate_random(kind?, length?, alphabet?)` for local hashing and random string generation.
- `compression_gzip_text(text)`, `compression_gunzip_text(data_base64)`, `compression_zip_text_files(files_json)`, and `compression_unzip_text_files(archive_base64)` for bounded text/archive transforms.

Secret-backed transform tools use the credential vault:
- `crypto_hmac_text(...)` uses provider `crypto`, field `hmac_secret` / `hmacSecret` / `secret`; env fallback `CRYPTO_HMAC_SECRET`.
- `totp_generate_code(...)` and `totp_verify_code(...)` use provider `totp`, field `secret` / `totp_secret` / `totpSecret` / `value`; env fallback `TOTP_SECRET`.
- `crypto_sign_text(...)` uses provider `crypto`, field `sign_private_key` / `signPrivateKey` / `private_key` / `privateKey`; env fallbacks `CRYPTO_SIGN_PRIVATE_KEY` and optional `CRYPTO_SIGN_PRIVATE_KEY_PASSPHRASE`.
- `jwt_sign_claims(...)` and `jwt_verify_token(...)` use provider `jwt`, fields `secret`, `private_key`, `public_key`, and optional `algorithm`; env fallbacks `JWT_SECRET`, `JWT_PRIVATE_KEY`, `JWT_PUBLIC_KEY`, and `JWT_ALGORITHM`.

### Public Information Tools

The public information batch includes:
- `coingecko_price(ids, vs_currencies?, include_market_cap?, include_24hr_vol?, include_24hr_change?, include_last_updated_at?)`
- `coingecko_coin_markets(vs_currency?, ids?, category?, order?, limit?, page?, sparkline?, price_change_percentage?)`
- `hackernews_search(query?, tags?, limit?, page?)`, `hackernews_get_item(item_id, include_comments?)`, `hackernews_get_user(username)`
- `npm_package_info(package_name, version?)` and `npm_package_search(query, limit?, offset?)`; optional vault provider `npm` supports `registry_url` / `base_url` and `token` / `api_key` / `value`.
- `open_thesaurus_synonyms(text, ...)`
- `rss_feed_read(url, max_items?, ignore_ssl?)`; marked MODERATE because it fetches arbitrary user-provided URLs.
- `nasa_apod(date?, start_date?, end_date?, thumbs?)`; uses vault provider `nasa` fields `api_key` or `value`, then `NASA_API_KEY`.
- `openweathermap_current(...)` and `openweathermap_forecast(...)`; use vault provider `openweathermap` fields `api_key`, `access_token`, or `value`, then `OPENWEATHERMAP_API_KEY`.
- `quickchart_create_url(chart_type, labels_json, data_json, ...)`

### Media Discovery Service Tools

This batch includes:
- `google_books_search(query, print_type?, projection?, order_by?, filter_value?, language?, country?, limit?, start_index?)` and `google_books_get_volume(volume_id, projection?, country?)`. Google Books can use public data without a key; saved credentials can still supply `api_key` and `base_url`.
- `youtube_search(query, search_type?, channel_id?, order?, published_after?, published_before?, region_code?, safe_search?, page_token?, limit?)`, `youtube_get_videos(video_ids, parts?, max_width?, max_height?)`, `youtube_get_channels(channel_ids?, for_username?, parts?, limit?, page_token?)`, and `youtube_list_playlist_items(playlist_id, parts?, page_token?, limit?)`.
- `spotify_search(query, types?, market?, limit?, offset?, include_external?)`, `spotify_get_track(track_id, market?)`, `spotify_get_artist(artist_id)`, `spotify_get_album(album_id, market?)`, and `spotify_get_playlist(playlist_id, market?, fields?, additional_types?)`.

Credential providers and fallback env vars:
- Google Books: provider `google_books`, fields `api_key`, `key`, `token`, or `value`; optional env fallback `GOOGLE_BOOKS_API_KEY`.
- YouTube Data API: provider `youtube`, fields `api_key`, `key`, `token`, or `value`; env fallback `YOUTUBE_API_KEY`.
- Spotify: provider `spotify`, fields `access_token`, `bearer_token`, `token`, or `value`; env fallback `SPOTIFY_ACCESS_TOKEN`. If no access token is saved, catalog tools use `client_id` plus `client_secret` from the vault or `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` to request a client-credentials token.

### Community Publishing Service Tools

This batch includes:
- `reddit_search_posts(query, subreddit?, sort?, time_filter?, limit?, after?, include_over_18?)`, `reddit_list_subreddit_posts(subreddit, listing?, time_filter?, limit?, after?)`, `reddit_get_post(post_id, subreddit?, comment_limit?, comment_sort?)`, `reddit_get_subreddit(subreddit)`, and `reddit_get_user(username)`. Read tools use Reddit OAuth when available and otherwise fall back to public `.json` endpoints.
- `reddit_create_post(subreddit, title, kind?, text?, url?, resubmit?, send_replies?)`, `reddit_create_comment(parent_fullname, text)`, and `reddit_delete_thing(fullname)`. These require a user OAuth token or refresh token with the needed Reddit scopes.
- `discourse_search(query, page?)`, `discourse_list_latest_topics(page?)`, `discourse_get_topic(topic_id, include_raw?)`, and `discourse_get_post(post_id)`. Public forums can be read with only `base_url`; private forums can add an API key and username.
- `discourse_create_topic(title, raw, category_id?, tags?)`, `discourse_create_post(topic_id, raw)`, and `discourse_update_post(post_id, raw, edit_reason?)`; these require a Discourse API key plus API username.
- `medium_get_me()`, `medium_list_publications(user_id?)`, `medium_create_post(...)`, and `medium_create_publication_post(...)`. Medium's official API is archived upstream, but the token-based endpoints remain implemented for compatibility with existing accounts that still use them.
- X/Twitter: `twitter_get_me`, `twitter_get_user`, `twitter_search_recent`, `twitter_create_post`, `twitter_delete_post`, `twitter_like_post`, `twitter_repost`, and `twitter_send_direct_message`. Reads/search are SAFE; posting, deleting, liking, reposting, and DMs are MODERATE.
- LinkedIn: `linkedin_get_me` and `linkedin_create_post`. Profile lookup is SAFE; posting is MODERATE.
- Facebook Graph/Page tools: `facebook_graph_get_me`, `facebook_graph_get_node`, `facebook_page_list_accounts`, and `facebook_page_create_post`. Graph reads are SAFE; Page posting is MODERATE.

Credential providers and fallback env vars:
- Reddit: provider `reddit`, fields `access_token`, `refresh_token`, `client_id`, `client_secret`, and optional `base_url` / `public_base_url` / `token_url`; env fallbacks `REDDIT_ACCESS_TOKEN`, `REDDIT_REFRESH_TOKEN`, `REDDIT_CLIENT_ID`, and `REDDIT_CLIENT_SECRET`.
- Discourse: provider `discourse`, fields `base_url`, `api_key`, and `api_username`; env fallbacks `DISCOURSE_BASE_URL`, `DISCOURSE_API_KEY`, and `DISCOURSE_API_USERNAME`.
- Medium: provider `medium`, fields `access_token`, `token`, or `value`; env fallback `MEDIUM_ACCESS_TOKEN`.
- X/Twitter: provider `twitter` (aliases `x`, `x_twitter`, `twitter_oauth2`), fields `bearer_token`, `access_token`, `api_key`, `token`, or `value`; env fallbacks `TWITTER_BEARER_TOKEN` or `TWITTER_ACCESS_TOKEN`.
- LinkedIn: provider `linkedin`, fields `access_token`, `bearer_token`, `token`, or `value`; env fallback `LINKEDIN_ACCESS_TOKEN`.
- Facebook: provider `facebook` (aliases `facebook_graph`, `facebook_graph_api`, `meta_graph`), fields `access_token`, optional `page_access_token`, and optional `app_secret`; env fallbacks `FACEBOOK_ACCESS_TOKEN` and `FACEBOOK_APP_SECRET`.

### Time And HR Service Tools

This batch includes:
- `bamboohr_list_employees()`, `bamboohr_get_employee(employee_id, fields?)`, `bamboohr_create_employee(first_name, last_name, fields_json?)`, `bamboohr_update_employee(employee_id, fields_json)`, and `bamboohr_get_company_report(report_id, only_current?, format?)`.
- `beeminder_get_user()`, `beeminder_list_goals()`, `beeminder_get_goal(goal_slug)`, `beeminder_list_datapoints(goal_slug, page?, per_page?)`, `beeminder_create_datapoint(goal_slug, value, comment?, timestamp?, request_id?)`, `beeminder_update_datapoint(goal_slug, datapoint_id, fields_json)`, and `beeminder_delete_datapoint(goal_slug, datapoint_id)`.
- `clockify_list_workspaces()`, `clockify_list_users(workspace_id, status?, limit?)`, `clockify_list_projects(workspace_id, name?, archived?, limit?)`, `clockify_create_project(...)`, `clockify_list_time_entries(...)`, `clockify_create_time_entry(...)`, `clockify_update_time_entry(...)`, and `clockify_delete_time_entry(...)`.
- `harvest_get_me()`, `harvest_get_company()`, `harvest_list_clients(...)`, `harvest_list_projects(...)`, `harvest_list_tasks(...)`, `harvest_list_time_entries(...)`, `harvest_create_time_entry(...)`, `harvest_update_time_entry(...)`, `harvest_stop_time_entry(time_entry_id)`, and `harvest_delete_time_entry(time_entry_id)`.

Credential providers and fallback env vars:
- BambooHR: provider `bamboohr`, fields `api_key` and `subdomain`, optional `base_url`; env fallbacks `BAMBOOHR_API_KEY`, `BAMBOOHR_SUBDOMAIN`, and `BAMBOOHR_BASE_URL`.
- Beeminder: provider `beeminder`, fields `auth_token`, `access_token`, `api_key`, or `value`; env fallback `BEEMINDER_ACCESS_TOKEN`.
- Clockify: provider `clockify`, fields `api_key` or `value`; env fallback `CLOCKIFY_API_KEY`.
- Harvest: provider `harvest`, fields `access_token` and `account_id`, optional `base_url`; env fallbacks `HARVEST_ACCESS_TOKEN`, `HARVEST_ACCOUNT_ID`, and `HARVEST_BASE_URL`.

### Personal Device Service Tools

This batch includes:
- `oura_get_profile()`, `oura_get_daily_activity(start_date?, end_date?, limit?)`, `oura_get_daily_readiness(start_date?, end_date?, limit?)`, and `oura_get_daily_sleep(start_date?, end_date?, limit?)`.
- `strava_list_activities(before?, after?, limit?)`, `strava_get_activity(activity_id, include_all_efforts?)`, `strava_create_activity(...)`, `strava_update_activity(activity_id, fields_json)`, `strava_list_activity_comments(activity_id, limit?)`, and `strava_get_activity_streams(activity_id, keys?, key_by_type?)`.
- `homeassistant_get_config()`, `homeassistant_check_config()`, `homeassistant_list_states(limit?)`, `homeassistant_get_state(entity_id)`, `homeassistant_set_state(entity_id, state, attributes_json?)`, `homeassistant_list_services(limit?)`, `homeassistant_call_service(domain, service, data_json?)`, `homeassistant_list_events(limit?)`, `homeassistant_fire_event(event_type, data_json?)`, `homeassistant_render_template(template)`, and `homeassistant_get_logbook(...)`.
- `philips_hue_list_lights(limit?)`, `philips_hue_get_light(light_id)`, `philips_hue_update_light_state(...)`, and `philips_hue_delete_light(light_id)`. Hue tools require an existing bridge username; they do not press the bridge link button or create a Hue user implicitly.

Credential providers and fallback env vars:
- Oura: provider `oura`, fields `access_token`, `api_key`, `token`, or `value`; env fallback `OURA_ACCESS_TOKEN`.
- Strava: provider `strava`, fields `access_token`, `token`, or `value`; env fallback `STRAVA_ACCESS_TOKEN`.
- Home Assistant: provider `homeassistant`, fields `base_url` and `access_token`; env fallbacks `HOMEASSISTANT_BASE_URL` and `HOMEASSISTANT_ACCESS_TOKEN`.
- Philips Hue: provider `philips_hue`, fields `access_token` and `username`, optional `base_url`; env fallbacks `PHILIPS_HUE_ACCESS_TOKEN`, `PHILIPS_HUE_USERNAME`, and `PHILIPS_HUE_BASE_URL`.

### Marketing Contact Service Tools

This batch includes:
- `activecampaign_list_contacts(...)`, `activecampaign_get_contact(contact_id)`, `activecampaign_sync_contact(...)`, `activecampaign_update_contact(contact_id, fields_json)`, `activecampaign_list_lists(limit?)`, `activecampaign_list_tags(...)`, `activecampaign_add_contact_to_list(...)`, and `activecampaign_add_contact_tag(contact_id, tag_id)`. Reads are SAFE; sync/update/list/tag membership changes are MODERATE.
- `convertkit_get_account()`, `convertkit_list_forms()`, `convertkit_list_tags()`, `convertkit_list_subscribers(...)`, `convertkit_add_subscriber_to_form(...)`, and `convertkit_add_subscriber_to_tag(...)`. Reads are SAFE; subscription writes are MODERATE.
- `getresponse_list_campaigns()`, `getresponse_list_contacts(...)`, `getresponse_get_contact(contact_id)`, `getresponse_create_contact(...)`, `getresponse_update_contact(...)`, and `getresponse_delete_contact(contact_id)`. Reads are SAFE; contact writes/deletes are MODERATE.
- `mailerlite_list_subscribers(...)`, `mailerlite_get_subscriber(subscriber_id)`, `mailerlite_create_subscriber(...)`, `mailerlite_update_subscriber(...)`, and `mailerlite_list_groups(...)`. Reads are SAFE; subscriber writes are MODERATE.
- `actionnetwork_list_records(...)`, `actionnetwork_get_record(...)`, `actionnetwork_create_person(...)`, `actionnetwork_update_person(...)`, `actionnetwork_create_event(...)`, `actionnetwork_create_petition(...)`, `actionnetwork_create_attendance(...)`, `actionnetwork_create_signature(...)`, `actionnetwork_add_person_tag(...)`, and `actionnetwork_remove_person_tag(...)`. Reads are SAFE; person/event/petition/signature/tag changes are MODERATE.
- `autopilot_list_contacts(...)`, `autopilot_get_contact(contact_id)`, `autopilot_upsert_contact(...)`, `autopilot_delete_contact(contact_id)`, `autopilot_list_lists(...)`, `autopilot_create_list(name)`, `autopilot_update_contact_list_membership(...)`, and `autopilot_add_contact_to_journey(...)`. Reads are SAFE; contact/list/journey writes are MODERATE.
- `egoi_list_lists(...)`, `egoi_list_contacts(list_id, ...)`, `egoi_get_contact(...)`, `egoi_create_contact(...)`, and `egoi_update_contact(...)`. Reads are SAFE; contact writes are MODERATE.
- `vero_identify_user(...)`, `vero_alias_user(...)`, `vero_update_user_subscription(...)`, `vero_update_user_tags(...)`, and `vero_track_event(...)`. These are MODERATE because they write Vero user or event data.
- `customerio_list_campaigns()`, `customerio_get_campaign(campaign_id)`, `customerio_upsert_customer(...)`, `customerio_track_event(...)`, `customerio_track_anonymous_event(...)`, and `customerio_update_segment(...)`. Campaign reads are SAFE; customer, event, and segment writes are MODERATE.
- `iterable_list_lists()`, `iterable_get_user(identifier, value)`, `iterable_upsert_user(...)`, `iterable_track_event(...)`, and `iterable_update_list_subscribers(...)`. List/user reads are SAFE; user, event, and list membership writes are MODERATE.
- `posthog_capture_event(...)`, `posthog_identify(...)`, `posthog_create_alias(...)`, and `posthog_track_page_or_screen(...)`. All PostHog event writes are MODERATE.
- `segment_identify(...)`, `segment_track(...)`, and `segment_group(...)`. All Segment calls are MODERATE because they emit analytics/customer data.
- `lemlist_list_campaigns(...)`, `lemlist_get_campaign_stats(...)`, `lemlist_list_activities(...)`, `lemlist_get_lead(email)`, `lemlist_create_lead(...)`, `lemlist_remove_lead(...)`, `lemlist_get_team()`, `lemlist_get_team_credits()`, `lemlist_list_unsubscribes(...)`, and `lemlist_update_unsubscribe(...)`. Reads are SAFE; lead and unsubscribe changes are MODERATE.
- `sendy_create_campaign(...)`, `sendy_add_subscriber(...)`, `sendy_get_subscriber_status(...)`, `sendy_count_active_subscribers(list_id)`, and `sendy_update_subscriber_subscription(...)`. Status/count reads are SAFE; campaign/subscriber writes are MODERATE.
- `emelia_list_campaigns(...)`, `emelia_get_campaign(campaign_id)`, `emelia_create_campaign(name)`, `emelia_update_campaign_status(...)`, `emelia_duplicate_campaign(...)`, `emelia_add_contact_to_campaign(...)`, `emelia_list_contact_lists(...)`, and `emelia_add_contact_to_list(...)`. Reads are SAFE; campaign/contact writes are MODERATE.
- `mautic_list_contacts(...)`, `mautic_get_contact(contact_id)`, `mautic_create_contact(...)`, `mautic_update_contact(...)`, `mautic_delete_contact(contact_id)`, `mautic_list_companies(...)`, `mautic_get_company(company_id)`, `mautic_create_company(...)`, `mautic_update_company(...)`, `mautic_delete_company(company_id)`, contact segment/campaign/company membership helpers, and email send helpers. Contact/company reads are SAFE; writes, membership changes, deletes, and sends are MODERATE.

Credential providers and fallback env vars:
- ActiveCampaign: provider `activecampaign`, fields `api_key` and `api_url` / `base_url`; env fallbacks `ACTIVECAMPAIGN_API_KEY` and `ACTIVECAMPAIGN_BASE_URL`.
- ConvertKit: provider `convertkit`, fields `api_secret`, `api_key`, or `value`; env fallback `CONVERTKIT_API_SECRET`.
- GetResponse: provider `getresponse`, fields `api_key`, `access_token`, or `value`; env fallback `GETRESPONSE_API_KEY`.
- MailerLite: provider `mailerlite`, fields `api_key`, `access_token`, or `value`; env fallback `MAILERLITE_API_KEY`. Set `MAILERLITE_CLASSIC_API=true` or credential field `classic_api=true` for Classic API header style.
- Action Network: provider `actionnetwork`, fields `api_key`, `token`, or `value`, optional `base_url`; aliases `action_network`, `actionnetwork_api`, and `action_network_api`; env fallbacks `ACTIONNETWORK_API_KEY` and `ACTIONNETWORK_BASE_URL`.
- Autopilot: provider `autopilot`, field `api_key`, `token`, or `value`, optional `base_url`; env fallbacks `AUTOPILOT_API_KEY` and `AUTOPILOT_BASE_URL`.
- E-goi: provider `egoi`, field `api_key`, `token`, or `value`, optional `base_url`; aliases `e_goi`, `egoi_api`, and `e_goi_api`; env fallbacks `EGOI_API_KEY` and `EGOI_BASE_URL`.
- Vero: provider `vero`, field `auth_token`, `api_key`, `token`, or `value`, optional `base_url`; env fallbacks `VERO_AUTH_TOKEN` and `VERO_BASE_URL`.
- Customer.io: provider `customerio`, fields `tracking_site_id`, `tracking_api_key`, optional `app_api_key`, and optional `region` / base URLs; env fallbacks `CUSTOMERIO_TRACKING_SITE_ID`, `CUSTOMERIO_TRACKING_API_KEY`, `CUSTOMERIO_APP_API_KEY`, `CUSTOMERIO_REGION`, `CUSTOMERIO_TRACKING_BASE_URL`, and `CUSTOMERIO_APP_BASE_URL`.
- Iterable: provider `iterable`, fields `api_key` and optional `base_url`; env fallbacks `ITERABLE_API_KEY` and `ITERABLE_BASE_URL`.
- PostHog: provider `posthog`, fields `api_key` / `project_api_key` and optional `base_url`; env fallbacks `POSTHOG_API_KEY` and `POSTHOG_BASE_URL`.
- Segment: provider `segment`, field `write_key`; env fallbacks `SEGMENT_WRITE_KEY` and `SEGMENT_BASE_URL`.
- Lemlist: provider `lemlist`, fields `api_key` / `apiKey` / `token` / `value`, optional `base_url`; env fallbacks `LEMLIST_API_KEY` and `LEMLIST_BASE_URL`.
- Sendy: provider `sendy`, fields `url` / `base_url` and `api_key` / `apiKey` / `value`; env fallbacks `SENDY_URL`, `SENDY_BASE_URL`, and `SENDY_API_KEY`.
- Emelia: provider `emelia`, fields `api_key` / `apiKey` / `token` / `value`, optional `graphql_url`; env fallbacks `EMELIA_API_KEY` and `EMELIA_GRAPHQL_URL`.
- Mautic: provider `mautic`, fields `base_url` / `url`, plus `access_token` / `token` for bearer auth or `username` plus `password` for basic auth; env fallback supports `MAUTIC_BASE_URL`, `MAUTIC_ACCESS_TOKEN`, `MAUTIC_USERNAME`, and `MAUTIC_PASSWORD`.

### Developer Platform Tools

The developer-platform batch includes:
- `github_get_repository(owner, repo)`, `github_search_repositories(query, sort?, order?, limit?)`, `github_list_issues(owner, repo, state?, labels?, sort?, direction?, since?, limit?, include_pull_requests?)`, `github_get_issue(owner, repo, issue_number)`, `github_list_pull_requests(owner, repo, state?, sort?, direction?, limit?)`, `github_list_releases(owner, repo, limit?)`, and `github_get_release(owner, repo, tag_name)`.
- `gitlab_get_project(project)`, `gitlab_search_projects(query, limit?, simple?)`, `gitlab_list_project_issues(project, state?, labels?, order_by?, sort?, search?, limit?)`, `gitlab_get_project_issue(project, issue_iid)`, `gitlab_list_project_releases(project, order_by?, sort?, limit?)`, `gitlab_get_project_release(project, tag_name)`, and `gitlab_list_user_projects(user_id, limit?)`.
- `graphql_execute_query(query, variables_json?, operation_name?, endpoint?, headers_json?)` for generic GraphQL POST requests. It is MODERATE because it can hit arbitrary endpoints and can run mutations.

GitHub tools use vault provider `github` fields `access_token`, `token`,
`api_key`, or `value`, then `GITHUB_TOKEN`. GitHub Enterprise can be configured
with vault field `base_url`, `api_base_url`, `server`, or env
`GITHUB_API_BASE_URL`.

GitLab tools use vault provider `gitlab` fields `access_token`,
`private_token`, `token`, `api_key`, or `value`, then `GITLAB_TOKEN`. GitLab
self-managed instances can be configured with vault field `base_url`, `server`,
or env `GITLAB_BASE_URL`; plain instance URLs automatically get `/api/v4`
appended.

GraphQL uses vault provider `graphql` fields `endpoint` / `graphql_url` /
`api_url` / `base_url` / `url`, optional `bearer_token` / `access_token` /
`token`, optional `api_key` plus `api_key_header`, and optional `headers_json`.
Env fallback supports `GRAPHQL_ENDPOINT`, `GRAPHQL_BEARER_TOKEN`,
`GRAPHQL_API_KEY`, `GRAPHQL_API_KEY_HEADER`, and `GRAPHQL_HEADERS_JSON`.

### Build And CI Service Tools

This batch includes:
- `circleci_list_pipelines(vcs, project_slug, branch?, limit?, page_token?)`, `circleci_get_pipeline(vcs, project_slug, pipeline_number)`, and `circleci_trigger_pipeline(vcs, project_slug, branch?, tag?, parameters_json?)`.
- `travisci_list_builds(include?, sort_by?, order?, limit?)`, `travisci_get_build(build_id, include?)`, `travisci_trigger_build(slug, branch, message?, merge_mode?, config_json?)`, `travisci_restart_build(build_id)`, and `travisci_cancel_build(build_id)`.
- `jenkins_get_instance(tree?)`, `jenkins_list_jobs(limit?)`, `jenkins_list_job_builds(job_name, limit?)`, `jenkins_trigger_job(job_name)`, `jenkins_trigger_job_with_parameters(job_name, parameters_json)`, `jenkins_copy_job(source_job_name, new_job_name)`, `jenkins_create_job(new_job_name, config_xml)`, `jenkins_quiet_down(reason?)`, `jenkins_cancel_quiet_down()`, `jenkins_restart_instance(mode?)`, and `jenkins_shutdown_instance(mode?)`.

Credential providers and fallback env vars:
- CircleCI: provider `circleci`, fields `api_key`, `api_token`, `token`, or `value`; env fallback `CIRCLECI_API_TOKEN`.
- Travis CI: provider `travisci`, fields `api_token`, `access_token`, `token`, or `value`; env fallback `TRAVISCI_API_TOKEN`.
- Jenkins: provider `jenkins`, fields `base_url`, `username`, and `api_key` / `api_token` / `token`; env fallbacks `JENKINS_BASE_URL`, `JENKINS_USERNAME`, and `JENKINS_API_TOKEN`.

### File Storage Service Tools

This batch includes:
- `dropbox_get_current_account()`, `dropbox_get_metadata(path)`, `dropbox_list_folder(path?, recursive?, include_deleted?, limit?)`, `dropbox_search(query, path?, filename_only?, limit?)`, `dropbox_download_file(path, max_bytes?)`, `dropbox_upload_text_file(path, content, mode?)`, `dropbox_create_folder(path)`, `dropbox_copy_path(from_path, to_path)`, `dropbox_move_path(from_path, to_path)`, and `dropbox_delete_path(path)`.
- `nextcloud_list_folder(path?, depth?)`, `nextcloud_download_file(path, max_bytes?)`, `nextcloud_upload_text_file(path, content)`, `nextcloud_create_folder(path)`, `nextcloud_copy_path(from_path, to_path)`, `nextcloud_move_path(from_path, to_path)`, `nextcloud_delete_path(path)`, `nextcloud_list_users(search?, limit?)`, and `nextcloud_get_user(user_id)`.
- `s3_list_buckets()`, `s3_list_objects(bucket, prefix?, delimiter?, limit?)`, `s3_get_object_text(bucket, key, max_bytes?)`, `s3_upload_text_object(bucket, key, content, content_type?)`, `s3_copy_object(source_bucket, source_key, destination_bucket, destination_key)`, `s3_delete_object(bucket, key)`, `s3_create_folder(bucket, folder_key)`, `s3_create_bucket(bucket, region?)`, and `s3_delete_bucket(bucket)`.

Credential providers and fallback env vars:
- Dropbox: provider `dropbox`, fields `access_token`, `token`, or `value`; env fallback `DROPBOX_ACCESS_TOKEN`.
- Nextcloud: provider `nextcloud`, field `webdav_url` plus either `username` and `password` or `access_token`; env fallbacks `NEXTCLOUD_WEBDAV_URL`, `NEXTCLOUD_USERNAME`, `NEXTCLOUD_PASSWORD`, and `NEXTCLOUD_ACCESS_TOKEN`.
- S3: provider `s3`, fields `access_key_id`, `secret_access_key`, optional `session_token`, `region`, `endpoint_url`, and `force_path_style`; env fallbacks `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_REGION`, `AWS_ENDPOINT_URL_S3`, and `S3_FORCE_PATH_STYLE`.

### AWS Service Tools

This batch includes:
- `aws_lambda_list_functions(region_name?, marker?, limit?)` and `aws_lambda_invoke(function_name, payload_json?, qualifier?, invocation_type?, log_type?, region_name?)`.
- `aws_sns_list_topics(region_name?, next_token?)`, `aws_sns_create_topic(name, attributes_json?, region_name?)`, `aws_sns_publish(topic_arn, message, subject?, message_attributes_json?, message_group_id?, message_deduplication_id?, region_name?)`, and `aws_sns_delete_topic(topic_arn, region_name?)`.
- `aws_ses_send_email(source, to_addresses, subject, text_body?, html_body?, cc_addresses?, bcc_addresses?, reply_to_addresses?, region_name?)`, `aws_ses_list_identities(identity_type?, next_token?, limit?, region_name?)`, `aws_ses_verify_email_identity(email_address, region_name?)`, `aws_ses_list_templates(next_token?, limit?, region_name?)`, `aws_ses_get_template(template_name, region_name?)`, `aws_ses_create_template(template_name, subject_part, text_part?, html_part?, region_name?)`, `aws_ses_update_template(template_name, subject_part, text_part?, html_part?, region_name?)`, and `aws_ses_delete_template(template_name, region_name?)`.
- `aws_textract_analyze_expense(document_base64, simplify?, region_name?)` for receipt/invoice image or PDF bytes.
- `aws_transcribe_start_job(job_name, media_file_uri, language_code?, detect_language?, output_bucket?, output_key?, settings_json?, region_name?)`, `aws_transcribe_get_job(job_name, region_name?)`, `aws_transcribe_list_jobs(status?, job_name_contains?, next_token?, limit?, region_name?)`, and `aws_transcribe_delete_job(job_name, region_name?)`.

Credential providers and fallback env vars:
- AWS services: provider `aws`, fields `access_key_id`, `secret_access_key`, optional `session_token`, `region`, and `endpoint_url`; aliases `amazon_web_services`, `s3`, and `aws_s3` are accepted so saved S3 connections can be reused. Env fallback supports `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, and `AWS_REGION`.

### Business And Language Service Tools

This batch includes:
- `bitly_get_bitlink(bitlink_id)`, `bitly_create_bitlink(long_url, title?, domain?, group_guid?, tags?)`, and `bitly_update_bitlink(bitlink_id, long_url?, title?, archived?, group_guid?, tags?)`. Create/update are MODERATE because they change Bitly state.
- `brandfetch_get_brand(domain)`, `brandfetch_get_brand_logos(domain)`, and `brandfetch_get_brand_colors(domain)`.
- `marketstack_get_eod(symbols, latest?, date?, date_from?, date_to?, limit?)`, `marketstack_get_ticker(symbol)`, and `marketstack_get_exchange(exchange)`.
- `deepl_translate_text(text, target_lang, source_lang?, formality?, preserve_formatting?)` and `deepl_list_languages(language_type?)`. Translation is MODERATE because it sends user text to DeepL and consumes quota.
- `lingvanex_translate_text(text, target_lang, source_lang?, platform?, translate_mode?)` and `lingvanex_list_languages()`. Translation is MODERATE because it sends user text to LingvaNex and consumes quota.
- `apitemplate_list_templates(template_type?)`, `apitemplate_get_account()`, `apitemplate_create_image(template_id, overrides_json?)`, and `apitemplate_create_pdf(template_id, properties_json)`. Create operations are MODERATE because they generate billable remote artifacts.
- `onesimple_create_pdf(url, ...)`, `onesimple_create_screenshot(url, ...)`, `onesimple_get_page_info(url, include_headers?)`, `onesimple_get_exchange_rate(value, from_currency, to_currency)`, `onesimple_get_image_metadata(image_url)`, `onesimple_validate_email(email)`, `onesimple_expand_url(url)`, and `onesimple_create_qr_code(content, ...)`. These are MODERATE because they send user-provided URLs, content, or addresses to an external API.
- `dhl_track_shipment(tracking_number, recipient_postal_code?)` for shipment tracking.
- `onfleet_test_auth()`, `onfleet_list_tasks(...)`, `onfleet_get_task(task_id)`, `onfleet_list_workers(...)`, `onfleet_get_worker(worker_id)`, `onfleet_list_teams(limit?)`, `onfleet_get_team(team_id)`, and `onfleet_complete_task(task_id, ...)`. Reads are SAFE; force-complete is MODERATE because it changes dispatch task state.
- `phantombuster_list_agents(limit?)`, `phantombuster_get_agent(agent_id)`, `phantombuster_get_agent_output(agent_id, ...)`, `phantombuster_launch_agent(agent_id, ...)`, and `phantombuster_delete_agent(agent_id)`. Launch/delete are MODERATE because they run or remove remote automation agents.

Credential providers and fallback env vars:
- Bitly: provider `bitly`, fields `access_token`, `token`, `api_key`, or `value`; env fallback `BITLY_TOKEN`.
- Brandfetch: provider `brandfetch`, fields `api_key`, `token`, or `value`; env fallback `BRANDFETCH_API_KEY`.
- Marketstack: provider `marketstack`, fields `api_key`, `access_key`, `token`, or `value`; env fallback `MARKETSTACK_API_KEY`.
- DeepL: provider `deepl`, fields `api_key`, `auth_key`, `token`, or `value`; env fallback `DEEPL_API_KEY`. Use `api_plan` / `plan` or `DEEPL_API_PLAN=free` for the free endpoint.
- LingvaNex: provider `lingvanex`, fields `api_key`, `access_token`, `token`, or `value`; env fallback `LINGVANEX_API_KEY`. Use `base_url` / `url` or `LINGVANEX_BASE_URL` for non-default API roots.
- APITemplate: provider `apitemplate`, fields `api_key`, `token`, or `value`; env fallback `APITEMPLATE_API_KEY`. Use `base_url` / `url` or `APITEMPLATE_BASE_URL` for non-default API roots.
- One Simple API: provider `onesimple`, fields `api_token`, `api_key`, `token`, or `value`; env fallback `ONESIMPLE_API_TOKEN`. Use `base_url` / `url` or `ONESIMPLE_BASE_URL` for non-default API roots.
- DHL: provider `dhl`, fields `api_key` / `apiKey` / `value`; env fallback `DHL_API_KEY`. Use `base_url` / `url` or `DHL_BASE_URL` for non-default API roots.
- Onfleet: provider `onfleet`, fields `api_key` / `apiKey` / `token` / `value`; env fallback `ONFLEET_API_KEY`. Use `base_url` / `url` or `ONFLEET_BASE_URL` for non-default API roots.
- Phantombuster: provider `phantombuster`, fields `api_key` / `apiKey` / `token` / `value`; env fallback `PHANTOMBUSTER_API_KEY`. Use `base_url` / `url` or `PHANTOMBUSTER_BASE_URL` for non-default API roots.

### Productivity Service Tools

This batch includes:
- `todoist_list_tasks(project_id?, section_id?, label?, filter_query?, limit?)`, `todoist_get_task(task_id)`, `todoist_create_task(...)`, `todoist_update_task(...)`, `todoist_close_task(task_id)`, `todoist_list_projects(limit?)`, `todoist_get_project(project_id)`, and `todoist_create_project(...)`. Create/update/close are MODERATE because they change Todoist state.
- `trello_search(query, model_types?, limit?, partial?)`, `trello_get_board(board_id, fields?)`, `trello_list_board_lists(board_id, filter_value?, limit?)`, `trello_list_cards(list_id, filter_value?, limit?)`, `trello_get_card(card_id, fields?)`, `trello_create_card(...)`, `trello_update_card(...)`, and `trello_add_card_comment(card_id, text)`. Create/update/comment are MODERATE because they change Trello state.

Credential providers and fallback env vars:
- Todoist: provider `todoist`, fields `api_key`, `access_token`, `token`, or `value`; env fallback `TODOIST_API_KEY`. Use `base_url` / `url` or `TODOIST_BASE_URL` for non-default API roots.
- Trello: provider `trello`, fields `api_key` / `key` and `api_token` / `token` / `value`; env fallback `TRELLO_API_KEY` plus `TRELLO_API_TOKEN`. Use `base_url` / `url` or `TRELLO_BASE_URL` for non-default API roots.

### Bookmark Link Service Tools

This batch includes:
- `raindrop_list_bookmarks(...)`, `raindrop_get_bookmark(bookmark_id)`, `raindrop_list_collections(...)`, `raindrop_get_collection(collection_id)`, `raindrop_list_tags(collection_id?)`, and `raindrop_get_user(user_id?)` for SAFE Raindrop bookmark, collection, tag, and user reads.
- `raindrop_create_bookmark(...)`, `raindrop_update_bookmark(bookmark_id, fields_json)`, `raindrop_delete_bookmark(bookmark_id)`, and `raindrop_delete_tags(tags, collection_id?)`. These are MODERATE because they change saved bookmarks or tag state.
- `yourls_shorten_url(...)`, `yourls_expand_url(short_url)`, `yourls_get_url_stats(short_url)`, and `yourls_get_db_stats()`. Shortening is MODERATE because it creates a server-side short link; expand/stats are SAFE.

Credential providers and fallback env vars:
- Raindrop: provider `raindrop`, fields `access_token`, `token`, or `value`; env fallback `RAINDROP_ACCESS_TOKEN`. Use `base_url` / `url` or `RAINDROP_BASE_URL` for non-default API roots.
- YOURLS: provider `yourls`, field `url` for the site root or `yourls-api.php` URL, plus `signature`; alternatively use `username` plus `password`. Env fallback supports `YOURLS_URL`, `YOURLS_SIGNATURE`, `YOURLS_USERNAME`, and `YOURLS_PASSWORD`.

### Work Tracking Service Tools

This batch includes:
- `asana_get_user(user_gid?)`, `asana_list_users(workspace_gid, limit?)`, `asana_list_projects(workspace_gid?, team_gid?, archived?, limit?)`, `asana_get_project(project_gid, opt_fields?)`, `asana_create_project(...)`, `asana_update_project(...)`, `asana_list_tasks(...)`, `asana_search_tasks(...)`, `asana_get_task(task_gid, opt_fields?)`, `asana_create_task(...)`, `asana_create_subtask(...)`, `asana_update_task(...)`, and `asana_add_task_comment(task_gid, text, is_html?)`. Create/update/comment operations are MODERATE because they change Asana state.
- `linear_list_teams(limit?)`, `linear_list_users(limit?)`, `linear_list_workflow_states(team_id?, limit?)`, `linear_list_issues(team_id?, assignee_id?, state_id?, limit?)`, `linear_get_issue(issue_id)`, `linear_create_issue(...)`, `linear_update_issue(...)`, `linear_add_issue_comment(issue_id, body, parent_id?)`, and `linear_add_issue_link(issue_id, url)`. Create/update/comment/link operations are MODERATE because they change Linear state.

Credential providers and fallback env vars:
- Asana: provider `asana`, fields `access_token`, `api_key`, `token`, or `value`; env fallback `ASANA_ACCESS_TOKEN`. Use `base_url` / `url` or `ASANA_BASE_URL` for non-default API roots.
- Linear: provider `linear`, fields `api_key`, `access_token`, `token`, or `value`; env fallback `LINEAR_API_KEY`. Use `api_url` / `graphql_url` / `base_url` / `url` or `LINEAR_API_URL` for non-default GraphQL endpoints.

### Project Management Service Tools

This batch includes:
- `jira_get_myself()`, `jira_list_projects(query?, limit?)`, `jira_search_issues(jql, fields?, limit?)`, `jira_get_issue(issue_key, fields?, expand?)`, `jira_create_issue(...)`, `jira_update_issue(...)`, `jira_list_issue_transitions(issue_key)`, `jira_list_users(query, limit?)`, `jira_list_issue_comments(issue_key, limit?)`, and `jira_add_issue_comment(issue_key, body)`. Create/update/comment operations are MODERATE because they change Jira state.
- `clickup_list_teams()`, `clickup_list_spaces(team_id, archived?)`, `clickup_list_folders(space_id, archived?)`, `clickup_list_lists(folder_id?, space_id?, archived?)`, `clickup_get_task(task_id, include_subtasks?, include_markdown_description?)`, `clickup_list_tasks(...)`, `clickup_create_task(...)`, `clickup_update_task(...)`, `clickup_list_task_comments(task_id)`, and `clickup_add_task_comment(task_id, comment_text, notify_all?)`. Create/update/comment operations are MODERATE because they change ClickUp state.
- `monday_get_me()`, `monday_list_boards(limit?, page?)`, `monday_get_board(board_id)`, `monday_create_board(...)`, `monday_archive_board(board_id)`, `monday_list_board_columns(board_id)`, `monday_create_board_column(...)`, `monday_list_board_groups(board_id)`, `monday_create_board_group(...)`, `monday_list_items(...)`, `monday_get_item(item_ids)`, `monday_create_item(...)`, `monday_update_item_columns(...)`, `monday_add_item_update(item_id, body)`, `monday_move_item(item_id, group_id)`, and `monday_delete_item(item_id)`. Create/archive/update/move/delete operations are MODERATE because they change Monday state.
- `taiga_list_projects(query?, member_id?, limit?)`, `taiga_list_records(resource, ...)`, `taiga_get_record(resource, record_id)`, `taiga_create_record(...)`, `taiga_update_record(...)`, and `taiga_delete_record(resource, record_id)`. Create/update/delete operations are MODERATE because they change Taiga state.
- `wekan_get_current_user()`, `wekan_list_users()`, `wekan_list_user_boards(user_id?, limit?)`, `wekan_get_board(board_id)`, `wekan_create_board(...)`, `wekan_delete_board(board_id)`, `wekan_list_lists(board_id, limit?)`, `wekan_create_list(board_id, title)`, `wekan_delete_list(board_id, list_id)`, `wekan_list_cards(...)`, `wekan_get_card(...)`, `wekan_create_card(...)`, `wekan_update_card(...)`, `wekan_delete_card(...)`, `wekan_list_card_comments(board_id, card_id, limit?)`, and `wekan_add_card_comment(...)`. Create/update/delete/comment operations are MODERATE because they change Wekan state.

Credential providers and fallback env vars:
- Jira: provider `jira`, fields `access_token`, `bearer_token`, `token`, or `value` for bearer auth; or `email` / `username` plus `api_token` / `apiToken` / `password` for basic auth. Use `base_url` / `domain` / `url` / `site_url` or `JIRA_BASE_URL` for the Jira Cloud site, such as `https://example.atlassian.net`. Env fallback supports `JIRA_ACCESS_TOKEN` or `JIRA_EMAIL` plus `JIRA_API_TOKEN`.
- ClickUp: provider `clickup`, fields `access_token`, `api_key`, `token`, or `value`; env fallback `CLICKUP_ACCESS_TOKEN`. Use `base_url` / `url` or `CLICKUP_BASE_URL` for non-default API roots.
- Monday: provider `monday`, fields `api_token`, `apiToken`, `access_token`, `token`, or `value`; env fallback `MONDAY_API_TOKEN`. Use `api_url` / `graphql_url` / `base_url` / `url` or `MONDAY_API_URL` for non-default GraphQL endpoints.
- Taiga: provider `taiga`, fields `auth_token`, `access_token`, `bearer_token`, `token`, or `value`; env fallback `TAIGA_AUTH_TOKEN`. Username/password login is also supported with `username` plus `password`, or `TAIGA_USERNAME` plus `TAIGA_PASSWORD`. Use `api_url` / `base_url` / `url` or `TAIGA_BASE_URL` for self-hosted instances.
- Wekan: provider `wekan`, fields `session_token`, `token`, `access_token`, `bearer_token`, or `value`; env fallback `WEKAN_TOKEN`. Username/password login is also supported with `username` plus `password`, or `WEKAN_USERNAME` plus `WEKAN_PASSWORD`. Use `base_url` / `url` or `WEKAN_BASE_URL` for the Wekan instance root.

### Enterprise Business Service Tools

This batch includes:
- `erpnext_get_logged_user()`, `erpnext_list_documents(doc_type, fields?, filters_json?, limit?)`, `erpnext_get_document(doc_type, document_name)`, `erpnext_create_document(doc_type, fields_json)`, `erpnext_update_document(doc_type, document_name, fields_json)`, and `erpnext_delete_document(doc_type, document_name)`. Create/update/delete operations are MODERATE because they change ERPNext records.
- `odoo_get_server_version()`, `odoo_list_records(model, fields?, filters_json?, limit?)`, `odoo_get_record(model, record_id, fields?)`, `odoo_create_record(model, fields_json)`, `odoo_update_record(model, record_id, fields_json)`, and `odoo_delete_record(model, record_id)`. Create/update/delete operations are MODERATE because they change Odoo records.
- `invoiceninja_list_records(resource, include?, status?, filters_json?, limit?)`, `invoiceninja_get_record(resource, record_id, include?)`, `invoiceninja_create_record(resource, fields_json, query_json?)`, `invoiceninja_delete_record(resource, record_id)`, and `invoiceninja_email_invoice_or_quote(resource, record_id)`. Create/delete/email operations are MODERATE because they change or send Invoice Ninja records.

Credential providers and fallback env vars:
- ERPNext: provider `erpnext`, fields `api_key` / `apiKey` plus `api_secret` / `apiSecret`; env fallbacks `ERPNEXT_API_KEY` and `ERPNEXT_API_SECRET`. Use `base_url` / `url` / `domain` or `ERPNEXT_BASE_URL`; cloud-hosted subdomain/domain fallback is also supported with `ERPNEXT_SUBDOMAIN` and `ERPNEXT_CLOUD_DOMAIN`.
- Odoo: provider `odoo`, fields `url`, `username`, `password` / `api_key`, and `database` / `db`; env fallbacks `ODOO_URL`, `ODOO_USERNAME`, `ODOO_PASSWORD`, and `ODOO_DATABASE`.
- Invoice Ninja: provider `invoiceninja`, fields `api_token` / `apiToken` / `token`, optional `secret`, and optional `base_url` / `url`; env fallbacks `INVOICENINJA_API_TOKEN`, `INVOICENINJA_SECRET`, `INVOICENINJA_BASE_URL`, and `INVOICENINJA_API_VERSION`.

### Event And Meeting Service Tools

This batch includes:
- `demio_list_events(event_type?, limit?)`, `demio_get_event(event_id, active?, date_id?)`, `demio_register_event(event_id, name, email, fields_json?)`, and `demio_get_session_participants(date_id, status?, limit?)`. Registration is MODERATE because it creates an attendee registration.
- `zoom_list_meetings(meeting_type?, limit?)`, `zoom_get_meeting(meeting_id, occurrence_id?, show_previous_occurrences?)`, `zoom_create_meeting(topic, fields_json?)`, `zoom_update_meeting(meeting_id, fields_json)`, and `zoom_delete_meeting(meeting_id, occurrence_id?, schedule_for_reminder?)`. Create/update/delete operations are MODERATE because they change Zoom meetings.
- `gotowebinar_list_webinars(from_time?, to_time?, limit?)`, `gotowebinar_get_webinar(webinar_key)`, `gotowebinar_create_webinar(subject, times_json, fields_json?)`, `gotowebinar_update_webinar(webinar_key, fields_json, notify_participants?)`, `gotowebinar_list_sessions(webinar_key?, from_time?, to_time?, limit?)`, `gotowebinar_get_session(webinar_key, session_key)`, `gotowebinar_list_registrants(webinar_key, limit?)`, `gotowebinar_get_registrant(webinar_key, registrant_key)`, `gotowebinar_create_registrant(...)`, and `gotowebinar_delete_registrant(webinar_key, registrant_key)`. Create/update/delete operations are MODERATE because they change webinars or registrations.

Credential providers and fallback env vars:
- Demio: provider `demio`, fields `api_key` / `apiKey` plus `api_secret` / `apiSecret`; env fallbacks `DEMIO_API_KEY` and `DEMIO_API_SECRET`. Use `base_url` / `api_url` / `url` or `DEMIO_BASE_URL` for non-default API roots.
- Zoom: provider `zoom`, fields `access_token`, `bearer_token`, `token`, or `value`; env fallback `ZOOM_ACCESS_TOKEN`. Use `base_url` / `api_url` / `url` or `ZOOM_BASE_URL` for non-default API roots.
- GoToWebinar: provider `gotowebinar`, fields `access_token`, `account_key`, and `organizer_key`; env fallbacks `GOTOWEBINAR_ACCESS_TOKEN`, `GOTOWEBINAR_ACCOUNT_KEY`, and `GOTOWEBINAR_ORGANIZER_KEY`. Use `base_url` / `api_url` / `url` or `GOTOWEBINAR_BASE_URL` for non-default API roots.

### Collaboration And Data Service Tools

This batch includes:
- `slack_list_channels(types?, exclude_archived?, limit?)`, `slack_get_channel_history(channel_id, ...)`, `slack_search_messages(query, ...)`, `slack_list_users(...)`, `slack_get_user(user_id)`, `slack_post_message(...)`, `slack_update_message(...)`, and `slack_add_reaction(...)`. Post/update/reaction operations are MODERATE because they change Slack state.
- `notion_search(query?, object_type?, limit?)`, `notion_get_page(page_id)`, `notion_get_block_children(block_id, ...)`, `notion_query_data_source(...)`, `notion_create_page(...)`, `notion_update_page(...)`, and `notion_append_block_children(...)`. Create/update/append operations are MODERATE because they change Notion content.
- `airtable_list_bases()`, `airtable_get_base_schema(base_id)`, `airtable_list_records(...)`, `airtable_get_record(...)`, `airtable_create_records(...)`, `airtable_update_records(...)`, and `airtable_delete_record(...)`. Create/update/delete operations are MODERATE because they change Airtable data.

Credential providers and fallback env vars:
- Slack: provider `slack`, fields `bot_token`, `access_token`, `token`, or `value`; env fallback `SLACK_BOT_TOKEN` or `SLACK_ACCESS_TOKEN`. Use `base_url` / `url` or `SLACK_BASE_URL` for non-default API roots.
- Notion: provider `notion`, fields `api_key`, `access_token`, `token`, or `value`; env fallback `NOTION_API_KEY`. Use `notion_version` / `version` or `NOTION_VERSION` for the Notion API version; default is `2026-03-11`.
- Airtable: provider `airtable`, fields `access_token`, `api_key`, `token`, or `value`; env fallback `AIRTABLE_ACCESS_TOKEN` or `AIRTABLE_API_KEY`. Use `base_url` / `url` or `AIRTABLE_BASE_URL` for non-default API roots.

### Customer Engagement Service Tools

This batch includes:
- `hubspot_list_crm_objects(...)`, `hubspot_search_crm_objects(...)`, `hubspot_get_crm_object(...)`, `hubspot_create_crm_object(...)`, `hubspot_update_crm_object(...)`, and `hubspot_archive_crm_object(...)`. Create/update/archive operations are MODERATE because they change HubSpot CRM data.
- `zendesk_search(...)`, `zendesk_get_ticket(ticket_id)`, `zendesk_list_tickets(...)`, `zendesk_create_ticket(...)`, `zendesk_update_ticket(...)`, `zendesk_get_user(user_id)`, and `zendesk_search_users(query, ...)`. Create/update operations are MODERATE because they change Zendesk support data.
- `mailchimp_list_audiences(...)`, `mailchimp_list_members(...)`, `mailchimp_get_member(...)`, `mailchimp_add_or_update_member(...)`, `mailchimp_update_member_tags(...)`, and `mailchimp_list_campaigns(...)`. Member add/update and tag changes are MODERATE because they change Mailchimp audience data.

Credential providers and fallback env vars:
- HubSpot: provider `hubspot`, fields `private_app_token`, `app_token`, `access_token`, `token`, or `value`; env fallback `HUBSPOT_ACCESS_TOKEN`. Use `base_url` / `url` or `HUBSPOT_BASE_URL` for non-default API roots.
- Zendesk: provider `zendesk`, fields `access_token`, `token`, or `value` for bearer auth; or `email` plus `api_token` / `apiToken` / `password` and `subdomain` / `base_url` for API-token auth. Env fallback supports `ZENDESK_ACCESS_TOKEN` or `ZENDESK_EMAIL` plus `ZENDESK_API_TOKEN` and `ZENDESK_SUBDOMAIN`.
- Mailchimp: provider `mailchimp`, fields `api_key`, `access_token`, `token`, or `value`; env fallback `MAILCHIMP_API_KEY` or `MAILCHIMP_ACCESS_TOKEN`. Use `server_prefix` / `dc` / `data_center` or `MAILCHIMP_SERVER_PREFIX`; API keys ending in `-usX` derive the server prefix automatically.

### Support Service Tools

This batch includes:
- `freshdesk_list_tickets(...)`, `freshdesk_search_tickets(...)`, `freshdesk_get_ticket(ticket_id)`, `freshdesk_create_ticket(...)`, `freshdesk_update_ticket(...)`, `freshdesk_delete_ticket(ticket_id)`, `freshdesk_list_contacts(...)`, `freshdesk_get_contact(contact_id)`, `freshdesk_create_contact(...)`, and `freshdesk_update_contact(...)`. Create/update/delete operations are MODERATE because they change Freshdesk support data.
- `freshservice_list_tickets(...)`, `freshservice_get_ticket(ticket_id)`, `freshservice_create_ticket(...)`, `freshservice_update_ticket(...)`, `freshservice_list_requesters(...)`, and `freshservice_get_requester(requester_id)`. Create/update operations are MODERATE because they change Freshservice support data.
- `helpscout_list_mailboxes(...)`, `helpscout_get_mailbox(mailbox_id)`, `helpscout_list_conversations(...)`, `helpscout_get_conversation(conversation_id)`, `helpscout_create_conversation(...)`, `helpscout_create_thread(...)`, `helpscout_list_customers(...)`, `helpscout_get_customer(customer_id)`, `helpscout_create_customer(...)`, and `helpscout_update_customer(...)`. Create/update/thread operations are MODERATE because they change Help Scout inbox data.
- `intercom_list_contacts(...)`, `intercom_search_contacts(...)`, `intercom_get_contact(contact_id)`, `intercom_create_contact(...)`, `intercom_update_contact(...)`, `intercom_archive_contact(contact_id)`, `intercom_list_conversations(...)`, `intercom_get_conversation(conversation_id)`, and `intercom_reply_conversation(...)`. Create/update/archive/reply operations are MODERATE because they change Intercom workspace data or send customer-facing/admin messages.
- `drift_get_contact(contact_id)`, `drift_create_contact(...)`, `drift_update_contact(...)`, `drift_delete_contact(contact_id)`, and `drift_list_contact_attributes()`. Contact reads and attribute listing are SAFE; create/update/delete operations are MODERATE because they change Drift contacts.
- `servicenow_list_records(table, ...)`, `servicenow_get_record(table, sys_id, ...)`, `servicenow_create_record(table, fields_json)`, `servicenow_update_record(table, sys_id, fields_json)`, and `servicenow_delete_record(table, sys_id)` for ServiceNow table records such as incidents and users. Create/update/delete operations are MODERATE.
- `zammad_list_records(resource, ...)`, `zammad_get_record(resource, record_id)`, `zammad_create_record(resource, fields_json)`, and `zammad_update_record(resource, record_id, fields_json)` for Zammad tickets, users, organizations, and groups. Create/update operations are MODERATE.

Credential providers and fallback env vars:
- Freshdesk: provider `freshdesk`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `FRESHDESK_API_KEY`. Use `domain` / `subdomain` or `FRESHDESK_DOMAIN`, or `base_url` / `url` / `FRESHDESK_BASE_URL` for the full API root.
- Freshservice: provider `freshservice`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `FRESHSERVICE_API_KEY`. Use `domain` / `subdomain` or `FRESHSERVICE_DOMAIN`, or `base_url` / `url` / `FRESHSERVICE_BASE_URL` for the full API root.
- Help Scout: provider `helpscout`, fields `access_token`, `token`, or `value`; env fallback `HELPSCOUT_ACCESS_TOKEN`. Use `base_url` / `url` or `HELPSCOUT_BASE_URL` for non-default API roots.
- Intercom: provider `intercom`, fields `access_token`, `api_key`, `token`, or `value`; env fallback `INTERCOM_ACCESS_TOKEN`. Use `base_url` / `url` or `INTERCOM_BASE_URL` for regional API roots; `intercom_version` / `version` or `INTERCOM_VERSION` overrides the API version header.
- Drift: provider `drift`, fields `access_token`, `accessToken`, `api_key`, `token`, or `value`; env fallback `DRIFT_ACCESS_TOKEN`. Use `base_url` / `url` or `DRIFT_BASE_URL` for non-default API roots.
- ServiceNow: provider `servicenow`, fields `access_token`, `token`, or `value` for bearer auth; or `username` plus `password` for basic auth. Use `base_url` / `url` or `instance` / `subdomain`; env fallback supports `SERVICENOW_BASE_URL`, `SERVICENOW_INSTANCE`, `SERVICENOW_ACCESS_TOKEN`, `SERVICENOW_USERNAME`, and `SERVICENOW_PASSWORD`.
- Zammad: provider `zammad`, fields `token`, `api_token`, or `value` for token auth; or `username` plus `password` for basic auth. Use `base_url` / `url`; env fallback supports `ZAMMAD_BASE_URL`, `ZAMMAD_TOKEN`, `ZAMMAD_USERNAME`, and `ZAMMAD_PASSWORD`.

### Sales CRM Service Tools

This batch includes:
- `salesforce_query_records(soql, ...)` and `salesforce_get_record(object_name, record_id, ...)` for SAFE Salesforce reads across standard and custom objects. `salesforce_create_record(...)`, `salesforce_update_record(...)`, and `salesforce_delete_record(...)` are MODERATE because they change Salesforce data.
- `zoho_crm_list_records(resource, ...)`, `zoho_crm_search_records(resource, ...)`, and `zoho_crm_get_record(resource, record_id, ...)` for SAFE Zoho CRM reads across accounts, contacts, deals, leads, products, invoices, quotes, orders, and vendors. `zoho_crm_create_records(...)`, `zoho_crm_update_record(...)`, and `zoho_crm_delete_record(...)` are MODERATE.
- `freshworks_crm_list_records(resource, ...)`, `freshworks_crm_search_records(term, ...)`, and `freshworks_crm_get_record(resource, record_id)` for SAFE Freshworks CRM reads across accounts, contacts, deals, tasks, appointments, notes, and sales activities. `freshworks_crm_create_record(...)`, `freshworks_crm_update_record(...)`, and `freshworks_crm_delete_record(...)` are MODERATE.
- `salesmate_list_users()`, `salesmate_search_records(resource, ...)`, and `salesmate_get_record(resource, record_id)` for SAFE Salesmate reads across companies, contacts, deals, and activities. `salesmate_create_record(...)`, `salesmate_update_record(...)`, and `salesmate_delete_record(...)` are MODERATE.
- `pipedrive_list_records(resource, ...)`, `pipedrive_search_records(resource, term, ...)`, and `pipedrive_get_record(resource, record_id)` for SAFE read access across deals, persons/people, organizations, activities, leads, notes, and products where supported by Pipedrive.
- `pipedrive_create_record(resource, fields_json)`, `pipedrive_update_record(resource, record_id, fields_json)`, and `pipedrive_delete_record(resource, record_id)`. These are MODERATE because they change Pipedrive CRM data.
- `pipedrive_list_users(...)` for resolving owner/user IDs.

Credential providers and fallback env vars:
- Salesforce: provider `salesforce`, fields `access_token` plus `instance_url`; env fallbacks `SALESFORCE_ACCESS_TOKEN`, `SALESFORCE_INSTANCE_URL`, optional `SALESFORCE_BASE_URL`, and `SALESFORCE_API_VERSION`.
- Zoho CRM: provider `zoho_crm`, fields `access_token` and optional `api_domain` / `base_url`; env fallbacks `ZOHO_CRM_ACCESS_TOKEN`, `ZOHO_CRM_API_DOMAIN`, and `ZOHO_CRM_BASE_URL`.
- Freshworks CRM: provider `freshworks_crm`, fields `api_key` plus `domain`, or `base_url`; env fallbacks `FRESHWORKS_CRM_API_KEY`, `FRESHWORKS_CRM_DOMAIN`, and `FRESHWORKS_CRM_BASE_URL`.
- Salesmate: provider `salesmate`, fields `session_token` plus `link_name`; env fallbacks `SALESMATE_SESSION_TOKEN`, `SALESMATE_LINK_NAME`, and `SALESMATE_BASE_URL`.
- Pipedrive: provider `pipedrive`, fields `api_token`, `apiToken`, `token`, or `value` for API-token auth; or `access_token` / `bearer_token` for OAuth bearer auth. Env fallback supports `PIPEDRIVE_API_TOKEN` or `PIPEDRIVE_ACCESS_TOKEN`. Use `base_url` / `url` or `PIPEDRIVE_BASE_URL` for non-default API roots.

### Relationship CRM Service Tools

This batch includes:
- `copper_list_records(resource, ...)`, `copper_get_record(resource, record_id)`, `copper_create_record(resource, fields_json)`, `copper_update_record(resource, record_id, fields_json)`, and `copper_delete_record(resource, record_id)` for Copper companies, people, leads, opportunities, projects, tasks, users, and customer sources. Reads are SAFE; create/update/delete operations are MODERATE.
- `agilecrm_list_records(resource, ...)`, `agilecrm_get_record(resource, record_id)`, `agilecrm_create_record(resource, fields_json)`, `agilecrm_update_record(resource, record_id, fields_json)`, and `agilecrm_delete_record(resource, record_id)` for Agile CRM contacts, companies, and deals. Reads are SAFE; create/update/delete operations are MODERATE.
- `monica_list_records(resource, ...)`, `monica_get_record(resource, record_id)`, `monica_create_record(resource, fields_json)`, `monica_update_record(resource, record_id, fields_json)`, and `monica_delete_record(resource, record_id)` for Monica CRM contacts, activities, calls, notes, reminders, tags, tasks, and related records. Reads are SAFE; create/update/delete operations are MODERATE.
- `affinity_list_records(resource, ...)`, `affinity_get_record(resource, record_id)`, `affinity_create_person(...)`, `affinity_create_organization(...)`, `affinity_update_record(resource, record_id, fields_json)`, `affinity_delete_record(resource, record_id)`, `affinity_list_entries(list_id, ...)`, `affinity_create_list_entry(...)`, and `affinity_delete_list_entry(list_id, list_entry_id)` for Affinity people, organizations, lists, and list entries. Reads are SAFE; create/update/delete operations are MODERATE.
- `keap_list_records(resource, ...)`, `keap_get_record(resource, record_id)`, `keap_create_record(resource, fields_json)`, `keap_update_note(note_id, fields_json)`, `keap_delete_record(resource, record_id)`, `keap_list_contact_tags(contact_id)`, `keap_apply_tags(contact_id, tag_ids_csv)`, `keap_remove_tags(contact_id, tag_ids_csv)`, and `keap_send_email(...)` for Keap CRM, note, tag, ecommerce, email, and file records. Reads are SAFE; create/update/delete/tag/send operations are MODERATE.

Credential providers and fallback env vars:
- Copper: provider `copper`, fields `api_key` and `email`, optional `base_url`; env fallbacks `COPPER_API_KEY`, `COPPER_EMAIL`, and `COPPER_BASE_URL`.
- Agile CRM: provider `agilecrm`, fields `email`, `api_key`, and `subdomain`, optional `base_url`; env fallbacks `AGILECRM_EMAIL`, `AGILECRM_API_KEY`, `AGILECRM_SUBDOMAIN`, and `AGILECRM_BASE_URL`.
- Monica CRM: provider `monica`, fields `api_token`, `access_token`, or `value`, optional `base_url`; env fallbacks `MONICA_ACCESS_TOKEN` and `MONICA_BASE_URL`.
- Affinity: provider `affinity`, fields `api_key`, `token`, or `value`, optional `base_url`; env fallbacks `AFFINITY_API_KEY` and `AFFINITY_BASE_URL`.
- Keap: provider `keap`, fields `access_token`, `bearer_token`, `token`, or `value`, optional `base_url`; env fallbacks `KEAP_ACCESS_TOKEN` and `KEAP_BASE_URL`.

### Messaging Delivery Service Tools

This batch includes:
- `twilio_send_message(...)`, `twilio_list_messages(...)`, `twilio_get_message(message_sid)`, and `twilio_make_call(...)`. Send/call operations are MODERATE because they contact external recipients and can consume telecom spend.
- `sendgrid_send_email(...)`, `sendgrid_list_contacts(...)`, `sendgrid_get_contact(contact_id)`, `sendgrid_upsert_contacts(...)`, and `sendgrid_list_lists(...)`. Send/upsert operations are MODERATE because they send email or change marketing-contact data.
- `mailgun_send_email(...)`, `mailgun_list_events(...)`, and `mailgun_get_domain()`. Email sending is MODERATE because it contacts external recipients and consumes sending quota.
- `plivo_send_message(...)`, `plivo_get_account()`, `vonage_send_sms(...)`, `vonage_get_balance()`, `seven_send_sms(...)`, and `seven_get_balance()`. Sends are MODERATE because they contact external recipients and can consume telecom spend; account/balance reads are SAFE.

Credential providers and fallback env vars:
- Twilio: provider `twilio`, fields `account_sid` / `accountSid` / `sid`, `auth_token` / `authToken` / `api_key_secret` / `apiKeySecret` / `token` / `value`, and optional `api_key_sid` / `apiKeySid`. Env fallback supports `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and optional `TWILIO_API_KEY_SID`. Use `base_url` / `url` or `TWILIO_BASE_URL` for non-default API roots.
- SendGrid: provider `sendgrid`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `SENDGRID_API_KEY`. Use `base_url` / `url` or `SENDGRID_BASE_URL` for non-default API roots.
- Mailgun: provider `mailgun`, fields `api_key`, `apiKey`, `token`, or `value`, plus `domain` / `email_domain` / `emailDomain`; env fallback `MAILGUN_API_KEY` and `MAILGUN_DOMAIN`. Use `base_url` / `api_domain` / `url` or `MAILGUN_BASE_URL` for non-default API roots.
- Brevo: provider `brevo`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `BREVO_API_KEY`. Use `base_url` / `url` or `BREVO_BASE_URL` for non-default API roots.
- Mailjet: provider `mailjet`, fields `api_key` plus `secret_key` for email API auth, and `sms_token` / `token` for SMS auth. Env fallback supports `MAILJET_API_KEY`, `MAILJET_SECRET_KEY`, and `MAILJET_SMS_TOKEN`. Use `base_url` / `url` or `MAILJET_BASE_URL` for regional API roots.
- Mandrill: provider `mandrill`, fields `api_key`, `key`, `token`, or `value`; env fallback `MANDRILL_API_KEY`. Use `base_url` / `url` or `MANDRILL_BASE_URL` for non-default API roots.
- MessageBird: provider `messagebird`, fields `access_key`, `accessKey`, `api_key`, `token`, or `value`; env fallback `MESSAGEBIRD_ACCESS_KEY`. Use `base_url` / `url` or `MESSAGEBIRD_BASE_URL` for non-default API roots.
- Mocean: provider `mocean`, fields `api_key` / `mocean-api-key` plus `api_secret` / `mocean-api-secret`; env fallback `MOCEAN_API_KEY` and `MOCEAN_API_SECRET`. Use `base_url` / `url` or `MOCEAN_BASE_URL` for non-default API roots.
- MSG91: provider `msg91`, fields `auth_key`, `authkey`, `api_key`, `token`, or `value`; env fallback `MSG91_AUTH_KEY`. Use `base_url` / `url` or `MSG91_BASE_URL` for non-default API roots.
- Plivo: provider `plivo`, fields `auth_id` plus `auth_token`; env fallback `PLIVO_AUTH_ID` and `PLIVO_AUTH_TOKEN`. Use `base_url` / `url` or `PLIVO_BASE_URL` for non-default API roots.
- Vonage: provider `vonage`, fields `api_key` plus `api_secret`; env fallback `VONAGE_API_KEY` and `VONAGE_API_SECRET`. Use `base_url` / `url` or `VONAGE_BASE_URL` for non-default API roots.
- seven.io: provider `seven`, fields `api_key`, `token`, or `value`; env fallback `SEVEN_API_KEY`. Use `base_url` / `url` or `SEVEN_BASE_URL` for non-default API roots.

### Commerce Billing Service Tools

This batch includes:
- `stripe_list_records(resource, ...)`, `stripe_search_records(resource, query, ...)`, `stripe_get_record(resource, record_id)`, and `stripe_get_balance()` for SAFE read/search access across common Stripe billing records. `stripe_create_customer(...)` and `stripe_update_customer(...)` are MODERATE because they change customer data.
- `shopify_list_records(resource, ...)` and `shopify_get_record(resource, record_id)` for SAFE Shopify Admin REST reads across products, orders, and customers. `shopify_create_product(...)` and `shopify_update_product(...)` are MODERATE because they change storefront catalog data.
- `woocommerce_list_records(resource, ...)` and `woocommerce_get_record(resource, record_id)` for SAFE WooCommerce reads across products, orders, and customers. `woocommerce_create_record(...)` and `woocommerce_update_record(...)` are MODERATE because they change store data.
- `chargebee_list_records(resource, ...)` and `chargebee_get_record(resource, record_id)` for SAFE Chargebee reads across customers, subscriptions, invoices, transactions, items, item prices, and plans. `chargebee_create_customer(...)` and `chargebee_update_customer(...)` are MODERATE because they change billing customer data.
- `paddle_list_products(...)`, `paddle_list_plans(...)`, `paddle_list_subscription_users(...)`, `paddle_list_payments(...)`, `paddle_get_order(...)`, and `paddle_list_coupons(...)` for SAFE Paddle vendor reads. `paddle_create_coupon(...)`, `paddle_update_coupon(...)`, and `paddle_reschedule_payment(...)` are MODERATE because they change coupons or payment schedules.
- `profitwell_get_settings()` and `profitwell_get_metrics(...)` for SAFE ProfitWell account and metrics reads.
- `tapfiliate_list_affiliates(...)`, `tapfiliate_get_affiliate(...)`, `tapfiliate_list_program_affiliates(...)`, and `tapfiliate_get_program_affiliate(...)` for SAFE Tapfiliate affiliate reads. Create/delete/metadata/program-approval tools are MODERATE because they change affiliate or program state.
- `magento_list_records(resource, ...)` and `magento_get_record(resource, record_id)` for SAFE Magento customer, order, and product reads. Customer/product create/update/delete, invoice creation, order cancellation, and shipment creation are MODERATE because they change store or order state.
- `unleashed_list_sales_orders(...)`, `unleashed_list_stock_on_hand(...)`, and `unleashed_get_stock_on_hand(product_id)` for SAFE Unleashed order and inventory reads.
- `quickbooks_query(...)`, `quickbooks_list_records(...)`, and `quickbooks_get_record(...)` for SAFE QuickBooks Online accounting reads. Customer and invoice create/update tools are MODERATE because they change accounting records.
- `xero_list_tenants()`, `xero_list_records(...)`, and `xero_get_record(...)` for SAFE Xero accounting reads. Contact and invoice create/update tools are MODERATE because they change accounting records.

Credential providers and fallback env vars:
- Stripe: provider `stripe`, fields `secret_key`, `secretKey`, `api_key`, `apiKey`, `token`, or `value`; env fallback `STRIPE_SECRET_KEY`. Use `base_url` / `url` or `STRIPE_BASE_URL` for non-default API roots.
- Shopify: provider `shopify`, fields `shop_subdomain` / `shopSubdomain` / `shop` / `domain` plus `access_token` / `accessToken` / `token` / `value` for modern Admin API token auth. Legacy basic auth can use `api_key` / `apiKey` plus `password`. Env fallback supports `SHOPIFY_SHOP`, `SHOPIFY_ACCESS_TOKEN`, optional `SHOPIFY_API_VERSION`, legacy `SHOPIFY_API_KEY` and `SHOPIFY_PASSWORD`, and `SHOPIFY_BASE_URL` for a full Admin REST root.
- WooCommerce: provider `woocommerce`, fields `url` / `site_url` / `base_url`, `consumer_key` / `consumerKey`, and `consumer_secret` / `consumerSecret`. Env fallback supports `WOOCOMMERCE_URL`, `WOOCOMMERCE_BASE_URL`, `WOOCOMMERCE_CONSUMER_KEY`, and `WOOCOMMERCE_CONSUMER_SECRET`.
- Chargebee: provider `chargebee`, fields `site` / `account_name` / `accountName` / `subdomain` plus `api_key` / `apiKey` / `token` / `value`; env fallback `CHARGEBEE_SITE` and `CHARGEBEE_API_KEY`. Use `base_url` / `url` or `CHARGEBEE_BASE_URL` for non-default API roots.
- Paddle: provider `paddle`, fields `vendor_id` / `vendorId` plus `vendor_auth_code` / `vendorAuthCode`; env fallback `PADDLE_VENDOR_ID` and `PADDLE_VENDOR_AUTH_CODE`. Use `sandbox` / `use_sandbox`, `PADDLE_SANDBOX`, `base_url`, or `PADDLE_BASE_URL` for sandbox or non-default vendor API roots.
- ProfitWell: provider `profitwell`, fields `access_token`, `api_token`, `token`, or `value`; env fallback `PROFITWELL_API_TOKEN`. Use `base_url` / `url` or `PROFITWELL_BASE_URL` for non-default API roots.
- Tapfiliate: provider `tapfiliate`, fields `api_key`, `token`, or `value`; env fallback `TAPFILIATE_API_KEY`. Use `base_url` / `url` or `TAPFILIATE_BASE_URL` for non-default API roots.
- Magento: provider `magento`, fields `host` / `base_url` plus `access_token`; env fallback `MAGENTO_HOST` or `MAGENTO_BASE_URL` plus `MAGENTO_ACCESS_TOKEN`.
- Unleashed: provider `unleashed`, fields `api_id` plus `api_key`; env fallback `UNLEASHED_API_ID` and `UNLEASHED_API_KEY`. Use `base_url` / `url` or `UNLEASHED_BASE_URL` for non-default API roots.
- QuickBooks Online: provider `quickbooks`, fields `access_token`, `realm_id` / `company_id`, optional `environment`, and optional `base_url`; env fallback supports `QUICKBOOKS_ACCESS_TOKEN`, `QUICKBOOKS_REALM_ID`, `QUICKBOOKS_ENVIRONMENT`, and `QUICKBOOKS_BASE_URL`.
- Xero: provider `xero`, fields `access_token`, `tenant_id` / `organization_id`, optional `base_url`, and optional `connections_url`; env fallback supports `XERO_ACCESS_TOKEN`, `XERO_TENANT_ID`, `XERO_BASE_URL`, and `XERO_CONNECTIONS_URL`.

### Notification Service Tools

This batch includes:
- `pushbullet_send_push(...)`, `pushbullet_list_pushes(...)`, `pushbullet_update_push(...)`, and `pushbullet_delete_push(push_id)` for Pushbullet note/link pushes and push history. Listing is SAFE; send/update/delete are MODERATE because they notify users or change push history.
- `pushcut_send_notification(notification_name, ...)` for Pushcut smart notifications. Sending is MODERATE because it notifies devices and can trigger linked actions.
- `gotify_send_message(...)`, `gotify_list_messages(...)`, and `gotify_delete_message(message_id)`. Listing is SAFE; send/delete are MODERATE because they notify clients or change message history.
- `pushover_send_message(...)` for Pushover notifications, including emergency-priority retry/expire fields. Sending is MODERATE because it contacts devices and can consume app quota.
- `signl4_send_alert(...)` and `signl4_resolve_alert(external_id, ...)` for SIGNL4 alert events. Both are MODERATE because they notify on-call teams or change alert state.

Credential providers and fallback env vars:
- Pushbullet: provider `pushbullet`, fields `access_token`, `accessToken`, `api_key`, `apiKey`, `token`, or `value`; env fallback `PUSHBULLET_ACCESS_TOKEN`. Use `base_url` / `url` or `PUSHBULLET_BASE_URL` for non-default API roots.
- Pushcut: provider `pushcut`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `PUSHCUT_API_KEY`. Use `base_url` / `url` or `PUSHCUT_BASE_URL` for non-default API roots.
- Gotify: provider `gotify`, fields `base_url` / `url`, `app_token` / `appApiToken` for sending, and `client_token` / `clientApiToken` for list/delete operations. Env fallback supports `GOTIFY_BASE_URL`, `GOTIFY_APP_TOKEN`, and `GOTIFY_CLIENT_TOKEN`.
- Pushover: provider `pushover`, fields `api_token` / `api_key` / `apiKey` / `token` plus `user_key` / `userKey` / `user`; env fallback `PUSHOVER_API_TOKEN` and `PUSHOVER_USER_KEY`. Use `base_url` / `url` or `PUSHOVER_BASE_URL` for non-default API roots.
- SIGNL4: provider `signl4`, fields `team_secret` / `teamSecret` / `secret` or `webhook_url`; env fallback `SIGNL4_TEAM_SECRET` or `SIGNL4_WEBHOOK_URL`. Use `base_url` / `url` or `SIGNL4_BASE_URL` for non-default webhook roots.

### Content Management Service Tools

This batch includes:
- `wordpress_list_records(resource, ...)`, `wordpress_get_record(resource, record_id)`, `wordpress_create_record(resource, ...)`, `wordpress_update_record(resource, record_id, ...)`, and `wordpress_delete_record(resource, record_id)`. Reads are SAFE; create/update/delete are MODERATE because they change site content or users.
- `strapi_list_entries(collection, ...)`, `strapi_get_entry(collection, entry_id)`, `strapi_create_entry(collection, data)`, `strapi_update_entry(collection, entry_id, data)`, and `strapi_delete_entry(collection, entry_id)`. Reads are SAFE; mutations are MODERATE because they change CMS records.
- `contentful_list_records(resource, ...)` and `contentful_get_record(resource, record_id, ...)` for Contentful Delivery/Preview API reads. Both are SAFE.
- `ghost_list_posts(...)`, `ghost_get_post(...)`, `ghost_create_post(...)`, `ghost_update_post(post_id, ...)`, and `ghost_delete_post(post_id)`. Reads use the Content API and are SAFE; Admin API mutations are MODERATE.
- `storyblok_list_stories(...)`, `storyblok_get_story(...)`, `storyblok_publish_story(story_id)`, `storyblok_unpublish_story(story_id)`, and `storyblok_delete_story(story_id)`. Reads are SAFE; publish/unpublish/delete are MODERATE because they change public content state or remove content.
- `webflow_list_sites()`, `webflow_list_site_collections(site_id)`, `webflow_get_collection(collection_id)`, `webflow_list_collection_items(collection_id, ...)`, `webflow_get_collection_item(collection_id, item_id)`, `webflow_create_collection_item(collection_id, field_data_json, live?)`, `webflow_update_collection_item(collection_id, item_id, field_data_json, live?)`, and `webflow_delete_collection_item(collection_id, item_id)`. Reads are SAFE; create/update/delete are MODERATE because they change CMS content.

Credential providers and fallback env vars:
- WordPress: provider `wordpress`, fields `url` / `site_url` / `base_url`, `username`, and `password` / `application_password`; env fallback `WORDPRESS_URL`, `WORDPRESS_USERNAME`, and `WORDPRESS_PASSWORD`.
- Strapi: provider `strapi`, fields `url` / `base_url`, `api_token` / `jwt` / `token`, or `email` plus `password` for local auth. Env fallback supports `STRAPI_URL`, `STRAPI_API_TOKEN`, `STRAPI_EMAIL`, `STRAPI_PASSWORD`, and `STRAPI_API_VERSION`.
- Contentful: provider `contentful`, fields `space_id` / `spaceId`, delivery token (`access_token`, `delivery_token`, or Contentful-style names), and optional preview token. Env fallback supports `CONTENTFUL_SPACE_ID`, `CONTENTFUL_DELIVERY_TOKEN`, `CONTENTFUL_PREVIEW_TOKEN`, `CONTENTFUL_BASE_URL`, and `CONTENTFUL_PREVIEW_BASE_URL`.
- Ghost: provider `ghost`, fields `url`, `content_api_key`, and `admin_api_key` (`key_id:hex_secret`). Env fallback supports `GHOST_URL`, `GHOST_CONTENT_API_KEY`, `GHOST_ADMIN_API_KEY`, and `GHOST_API_VERSION`.
- Storyblok: provider `storyblok`, fields `content_token`, `management_token`, and `space_id` / `spaceId`; env fallback supports `STORYBLOK_CONTENT_TOKEN`, `STORYBLOK_MANAGEMENT_TOKEN`, `STORYBLOK_SPACE_ID`, `STORYBLOK_CONTENT_BASE_URL`, and `STORYBLOK_MANAGEMENT_BASE_URL`.
- Webflow: provider `webflow`, fields `access_token` / `accessToken` / `token` / `value`; env fallback `WEBFLOW_ACCESS_TOKEN`. Use `base_url` / `url` or `WEBFLOW_BASE_URL` for non-default API roots.

### Operations Monitoring Service Tools

This batch includes:
- `netlify_list_sites(...)`, `netlify_get_site(site_id)`, `netlify_list_deploys(site_id, ...)`, `netlify_get_deploy(site_id, deploy_id)`, `netlify_cancel_deploy(deploy_id)`, and `netlify_delete_site(site_id)`. Reads are SAFE; cancel/delete operations are MODERATE because they alter deploy or site state.
- `uptimerobot_get_account()`, `uptimerobot_list_monitors(...)`, `uptimerobot_get_monitor(monitor_id, ...)`, `uptimerobot_create_monitor(...)`, `uptimerobot_update_monitor(monitor_id, ...)`, `uptimerobot_delete_monitor(monitor_id)`, and `uptimerobot_reset_monitor(monitor_id)`. Reads are SAFE; create/update/delete/reset are MODERATE because they alter monitoring state.
- `pagerduty_list_incidents(...)`, `pagerduty_get_incident(incident_id)`, `pagerduty_create_incident(...)`, `pagerduty_update_incident(incident_id, ...)`, `pagerduty_add_incident_note(incident_id, content, ...)`, `pagerduty_list_services(...)`, and `pagerduty_get_user(user_id)`. Reads are SAFE; incident writes and notes are MODERATE because they change incident workflows.
- `sentry_list_organizations(...)`, `sentry_list_projects(...)`, `sentry_list_project_issues(...)`, `sentry_get_issue(issue_id)`, `sentry_update_issue(organization_slug, issue_id, ...)`, `sentry_list_project_events(...)`, and `sentry_get_event(...)`. Reads are SAFE; issue updates are MODERATE.
- `cloudflare_list_zones(...)`, `cloudflare_list_dns_records(zone_id, ...)`, `cloudflare_create_dns_record(...)`, `cloudflare_update_dns_record(...)`, `cloudflare_delete_dns_record(...)`, `cloudflare_list_origin_certificates(zone_id, ...)`, `cloudflare_get_origin_certificate(...)`, `cloudflare_upload_origin_certificate(...)`, and `cloudflare_delete_origin_certificate(...)`. Reads are SAFE; DNS/certificate writes are MODERATE.
- `grafana_search_dashboards(...)`, `grafana_get_dashboard(dashboard_uid)`, `grafana_create_dashboard(...)`, `grafana_delete_dashboard(dashboard_uid)`, and `grafana_list_teams(...)`. Reads are SAFE; create/delete operations are MODERATE because they change dashboards.
- `metabase_list_questions(...)`, `metabase_get_question(question_id)`, `metabase_query_question(question_id, ...)`, `metabase_list_dashboards(...)`, and `metabase_get_dashboard(dashboard_id)`. These are SAFE analytics reads; question execution may consume database resources.
- `elasticsearch_list_indices(...)`, `elasticsearch_search(index, ...)`, `elasticsearch_get_document(index, document_id)`, `elasticsearch_index_document(...)`, and `elasticsearch_delete_document(...)`. Reads/searches are SAFE; index/delete operations are MODERATE because they change documents.
- `splunk_list_saved_searches(...)`, `splunk_create_search_job(...)`, `splunk_get_search_job(search_id)`, and `splunk_get_search_results(search_id, ...)`. Listing/job reads are SAFE; search-job creation is MODERATE because it runs backend work and can consume Splunk capacity.
- `rundeck_get_job_metadata(job_id, ...)` and `rundeck_execute_job(job_id, ...)`. Job metadata reads are SAFE; job execution is MODERATE because it starts Rundeck automation.

Credential providers and fallback env vars:
- Netlify: provider `netlify`, fields `access_token` / `api_key` / `token` / `value`; env fallback `NETLIFY_ACCESS_TOKEN`. Use `base_url` / `url` or `NETLIFY_BASE_URL` for non-default API roots.
- UptimeRobot: provider `uptimerobot`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `UPTIMEROBOT_API_KEY`. Use `base_url` / `url` or `UPTIMEROBOT_BASE_URL` for non-default API roots.
- PagerDuty: provider `pagerduty`, fields `api_token` / `api_key` / `token` / `value` for REST API-token auth, or `access_token` for OAuth bearer auth. Env fallback supports `PAGERDUTY_API_TOKEN`, optional `PAGERDUTY_FROM_EMAIL`, and `PAGERDUTY_BASE_URL`.
- Sentry: provider `sentry`, fields `auth_token` / `access_token` / `api_key` / `token` / `value`; env fallback `SENTRY_AUTH_TOKEN`. Use `base_url` / `url` or `SENTRY_BASE_URL` for self-hosted Sentry.
- Cloudflare: provider `cloudflare`, fields `api_token` / `access_token` / `token` / `value`; env fallback `CLOUDFLARE_API_TOKEN`. Use `base_url` / `url` or `CLOUDFLARE_BASE_URL` for non-default API roots.
- Grafana: provider `grafana`, fields `api_key`, `apiKey`, `access_token`, `token`, or `value`, plus `base_url` / `url`; env fallback `GRAFANA_API_TOKEN` and `GRAFANA_BASE_URL`.
- Metabase: provider `metabase`, fields `base_url` / `url`, plus `session_token`, `api_key`, or `username` plus `password`; env fallback supports `METABASE_BASE_URL`, `METABASE_SESSION_TOKEN`, `METABASE_API_KEY`, `METABASE_USERNAME`, and `METABASE_PASSWORD`.
- Elasticsearch: provider `elasticsearch`, `elastic`, or `elastic_cloud`, fields `base_url` / `url`, `api_key`, `bearer_token`, or `username` plus `password`; env fallback supports `ELASTICSEARCH_BASE_URL`, `ELASTICSEARCH_API_KEY`, `ELASTICSEARCH_BEARER_TOKEN`, `ELASTICSEARCH_USERNAME`, `ELASTICSEARCH_PASSWORD`, and `ELASTICSEARCH_IGNORE_SSL_ISSUES`.
- Splunk: provider `splunk`, fields `auth_token`, `access_token`, `token`, or `value`, plus `base_url` / `url` and optional `allow_unauthorized_certs`; env fallback supports `SPLUNK_BASE_URL`, `SPLUNK_AUTH_TOKEN`, and `SPLUNK_ALLOW_UNAUTHORIZED_CERTS`.
- Rundeck: provider `rundeck`, fields `token`, `api_token`, `access_token`, or `value`, plus `base_url` / `url`; env fallback supports `RUNDECK_BASE_URL` and `RUNDECK_TOKEN`.

### Enrichment Security Service Tools

This batch includes:
- `urlscan_search_scans(query, ...)`, `urlscan_get_result(scan_id)`, and `urlscan_submit_scan(url, ...)`. Searches and result reads are SAFE; scan submission is MODERATE because it sends a URL to an external scanner and may make scan artifacts discoverable depending on visibility.
- `hunter_domain_search(domain, ...)`, `hunter_email_finder(domain, first_name, last_name)`, and `hunter_email_verifier(email)` for Hunter email discovery and deliverability data. These are SAFE read/enrichment calls.
- `mailcheck_check_email(email)` for Mailcheck email validation. This is SAFE.
- `peekalink_preview_url(url)` and `peekalink_check_availability(url)` for Peekalink link metadata. These are SAFE.
- `jina_reader_fetch_url(url, ...)`, `jina_search_web(query, ...)`, and `jina_deep_research(query, ...)` for Jina Reader/Search/DeepSearch. Reader and Search are SAFE extraction/search calls; DeepSearch is MODERATE because it can perform broader external research and consume hosted AI quota.
- `misp_search_attributes(...)`, `misp_search_events(...)`, `misp_get_event(event_id)`, `misp_create_event(...)`, `misp_list_tags(...)`, `misp_add_event_tag(...)`, and `misp_remove_event_tag(...)` for MISP threat-intelligence event and tag workflows. Search/list/get operations are SAFE; create/tag mutations are MODERATE.
- `thehive_list_cases(...)`, `thehive_get_case(case_id)`, `thehive_create_case(...)`, `thehive_list_alerts(...)`, `thehive_get_alert(alert_id)`, and `thehive_create_alert(...)` for TheHive case and alert workflows. Reads are SAFE; create operations are MODERATE.
- `securityscorecard_get_company_scorecard(...)`, `securityscorecard_list_company_factors(...)`, `securityscorecard_get_company_history(...)`, `securityscorecard_list_portfolios(...)`, `securityscorecard_add_portfolio_company(...)`, and `securityscorecard_remove_portfolio_company(...)`. Scorecard reads are SAFE; portfolio membership changes are MODERATE.
- `okta_list_users(...)`, `okta_get_user(user_id)`, `okta_create_user(...)`, `okta_update_user(...)`, and `okta_delete_user(...)` for Okta user administration. List/get operations are SAFE; create/update/delete operations are MODERATE because they change identity records.
- `elastic_security_list_cases(...)`, `elastic_security_get_case(case_id)`, `elastic_security_list_case_tags()`, `elastic_security_create_case(...)`, and `elastic_security_add_case_comment(...)` for Elastic Security/Kibana cases. Reads are SAFE; create/comment actions are MODERATE.

Credential providers and fallback env vars:
- urlscan.io: provider `urlscan`, fields `api_key`, `apiKey`, `access_token`, `token`, or `value`; env fallback `URLSCAN_API_KEY`. Use `base_url` / `url` or `URLSCAN_BASE_URL` for non-default API roots.
- Hunter: provider `hunter`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `HUNTER_API_KEY`. Use `base_url` / `url` or `HUNTER_BASE_URL` for non-default API roots.
- Mailcheck: provider `mailcheck`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `MAILCHECK_API_KEY`. Use `base_url` / `url` or `MAILCHECK_BASE_URL` for non-default API roots.
- Peekalink: provider `peekalink`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `PEEKALINK_API_KEY`. Use `base_url` / `url` or `PEEKALINK_BASE_URL` for non-default API roots.
- Jina AI: provider `jina`, fields `api_key`, `apiKey`, `access_token`, `token`, or `value`; env fallback `JINA_API_KEY`. Reader/Search can run without a key where Jina allows anonymous usage; DeepSearch requires a saved credential or env key. Base URL overrides are `JINA_READER_BASE_URL`, `JINA_SEARCH_BASE_URL`, and `JINA_DEEPSEARCH_BASE_URL`.
- MISP: provider `misp`, fields `api_key`, `apiKey`, `auth_key`, `authKey`, `token`, or `value`, plus `base_url` / `url`; env fallback supports `MISP_BASE_URL`, `MISP_API_KEY`, and `MISP_ALLOW_UNAUTHORIZED_CERTS`.
- TheHive: provider `thehive`, fields `api_key`, `ApiKey`, `apiKey`, `access_token`, `token`, or `value`, plus `base_url` / `url` and optional `api_version`; env fallback supports `THEHIVE_BASE_URL`, `THEHIVE_API_KEY`, `THEHIVE_API_VERSION`, and `THEHIVE_ALLOW_UNAUTHORIZED_CERTS`.
- SecurityScorecard: provider `securityscorecard`, fields `api_key`, `apiKey`, `access_token`, `token`, or `value`; env fallback supports `SECURITYSCORECARD_API_KEY` and `SECURITYSCORECARD_BASE_URL`.
- Okta: provider `okta`, fields `access_token`, `accessToken`, `api_token`, `ssws_token`, `token`, or `value`, plus `base_url` / `org_url` / `url` or `domain`; env fallback supports `OKTA_ACCESS_TOKEN`, `OKTA_BASE_URL`, and `OKTA_DOMAIN`.
- Elastic Security: provider `elastic_security`, fields `base_url` / `url`, `api_key`, or `username` plus `password`; env fallback supports `ELASTIC_SECURITY_BASE_URL`, `ELASTIC_SECURITY_API_KEY`, `ELASTIC_SECURITY_USERNAME`, and `ELASTIC_SECURITY_PASSWORD`.

### Lead Enrichment Service Tools

This batch includes:
- `clearbit_enrich_company(domain, ...)`, `clearbit_autocomplete_company(name)`, and `clearbit_enrich_person(email, ...)` for company and person enrichment. These are MODERATE because they send identity/company data to an external enrichment provider and consume quota.
- `uplead_enrich_company(domain?, company?)` and `uplead_enrich_person(email?, first_name?, last_name?, domain?)` for company and contact enrichment. These are MODERATE for the same reason.
- `dropcontact_submit_enrichment(...)` and `dropcontact_fetch_request(request_id)` for asynchronous contact enrichment.
- `humantic_create_profile(user_id)`, `humantic_get_profile(user_id, persona?)`, and `humantic_update_profile_text(user_id, text)` for contact-intelligence profile workflows.
- `lonescale_create_list(name, entity_type?)`, `lonescale_add_people_item(...)`, and `lonescale_add_company_item(...)` for prospecting list management.
- `uproc_get_profile()` and `uproc_process(processor, params_json, callback_url?)` for saved enrichment account inspection and explicit processor calls.

Credential providers and fallback env vars:
- Clearbit: provider `clearbit`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `CLEARBIT_API_KEY`. Base URL overrides are `CLEARBIT_COMPANY_BASE_URL`, `CLEARBIT_PERSON_BASE_URL`, and `CLEARBIT_AUTOCOMPLETE_BASE_URL`.
- Uplead: provider `uplead`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `UPLEAD_API_KEY`. Use `base_url` / `url` or `UPLEAD_BASE_URL` for non-default API roots.
- Dropcontact: provider `dropcontact`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `DROPCONTACT_API_KEY`. Use `base_url` / `url` or `DROPCONTACT_BASE_URL` for non-default API roots.
- Humantic AI: provider `humantic`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `HUMANTIC_API_KEY`. Use `base_url` / `url` or `HUMANTIC_BASE_URL` for non-default API roots.
- LoneScale: provider `lonescale`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `LONESCALE_API_KEY`. Use `base_url` / `url` or `LONESCALE_BASE_URL` for non-default API roots.
- uProc: provider `uproc`, fields `email` and `api_key` / `apiKey` / `token` / `value`; env fallbacks `UPROC_EMAIL` and `UPROC_API_KEY`. Use `base_url` / `url` or `UPROC_BASE_URL` for non-default API roots.

### Data Table Service Tools

This batch includes:
- `baserow_list_tables(...)`, `baserow_list_fields(table_id)`, `baserow_list_rows(table_id, ...)`, `baserow_get_row(table_id, row_id)`, `baserow_create_row(table_id, fields_json, ...)`, `baserow_update_row(table_id, row_id, fields_json, ...)`, and `baserow_delete_row(table_id, row_id)`. Reads are SAFE; create/update/delete are MODERATE because they change table data.
- `nocodb_list_bases(...)`, `nocodb_get_base(base_id, ...)`, `nocodb_list_records(base_id, table_id, ...)`, `nocodb_get_record(base_id, table_id, record_id)`, `nocodb_count_records(base_id, table_id, ...)`, `nocodb_create_record(base_id, table_id, fields_json)`, `nocodb_update_record(base_id, table_id, record_id, fields_json)`, and `nocodb_delete_record(base_id, table_id, record_id)`. Reads are SAFE; create/update/delete are MODERATE.
- `coda_list_docs(...)`, `coda_list_tables(doc_id, ...)`, `coda_list_table_rows(doc_id, table_id, ...)`, `coda_get_table_row(doc_id, table_id, row_id, ...)`, `coda_create_table_row(doc_id, table_id, cells_json, ...)`, `coda_update_table_row(doc_id, table_id, row_id, cells_json, ...)`, `coda_delete_table_row(doc_id, table_id, row_id)`, `coda_list_formulas(doc_id, ...)`, and `coda_list_controls(doc_id, ...)`. Reads are SAFE; row writes/deletes are MODERATE.
- `grist_list_orgs()`, `grist_list_workspaces(org_id)`, `grist_list_docs(workspace_id)`, `grist_list_tables(doc_id)`, `grist_list_columns(doc_id, table_id)`, `grist_list_records(doc_id, table_id, ...)`, `grist_create_record(doc_id, table_id, fields_json)`, `grist_update_record(doc_id, table_id, record_id, fields_json)`, and `grist_delete_records(doc_id, table_id, row_ids)`. Reads are SAFE; record writes/deletes are MODERATE.
- `adalo_list_records(collection_id, ...)`, `adalo_get_record(collection_id, row_id)`, `adalo_create_record(collection_id, fields_json)`, `adalo_update_record(collection_id, row_id, fields_json)`, and `adalo_delete_record(collection_id, row_id)`. Reads are SAFE; create/update/delete are MODERATE.
- `bubble_list_objects(type_name, ...)`, `bubble_get_object(type_name, object_id)`, `bubble_create_object(type_name, fields_json)`, `bubble_update_object(type_name, object_id, fields_json)`, and `bubble_delete_object(type_name, object_id)`. Reads are SAFE; create/update/delete are MODERATE.
- `cockpit_list_collections()`, `cockpit_list_collection_entries(collection, ...)`, `cockpit_save_collection_entry(collection, data_json, entry_id?)`, `cockpit_list_singletons()`, `cockpit_get_singleton(singleton)`, and `cockpit_submit_form(form, data_json)`. Reads are SAFE; collection saves and form submissions are MODERATE.
- `supabase_list_rows(table, ...)`, `supabase_insert_rows(table, rows_json, ...)`, `supabase_update_rows(table, fields_json, filters_query, ...)`, and `supabase_delete_rows(table, filters_query, ...)`. Reads are SAFE; insert/update/delete are MODERATE.
- `quickbase_list_fields(table_id)`, `quickbase_query_records(table_id, ...)`, `quickbase_upsert_records(table_id, records_json, ...)`, and `quickbase_delete_records(table_id, where)`. Reads are SAFE; upsert/delete are MODERATE.
- `seatable_get_metadata()`, `seatable_list_rows(table_name, ...)`, `seatable_get_row(table_name, row_id)`, `seatable_create_row(table_name, fields_json)`, `seatable_update_row(table_name, row_id, fields_json)`, and `seatable_delete_row(table_name, row_id)`. Reads are SAFE; create/update/delete are MODERATE.
- `stackby_list_rows(stack_id, table, ...)`, `stackby_get_row(stack_id, table, row_id)`, `stackby_create_rows(stack_id, table, records_json)`, and `stackby_delete_rows(stack_id, table, row_ids)`. Reads are SAFE; create/delete are MODERATE.
- `kobotoolbox_list_forms(...)`, `kobotoolbox_get_form(form_id)`, `kobotoolbox_redeploy_form(form_id)`, submission list/get/delete/validation helpers, REST hook list/get/log/retry helpers, and form media file list/get/delete/create helpers. Form/submission/hook/file reads are SAFE; redeploy, delete, validation update, retry, and file creation operations are MODERATE because they change form state or data.

Credential providers and fallback env vars:
- Baserow: provider `baserow`, fields `token`, `api_token`, `apiKey`, `api_key`, `database_token`, or `value`; env fallback `BASEROW_API_TOKEN`. Use `base_url` / `host` / `url` or `BASEROW_BASE_URL` for self-hosted Baserow.
- NocoDB: provider `nocodb`, fields `api_token`, `apiToken`, `token`, `access_token`, `api_key`, or `value`; env fallback `NOCODB_API_TOKEN`. Use `base_url` / `host` / `url` or `NOCODB_BASE_URL` for self-hosted NocoDB. `NOCODB_AUTH_HEADER` defaults to `xc-token`; set `xc-auth` when using a user token.
- Coda: provider `coda`, fields `access_token`, `api_token`, `api_key`, `token`, or `value`; env fallback `CODA_API_TOKEN`. Use `base_url` / `url` or `CODA_BASE_URL` for non-default API roots.
- Grist: provider `grist`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `GRIST_API_KEY`. Use `base_url` / `url` or `GRIST_BASE_URL` for paid-team or self-hosted API roots.
- Adalo: provider `adalo`, fields `api_key` / `apiKey` plus `app_id` / `appId`, optional `base_url`; env fallbacks `ADALO_API_KEY`, `ADALO_APP_ID`, and `ADALO_BASE_URL`.
- Bubble: provider `bubble`, fields `api_token` / `apiToken`, plus `app_name` / `appName` or `domain`, optional `environment` and `base_url`; env fallbacks `BUBBLE_API_TOKEN`, `BUBBLE_APP_NAME`, `BUBBLE_ENVIRONMENT`, `BUBBLE_DOMAIN`, and `BUBBLE_BASE_URL`.
- Cockpit: provider `cockpit`, fields `url` / `base_url` plus `access_token` / `accessToken`; env fallbacks `COCKPIT_BASE_URL` and `COCKPIT_ACCESS_TOKEN`.
- Supabase: provider `supabase`, fields `service_role`, `service_role_key`, `api_key`, `anon_key`, `token`, or `value`; env fallbacks `SUPABASE_SERVICE_ROLE_KEY` or `SUPABASE_API_KEY`. Save `base_url` / `host` / `url` / `project_url` or set `SUPABASE_URL`; `SUPABASE_BASE_URL` can point directly at a REST API root.
- Quickbase: provider `quickbase`, fields `user_token`, `api_key`, `token`, or `value`, plus `hostname`; env fallbacks `QUICKBASE_USER_TOKEN` and `QUICKBASE_HOSTNAME`. Use `base_url` / `url` or `QUICKBASE_BASE_URL` for non-default API roots.
- SeaTable: provider `seatable`, fields `api_token`, `token`, or `value`; env fallback `SEATABLE_API_TOKEN`. Use `base_url` / `domain` / `url` or `SEATABLE_BASE_URL` for self-hosted SeaTable.
- Stackby: provider `stackby`, fields `api_key`, `apiKey`, `api_token`, `token`, or `value`; env fallback `STACKBY_API_KEY`. Use `base_url` / `host` / `url` or `STACKBY_BASE_URL` for non-default API roots.
- KoBoToolbox: provider `kobotoolbox`, fields `api_token`, `apiToken`, `token`, or `value`; env fallback `KOBOTOOLBOX_API_TOKEN`. Use `base_url` / `host` / `url` or `KOBOTOOLBOX_BASE_URL` for self-hosted or regional KoBoToolbox hosts.

### Chat Platform Service Tools

This batch includes:
- `telegram_get_me()`, `telegram_get_chat(chat_id)`, `telegram_send_message(chat_id, text, ...)`, and `telegram_delete_message(chat_id, message_id)`. Reads are SAFE; send/delete are MODERATE.
- `webex_list_rooms(...)`, `webex_get_room(room_id)`, `webex_list_messages(...)`, `webex_get_message(message_id)`, `webex_send_message(...)`, and `webex_delete_message(message_id)`. Reads are SAFE; send/delete are MODERATE.
- `whatsapp_list_phone_numbers(...)`, `whatsapp_send_text_message(...)`, `whatsapp_send_template_message(...)`, `whatsapp_get_media_url(media_id)`, and `whatsapp_delete_media(media_id)`. Phone/media reads are SAFE; sends and deletes are MODERATE.
- `discord_list_guild_channels(guild_id)`, `discord_get_channel(channel_id)`, `discord_get_channel_messages(channel_id, ...)`, `discord_send_channel_message(channel_id, content, ...)`, and `discord_delete_message(channel_id, message_id)`. Reads are SAFE; send/delete are MODERATE.
- `mattermost_get_me()`, `mattermost_list_teams(...)`, `mattermost_list_channels(team_id, ...)`, `mattermost_list_channel_posts(channel_id, ...)`, `mattermost_create_post(channel_id, message, ...)`, and `mattermost_delete_post(post_id)`. Reads are SAFE; create/delete are MODERATE.
- `matrix_whoami()`, `matrix_list_joined_rooms()`, `matrix_get_room_messages(room_id, ...)`, `matrix_send_room_message(room_id, body, ...)`, and `matrix_leave_room(room_id)`. Reads are SAFE; sending/leaving are MODERATE.
- `rocketchat_get_me()`, `rocketchat_list_channels(...)`, `rocketchat_get_channel_history(...)`, `rocketchat_post_message(channel, text, ...)`, and `rocketchat_delete_message(room_id, message_id)`. Reads are SAFE; post/delete are MODERATE.
- `zulip_get_profile()`, `zulip_list_streams(...)`, `zulip_get_messages(...)`, `zulip_send_message(message_type, to, content, ...)`, and `zulip_delete_message(message_id)`. Reads are SAFE; send/delete are MODERATE.

Credential providers and fallback env vars:
- Telegram: provider `telegram`, fields `bot_token`, `api_key`, `token`, or `value`; env fallback reuses `TELEGRAM_BOT_TOKEN`. Optional base override `TELEGRAM_API_BASE_URL`.
- Webex: provider `webex`, fields `access_token`, `api_key`, `token`, or `value`; env fallback `WEBEX_ACCESS_TOKEN`. Optional base override `WEBEX_BASE_URL`.
- WhatsApp Business Cloud: provider `whatsapp`, fields `access_token`, `business_account_id`, and `phone_number_id`; env fallbacks `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_BUSINESS_ACCOUNT_ID`, `WHATSAPP_PHONE_NUMBER_ID`, and `WHATSAPP_BASE_URL`.
- Discord: provider `discord`, fields `bot_token`, `botToken`, `token`, or `value`; env fallback reuses `DISCORD_BOT_TOKEN`. Optional base override `DISCORD_BASE_URL`.
- Mattermost: provider `mattermost`, fields `access_token`, `accessToken`, `api_token`, `token`, or `value`; env fallback `MATTERMOST_ACCESS_TOKEN`. Save `base_url` / `baseUrl` or set `MATTERMOST_BASE_URL`; the tool appends `/api/v4` when needed.
- Matrix: provider `matrix`, fields `access_token`, `accessToken`, `token`, or `value`; env fallback `MATRIX_ACCESS_TOKEN`. Save `homeserverUrl` / `base_url` or set `MATRIX_BASE_URL`; the tool appends `/_matrix/client/v3` when needed.
- Rocket.Chat: provider `rocketchat`, fields `auth_token`, `authKey`, `token`, or `value`, plus `user_id` / `userId`; env fallbacks `ROCKETCHAT_AUTH_TOKEN`, `ROCKETCHAT_USER_ID`, and `ROCKETCHAT_BASE_URL`.
- Zulip: provider `zulip`, fields `api_key`, `apiKey`, `token`, or `value`, plus `email`; env fallbacks `ZULIP_API_KEY`, `ZULIP_EMAIL`, and `ZULIP_BASE_URL`.

### Microsoft Graph Service Tools

This batch includes:
- `microsoft_todo_list_task_lists()`, `microsoft_todo_list_tasks(list_id, ...)`, `microsoft_todo_create_task(list_id, title, ...)`, and `microsoft_todo_update_task(list_id, task_id, fields_json)` for Microsoft To Do. Listing is SAFE; create/update operations are MODERATE because they change task state.
- `microsoft_onedrive_list_children(...)`, `microsoft_onedrive_get_item(...)`, `microsoft_onedrive_search(query, ...)`, and `microsoft_onedrive_upload_text_file(path, content, ...)` for OneDrive files. Metadata/search reads are SAFE; upload is MODERATE because it writes files.
- `microsoft_teams_list_joined_teams(...)`, `microsoft_teams_list_channels(team_id, ...)`, `microsoft_teams_list_channel_messages(team_id, channel_id, ...)`, and `microsoft_teams_send_channel_message(team_id, channel_id, content, ...)` for Teams channel workflows. Listing reads are SAFE; sending is MODERATE because it posts a message.
- `microsoft_sharepoint_search_sites(...)`, `microsoft_sharepoint_get_site(...)`, `microsoft_sharepoint_list_lists(site_id, ...)`, `microsoft_sharepoint_list_items(...)`, `microsoft_sharepoint_get_item(...)`, `microsoft_sharepoint_create_item(...)`, `microsoft_sharepoint_update_item_fields(...)`, and `microsoft_sharepoint_delete_item(...)` for SharePoint sites and lists. Reads are SAFE; create/update/delete are MODERATE.
- `microsoft_excel_list_worksheets(...)`, `microsoft_excel_get_used_range(...)`, `microsoft_excel_read_range(...)`, `microsoft_excel_update_range(...)`, `microsoft_excel_list_tables(...)`, and `microsoft_excel_add_table_row(...)` for Excel workbooks stored in OneDrive or SharePoint. Worksheet/table/range reads are SAFE; range and table-row writes are MODERATE.

Credential providers and fallback env vars:
- Microsoft Graph: provider `microsoft_graph`, fields `access_token`, `accessToken`, `token`, `bearer_token`, or `value`; env fallback `MICROSOFT_GRAPH_ACCESS_TOKEN`. Use `base_url` / `baseUrl` / `url` or `MICROSOFT_GRAPH_BASE_URL` for non-default Graph API roots.

### tool_manage

Enable, disable, prune, or inspect current-thread tool bindings. This is
normally available after the agent activates `Skill(name="tool-management")`.

```python
tool_manage(
    action: str,
    tools: list[str] = None,
    category: str = "",
    ttl: str | None = None,
    force: bool = False,
    stale_after_days: int = 30,
    min_enabled_age_days: int = 7,
    dry_run: bool = False,
)
```

**Actions:**
- `enable`  -  Enable tools by name (`tools`) or by category (`category`). In `astream()` (REST/SSE and sync-worker bridge callers) and `chat()` (MCP final-string path), this triggers an in-turn graph rebuild so the tools are callable in the very next step of the same user message.
- `disable`  -  Disable tools for the thread (`tools`). Takes effect on the next agent step. Refuses core tools (`file_read`, `file_write`, etc.) unless `force=True`. Mixed batches partially succeed: non-core names are disabled, core names are listed under `[Refused]` with a hint to retry that subset with `force=True`. Disable is non-destructive  -  it only appends to `disabled_tools`; entries in `enabled_tools` / `temporary_tools` are preserved, so a subsequent `enable` restores the tool's original permanent/TTL state. "Core" here is the hardcoded `ALL_TOOLS` set, which is a **superset** of what the `already_default` classifier bucket calls default-bound (user profile's `default_thread_tools` curates a subset of `ALL_TOOLS`).
- `prune`  -  Conservatively clean current-thread binding clutter. Removes expired temporary tools, missing/unavailable tool names, default-bound tools redundantly listed in `enabled_tools`, and recorded-stale permanent enablements whose last use is older than `stale_after_days`. Bindings with no recorded usage are left intact.
- `list_categories`  -  List all tool categories with tool counts.
- `status` / `inspect`  -  Show currently enabled/disabled tools for this thread, with TTL remaining per entry.

**Parameters:**
- `action` (`str`): One of: `enable`, `disable`, `prune`, `list_categories`, `status`.
- `tools` (`list[str]`): Specific tool names to enable or disable.
- `category` (`str`): Category name to enable all tools in.
- `ttl` (`str`): Required for `enable` only; ignored by other actions. Duration format: `Nm` (minutes), `Nh` (hours), `Nd` (days), `Nw` (weeks), or `"never"`/`"permanent"` for no expiry. Examples: `"30m"`, `"2h"`, `"7d"`, `"4w"`, `"never"`.
- `force` (`bool`): For `disable` only  -  set `True` to allow disabling core tools. Default `False`.
- `stale_after_days` (`int`): For `prune`, stale-use cutoff. Default `30`.
- `min_enabled_age_days` (`int`): For `prune`, minimum thread config age before stale-use pruning. Default `7`.
- `dry_run` (`bool`): For `prune`, report proposed changes without saving.

**Enable response buckets:** every input tool is classified in exactly one bucket, checked in this priority order  -  (1) `Un-disabled` (was in `disabled_tools`, now removed; if the tool has a preserved `enabled_tools` or `temporary_tools` entry, it is restored AS-IS  -  the requested `ttl` does NOT apply, so a batch-level TTL can't silently promote/demote an unrelated tool; a fresh entry is only written when there is no preserved state and no default binding), (2) `Already permanent` (in `tc.enabled_tools`; TTL requests are rejected, no demotion), (3) `Already bound (default set)` (in the thread's default-bound set  -  `ALL_TOOLS` or the user-profile-level `default_thread_tools` override; already callable, no write), (4) `TTL refreshed` (in `tc.temporary_tools`; `expires_at` pushed out), (5) `Promoted to permanent` (in `tc.temporary_tools`, `ttl="never"` or `ttl="permanent"` → moved to `tc.enabled_tools`), (6) `Newly loaded` (none of the above; written fresh to `enabled_tools` or `temporary_tools` depending on `ttl`).

The classifier sources its default-bound set from the same place as graph-build (`agent._build_graph_with_prompt`: `profile.tool_preferences.default_thread_tools` if set, else `{t.name for t in ALL_TOOLS}`). Tools that live in `ALL_TOOLS` but are excluded from the user's `default_thread_tools` list are correctly treated as optional (priority-6 newly-loaded) rather than already-bound. Note: the bucket is called `Already bound (default set)`  -  not "core"  -  to avoid conflating it with the `disable` guard's "core" protection, which uses the broader `ALL_TOOLS` list.

**`disabled_tools` is authoritative in graph-build.** The graph-build pipeline is: start with the default-bound set, filter out `disabled_tools`, then add extras from `enabled_tools ∪ live_temporary_tools`  -  BUT extras are also filtered by `disabled_tools` before merging. So a tool listed in both `enabled_tools` and `disabled_tools` is unbound (disable wins). This lets `disable` be non-destructive: it only appends to `disabled_tools` and leaves `enabled_tools` / `temporary_tools` alone. An `enable` on that same tool just removes it from `disabled_tools`; the preserved permanent/TTL entry comes back automatically. Without this rule, `disable` would have to destructively mutate `enabled_tools` to actually disable an overlapping tool, and a disable→enable round-trip would silently strip the permanent badge.

**Status display filters disabled tools from the enabled sections.** Because `disabled_tools` is authoritative, a tool that has a preserved `enabled_tools` or `temporary_tools` entry while ALSO being in `disabled_tools` is currently unbound. The `status` and `search` renderers suppress such tools from the `Enabled (permanent)` / `Enabled (TTL)` sections and annotate them in the `Disabled` section with `(preserved: permanent)` or `(preserved: Xm left)`, so the user can still see what will round-trip back on un-disable without seeing the same tool in two places.

### manage_mcp

Search, preview, install, inspect, repair, and remove managed MCP servers
through the capability expansion path.

```python
manage_mcp(action: str, query: str = "", source: str = "", name: str = "", server_id: str = "", preview_token: str = "", candidate_id: str = "", confirmed: bool = False, confirmed_risk_ids: list[str] = [], config_values: dict = {}, ttl: str = "2h", auto_enable_thread: bool = True)
```

Actions are `search`, `preview`, `install`, `inspect`/`status`/`list`,
`logs`, `test`, `discover`, `retry`, `disable`, `enable`, `delete`, and
`configure_credentials`.

`preview` is non-executing. It can return multiple candidates for multi-server
JSON or prose with several install snippets; `install` selects one with
`preview_token` plus `candidate_id`. Agent direct installs still run the same
preview planner internally before any local process, package manager, Git
clone, or bundle setup is allowed.

Successful installs reload MCP server tools, enable discovered
`mcp__<server>__<tool>` tools on the current thread when
`auto_enable_thread=true`, and queue a same-turn reload with
`source="mcp_install"`. Installing MCP servers remains admin-only at execution
time because stdio servers can launch local commands.

The agent cannot receive plaintext MCP secrets. If an install needs secret
material, `manage_mcp(action="configure_credentials")` creates pending
credential-vault records and tells the user to finish in Settings >
Connections.

`ttl` controls how long discovered tools stay bound on this thread. It accepts
`Nm`, `Nh`, `Nd`, `Nw`, or `"never"`/`"permanent"`; default is `"2h"`.

### skill_manage

List, search, install, enable, disable, prune, or inspect Agent Skills.

```python
skill_manage(
    action: str,
    query: str = "",
    name: str = "",
    source: str = "installed",
    scope: str = "user",
    activate_current_thread: bool = False,
    stale_after_days: int = 30,
    min_enabled_age_days: int = 7,
    dry_run: bool = False,
)
```

Actions are `list`, `search`, `install`, `enable`, `disable`, `inspect`,
`status`, and `prune`. `prune` removes missing thread skill references,
thread-enabled skills that redundantly duplicate the user's global enabled
skill list, no-op disabled entries, and recorded-stale thread-enabled skills.
It does not remove Skill Kit tool TTL bindings; use `tool_manage(action="prune")`
for tool bindings.
Marketplace installs default to user scope; global scope requires admin.
When a skill is installed or enabled on the current thread and a graph rebuild
is needed, reload metadata uses `source="skill_install"`.

#### In-turn auto-continue

When the agent calls `tool_manage(action="enable", tools=[...])` during a turn, the enable result explicitly tells the agent to stop after that tool result. It should not write a final answer, explain the enablement, or attempt to call the newly enabled tool in the same graph invocation. The harness then:

1. Persists the enablement to the thread config (with TTL) and invalidates the cached graph.
2. Finishes the current graph invocation normally.
3. Emits a `tool_reload` SSE event (`{type: "tool_reload", tools, ttl, ttl_seconds, source, skill_name, reason}`).
4. Builds a fresh graph with the new tools bound to the LLM.
5. Injects an internal resume message (`internal_type="tool_reload_resume"`) and drives the new graph against it, streaming into the same SSE connection.

To the client this looks like one continuous turn: no extra `done` event, no separate user message. The thread lock stays held the whole time. The loop is capped at `AgentCore.MAX_TOOL_RELOADS_PER_TURN` rebuilds per user turn to bound token usage. Once the cap is hit, `tool_manage(action="enable")` and Skill Kit activation stop returning `Command(goto=END)` and instead return a plain string whose body includes a `[Reload cap hit]` notice  -  the agent can still respond in-turn, and the new binding takes effect on the next user message. This prevents an orphaned `tool_result` with no LLM follow-up (symptom: the stream looks like it froze because the last enable's `Command` ended the graph but the reload loop was already exhausted).

`astream()` (REST/SSE and sync-worker bridge callers) and `chat()` (MCP final-string path) honor the auto-continue. The streaming path emits `tool_reload` and then drives the fresh post-reload graph through the same live event conversion, so resumed `thinking`, `tool_call`, `tool_result`, `workspace_artifact`, and `response` chunks remain visible in the same turn. Scheduled TODOs, triggers, callable threads, spawned threads, and the CLI consume `astream()` through `core/stream_bridge.py`, which keeps async-only tools such as `tool_create` available outside regular chat. The bridge uses one process-local asyncio loop for synchronous callers, and async graph caches plus provider SDK HTTP pools are loop-local so FastAPI-loop chat and bridge-loop callable calls do not share loop-bound transports. Autonomous callers use `stream_and_collect()` for shared response/thinking collection, error propagation, and iteration-limit tracking before publishing their own completion payloads. `chat()` remains non-streaming and returns only the final string.

#### TTL and eviction

Each enablement (other than `ttl="never"` or `ttl="permanent"`) gets an `expires_at` timestamp stored in `ThreadConfig.temporary_tools`. At the start of every new turn, `_build_graph_with_prompt` calls `_resolve_temporary_tools(tc)` which:

1. Drops entries whose `expires_at` has passed.
2. Persists the cleaned config back to disk.
3. Returns the still-live set for inclusion in the tool list.

Eviction never happens mid-invocation, so a tool that was bound at the start of a graph run is callable for the whole run  -  there are no surprise eviction errors. Calling `enable` on a tool already in `temporary_tools` refreshes `expires_at`; calling `enable` with `ttl="never"` or `ttl="permanent"` promotes the entry into `enabled_tools` (which has no expiry and is also what the UI/API writes to). Calling `disable` adds the name to `disabled_tools` without deleting preserved permanent/TTL state, so a later enable restores that state.

Pick the shortest TTL that covers your task. Use `30m` or `2h` for short work, `7d` or `14d` for multi-day projects, and `never` only if the tool should remain as a standing capability on the thread.

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

**Security:** **SENSITIVE**  -  disabled by default. Requires explicit opt-in via user tool preferences or per-thread config.

**Availability:** This is not an always-loaded core tool. It is surfaced through self-modify or optional tool paths.

---

## Trigger Tools (Optional)

Event-driven automation  -  triggers fire agent prompts or actions in response to external events. These complement recurring TODOs, which handle time-based work.

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
- `create`  -  Create a new trigger, bound to the current thread.
- `update`  -  Patch an existing trigger's display name, enabled state, source/action config, cooldown, or conditions.
- `delete`  -  Delete a trigger permanently.

**Parameters:**
- `action` (`str`): `"create"`, `"update"`, or `"delete"`
- `trigger_id` (`Optional[str]`): Required for `update` and `delete`
- `name` (`str`): Human-friendly trigger name (e.g., `"Wake-up morning briefing"`)
- `source_type` (`str`): Event source type. Use `"webhook"` for HTTP push triggers. Call `trigger_info(action="sources")` to see available sources.
- `action_type` (`str`): What to do when triggered:
  - `"agent_prompt"`  -  send a prompt to the agent (most powerful, triggers an LLM call)
  - `"notify"`  -  send a notification to the user (no LLM call)
  - `"create_todo"`  -  create a TODO item (no LLM call)
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
- `list`  -  List trigger summaries.
- `detail`  -  Show one trigger's configuration, health, conditions, pending events, and thread binding.
- `test`  -  Dry-run one trigger with sample event data. Does not fire the trigger.
- `history`  -  Show recent execution history for one trigger.
- `sources`  -  Show available trigger source types, config fields, template variables, and examples.

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

Gives the agent a single dispatch tool that invokes the same user-facing slash commands exposed by the Discord and Telegram bots  -  so the agent can inspect and change its own backend (LLM model, tool set, memories, TODOs, env vars, notepad) without dedicated per-setting tools bloating the tool list.

> **Note:** Not loaded by default. Lives in `OPTIONAL_TOOLS`  -  enable per-thread via thread config UI or `PATCH /threads/{id}/config {"enabled_tools": ["slash_command"]}`.

### slash_command

Run a Nymeria slash command on the agent's own thread.

```python
slash_command(command: str)
```

**Parameters:**
- `command` (`str`): The slash command string (with or without a leading `/`). Values with spaces may be quoted.

**How to use:** Tell the agent to call `/help` first. The help output is the source of truth for syntax  -  the tool's own description only lists a handful of examples to keep the tool schema small.

**Example commands:**
- `/help`  -  list every supported command
- `/status`  -  model, context, tools, tasks summary
- `/config set llm_model claude-opus-4-7`  -  change global model
- `/env get PERPLEXITY_API_KEY`  -  fetch unmasked secret
- `/memory save color "deep blue"`  -  save a user memory
- `/tools enable browser`  -  turn on a category on this thread
- `/skill <name> [prompt]`  -  activate a markdown-only skill for this turn
- `/kit <name> [ttl] [prompt]`  -  activate a visible Skill Kit and bind tools
- `/skills list`  -  show skill and Skill Kit activation status on the current thread
- `/todos add Check logs | 2h | daily`  -  scheduled repeating TODO
- `/notepad write replace:new notepad contents`  -  overwrite the thread notepad

**Blocked commands:** `/ask`, `/stop`, `/clear`, `/compact`, `/restart`, `/start`  -  these would interrupt or destroy the current conversation and are rejected before any API call.

**Returns:** Markdown from the centralized command service. The REST command
endpoint also includes a structured `level` (`info`, `success`, `warning`, or
`error`) and optional `data` for richer clients.

**Runtime behavior:** Works in both normal conversation turns and autonomous scheduled TODO runs. Some tool callers still need a synchronous return value, so `slash_command` provides both sync and async invocation modes while the centralized backend registry handles parsing, metadata, aliases, and execution.

**Requirements:** Runs in-process through the command service's direct backend
adapter when a `NymeriaAgent` is active. The legacy REST compatibility path is
only a fallback for out-of-process callers and still uses
`NYMERIA_SERVICE_TOKEN` plus `X-Nymeria-Act-As`.

**Security note:** The tool runs in-process as the calling user. `/env get`
returns unmasked secrets and is admin-only on local/authenticated command
surfaces. Telegram no longer exposes `/env_get`; non-admin callers still get
403 if they try to invoke admin-gated slash commands like `/restart` or
`/config_*`.

**Implementation:** See `nymeria/tools/slash_command.py` (tool entry point) and
`nymeria/core/command_service.py` (global command registry, path metadata,
alias resolution, direct backend adapter, actor/surface filtering, and markdown
command execution).
`nymeria/triggers/slash_dispatcher.py` remains only as a compatibility shim for
legacy parsed-command callers.

---

## HTTP/API and Skill Authoring Tools (Optional)

General-purpose API primitives and authoring tools for one-off integration work, reusable HTTP/Python tools, and generated Skills or Skill Kits. These are not loaded by default; normally load `Skill(name="tool-management")` for the HTTP/API and tool-building primitives or `Skill(name="skill-management")` for the Skill authoring tools, then manage per-thread bindings with `tool_manage` when needed.

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

**Network policy:** HTTP tools are public-internet-only by default. The runtime blocks loopback, private, link-local, reserved, unspecified, multicast, and metadata targets, including hostnames that resolve to those addresses. Policy-managed requests pin the socket resolver to the approved IPs during connect, so a hostname cannot pass DNS policy and then rebind to a private address for the actual request. Internal/local access requires server-side `HTTP_INTERNAL_ALLOWLIST` entries; the model cannot opt into it per request. Blocked requests return `error.type="blocked_network_target"` and a `policy` object with the reason.

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

Draft, test, and publish reusable HTTP or Python custom tools from inside an
agent conversation.

```python
tool_create(
    action: str,
    tool_id: str = "",
    name: str = "",
    description: str = "",
    parameters: Optional[dict] = None,
    http_config: Optional[dict] = None,
    implementation_type: str = "http",
    python_code: str = "",
    entrypoint: str = "run",
    draft_id: str = "",
    sample_params: Optional[dict] = None,
    ttl: str = "2h",
    validation_timeout_seconds: int = 60,
)
```

**Actions:**
- `draft`  -  Save or update a per-user draft in `data/tool_drafts/{user_id}/`. HTTP drafts require `tool_id`, `description`, `parameters`, and `http_config`. Python drafts require `implementation_type="python"`, `python_code`, and an entrypoint function, default `run`.
- `test`  -  Execute a saved draft with `sample_params` and record whether the request succeeded. Python tests run in a subprocess.
- `publish`  -  Save a successfully tested draft into the global `data/custom_tools/` registry, reload custom tools, and enable the new tool on the current thread. Python drafts may provide `sample_params` on publish to validate and publish in one call.
- `list`  -  Show this user's drafts plus globally published custom tools without exposing request headers or bodies.
- `delete`  -  Delete this user's draft only. It does not delete a globally published tool.

**Publish semantics:** Published tools are global registry entries, so any user can discover and enable them later. They are not added to `default_thread_tools` and are not enabled by default for other users or threads. The publishing thread gets the new tool enabled with a TTL (`Nm`, `Nh`, `Nd`, `Nw`, or `never`/`permanent`; default `2h`) using the same binding path as `tool_manage(action="enable")`. With dynamic binding enabled, the tool is callable on the next model step without a graph rebuild. In legacy rebuild mode, reload metadata uses `source="tool_create"` and `reason="tool_published"` or `reason="python_tool_published"`.

**HTTP safety:** Agent-created HTTP tools reject inline secrets and `${env:...}` references. Sensitive headers are allowed only when their value uses a credential-vault reference like `${credential:cred_id.value}`.

**Python safety:** Python custom tools are stored as data-backed definitions,
not package modules, so Nymeria updates do not overwrite them and publishing
does not edit `nymeria/tools/__init__.py`. The API registers a stable wrapper
tool and runs the user code in a child Python process for validation and each
runtime invocation. Syntax errors, exceptions, `sys.exit`, process crashes, and
timeouts return tool errors instead of crashing the API process. This is crash
containment, not a malicious-code security sandbox: the child process still
runs with the backend container or process user's OS permissions. Publish-time
validation defaults to `60` seconds and can be overridden with
`validation_timeout_seconds`; published tool calls use the native
`tool_timeout` setting.

**Credential vault auth:** Authenticated custom HTTP tools should use `${credential:<credential_id>.<field>}` references in headers, query params, URLs, or bodies instead of raw values. Runtime execution resolves the reference server-side, checks the credential's allowed target, and audits the use without returning secret material to the agent.

**Audit and deferred production safety:** HTTP tool calls append redacted HTTP events to the audit log when `AUDIT_LOG_ENABLED=true`. Raw bearer/API-key-like values are best-effort redacted and custom HTTP `${env:VAR}` usage records the variable names, not the values. Credential-vault usage records credential IDs, not plaintext values. Future hardening still needs stronger per-user rate-limit budgets per task, pagination helpers, and policy hooks for actions that send messages, delete data, spend money, modify production systems, post publicly, or change infrastructure.

### auth_inspect / auth_cleanup / auth_bindings

Agent-safe credential management tools. They are default-enabled on every new
thread so the agent can inspect, clean up, and bind stored credentials without
the user pre-enabling them.

```python
auth_inspect(
    view: Literal["list", "status", "oauth_accounts"] = "list",
    credential_id: str = "",
    provider: str = "",
    kind: str = "",
    status: str = "",
    account_id: str = "",
    prompt_id: str = "",
    include_disabled: bool = False,
    limit: int = 100,
)

auth_cleanup(
    operation: Literal["stale_oauth", "disable_matching", "disable"] = "stale_oauth",
    credential_id: str = "",
    provider: str = "",
    kind: str = "",
    status: str = "",
    account_id: str = "",
    prompt_id: str = "",
    dry_run: bool = True,
    limit: int = 100,
)

auth_bindings(
    operation: Literal["bind", "unbind"] = "bind",
    credential_id: str = "",
    target_type: str = "",
    target_id: str = "",
    binding_name: str = "",
    binding_id: str = "",
)
```

`auth_inspect` returns metadata only. `auth_cleanup` can disable user-owned
credentials and keeps bulk operations dry-run by default. `auth_bindings`
binds a credential to a runtime target such as `mcp_server:<id>`,
`custom_tool:<id>`, or `native_tool:<name>`, and updates allowed targets for
runtime secret resolution. Unbinding removes the binding row and revokes the
matching allowed target when no same-target binding remains. These tools cannot
alter system credentials and never return plaintext secrets, ciphertext, or
partial key material. User prompting for new secrets belongs to
`request_credential`.

### skill_write

Write a new Skill or Skill Kit from full SKILL.md markdown, with optional
script files. This is the preferred agent-facing authoring tool from the
`skill-management` kit.

```python
skill_write(
    markdown: str,
    tools: Optional[list[str] | str] = None,
    scripts: Optional[list[dict]] = None,
    tool_ttl: Optional[str] = None,
    scope: str = "user",
    overwrite: bool = False,
    activate_current_thread: bool = True,
    dry_run: bool = False,
)
```

`markdown` must be a complete SKILL.md with YAML frontmatter and body. If
`tools` is provided, it replaces `metadata.nymeria.required_tools`; an empty
tool list creates a plain instruction-only Skill. `scripts` writes files under
`scripts/` only, rejects unsafe paths and raw secret-looking values, and
syntax-checks `.py` files before any package is written. The tool validates the
candidate directory in a temporary location first, then writes it, reloads the
skill manager, optionally enables the skill on the current thread, and queues a
same-turn skill reload when needed.

### skill_edit

Edit an existing user/global Skill's SKILL.md. V1 preserves existing scripts,
references, and assets and only rewrites the frontmatter/body.

```python
skill_edit(
    name: str,
    markdown: str = "",
    new_name: str = "",
    description: str = "",
    body: str = "",
    set_tools: Optional[list[str] | str] = None,
    add_tools: Optional[list[str] | str] = None,
    remove_tools: Optional[list[str] | str] = None,
    tool_ttl: Optional[str] = None,
    scope: str = "user",
    overwrite: bool = False,
    activate_current_thread: bool = True,
    dry_run: bool = False,
)
```

Pass `markdown` to replace the full SKILL.md, or use structured patch fields
to rename, update description/body, and set/add/remove Skill Kit required
tools. Bundled skills are read-only. Renames update the current thread's skill
references when the edited skill is active there.

## Watchdog Tools (Optional)

Tools for the Smart Watchdog  -  an intelligent scheduler thread that observes system activity and dispatches work to other threads. Not loaded by default; enable per-thread via thread config.

### activity_feed

Get a structured activity summary across all threads since a given time window.

```python
activity_feed(minutes_ago: int = 10)
```

**Parameters:**
- `minutes_ago` (`int`): Look-back window in minutes (default 10)

**Returns:** Structured text report grouped by thread showing user messages, autonomous tasks, TODO changes, and notifications. Returns "No activity" if the window is empty.

**Data source:** Reads from the persisted activity log (`data/activity/{user_id}.json`). Only as complete as what gets logged  -  user messages, TODO state changes, autonomous task execution, and notifications are all captured.

### watchdog_dispatch

Create a TODO on a different thread. Cannot target the calling thread.

```python
watchdog_dispatch(target_thread_id: str, task: str, scheduled_for: str = "now", notes: str = "")
```

**Parameters:**
- `target_thread_id` (`str`): Thread ID to dispatch the TODO to (must differ from caller)
- `task` (`str`): Clear, specific description of what the target thread should do
- `scheduled_for` (`str`): When to fire  -  `"now"`, any relative duration such as `"30s"`, `"17m"`, `"1h"`, `"1d"`, `"1w"`, or an absolute/ISO datetime
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
    tool_queries: Optional[List[str]] = None,
    tool_query_top_k: int = 8,
    optional_tools: Optional[List[str]] = None,
    tool_categories: Optional[List[str]] = None,
    disabled_tools: Optional[List[str]] = None,
    include_core_tools: bool = True,
    make_callable: bool = True,
    llm_provider: Optional[str] = None,
    llm_model: Optional[str] = None,
    llm_temperature: Optional[float] = None,
    llm_max_tokens: Optional[int] = None,
    llm_extended_thinking: Optional[bool] = None,
    llm_reasoning_effort: Optional[str] = None,
    prompt: Optional[str] = None,
    action: str = "create",
    delete_thread_id: Optional[str] = None,
    mode: str = "fresh",
    ttl_hours: Optional[int] = None,
    lifetime: Optional[str] = None,
    idle_timeout_hours: Optional[int] = None,
)
```

**Create mode (`action="create"`, default):**

- `title` (required for create): User-visible thread title. Truncated to 80 chars.
- `instructions`: Extra system-prompt instructions **APPENDED** to `soul.md` (max 5000 chars). Cannot replace the base personality. Also used as the callable tool's description if provided.
- `tool_queries`: Free-text intents (e.g. `["research", "browser automation"]`) resolved through semantic tool search. Merged with `optional_tools` and `tool_categories`.
- `tool_query_top_k`: Max matches kept per query before deduping. Default `8`.
- `optional_tools`: Exact optional tool names to enable (e.g. `["memory_clear_all", "browser_navigate"]`). Use this when semantic search misses or surgical control is needed.
- `tool_categories`: List of categories (e.g. `["email", "browser"]`) to bulk-enable every optional tool in that category. Merged with `optional_tools` and `tool_queries`.
- `disabled_tools`: List of core tool names to EXCLUDE from the new thread.
- `include_core_tools` (default `True`): If `False`, disables every core tool so the child gets only explicitly resolved or selected tools.
- `make_callable` (default `True`): If `True`, the new thread is registered as a callable tool with an auto-derived name (`spawned_{slug}_{rand8}`) and ownership is **claimed for the spawning user** in `thread_owners`. Threads owned by that same user (including the parent) can invoke it; threads owned by any other user cannot  -  the runtime gate in `agents/tool_factory.py` rejects cross-user invocations. Set `False` for a single-use thread.
- `llm_*`: Optional LLM overrides. Omit to inherit global settings.
- `prompt`: If provided, dispatches this message and **blocks** until the child responds. The child's response becomes part of this tool's output.
- `mode` (default `"fresh"`): `"fresh"` builds an empty thread. `"branched"` forks the calling thread's full checkpoint history and configuration via `branch_thread()`; the new thread starts with the parent's conversation context, then the spawn-thread overrides are layered on top. Requires a parent thread.
- `ttl_hours`: Optional lifetime. `None` is permanent. Any positive integer marks the thread temporary and auto-deletes it after that many idle hours.
- `lifetime` / `idle_timeout_hours`: Deprecated aliases kept for old prompts. `lifetime="temporary"` maps to `ttl_hours=idle_timeout_hours or 24`.

**Create returns:**
- Preamble with the new `thread_id` (`spawned-{slug}-{rand8}`).
- If `make_callable=True`: the generated callable tool name (e.g. `spawned_research_a3f21c9d`) the parent can invoke later.
- If `tool_queries` resolved anything: a `[Resolved tools]: ...` line with the selected tool names and scores.
- If `prompt` provided: the child's response text appended.
- A reminder of the `action="delete"` call needed to remove the thread.

**Delete mode (`action="delete"`):**

- `delete_thread_id` (required for delete): The spawned thread's ID (must start with `"spawned-"`).
- Only the **calling thread** (the original spawn parent, tracked via `platform_meta.spawn_parent`) can delete a given spawned thread. If the stored spawn_parent is empty (e.g. an older spawn without lineage), any thread may delete it.
- Cleans up: metadata, config, checkpoints (SQLite or Postgres), notepad, and  -  if the thread was callable  -  unregisters the tool globally via `sync_agent_tools()`.
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

Any thread with `callable=True` in its thread config becomes a callable tool  -  there are no hardcoded agent names or fixed configurations. Each callable thread is fully configurable via the UI:

- **Name**: The tool name equals the thread's sidebar title (synced via `callable_name` in thread config)
- **Model**: Set per-thread via `llm_config.model` (inherits global default if not set)
- **Tools**: Enable/disable any optional tools per-thread
- **System prompt**: Custom `system_prompt` or `instructions` per-thread

Create a callable thread: open thread settings → Agent → check "Make Callable" → set a name and description. On desktop, the thread row's Agent shortcut opens this tab directly. The callable becomes available as a tool to **threads owned by the same user** after `sync_agent_tools()` runs. Other users do not see the callable in their tool list, and the runtime ownership gate rejects invocation by any non-owner, including admin-as-admin. Admins can act-as the owning user via `X-Nymeria-Act-As` to test or trigger another user's callable.

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

For handoffs, `scheduled_for` can delay execution with any relative duration such as `"30s"`, `"17m"`, `"1h"`, `"1d"`, `"1w"`, or an absolute/ISO datetime; delayed handoffs are stored as scheduled TODOs on the target thread. Omit `scheduled_for` for immediate handoff. For immediate handoffs, `if_busy="queue"` lets the target thread wait for its lock in the background, while `if_busy="error"` returns a busy response if the target is already running. In blocking ask mode, `if_busy="error"` performs a best-effort busy check before waiting.

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

**Network policy:** Browser navigation, Playwright HTTP(S) subrequests, and fallback requests all use Nymeria's HTTP egress policy. Loopback, private, link-local, metadata, and blocked-domain targets are rejected unless explicitly allowed by the operator.

**Fallback mode:** Set `BROWSER_FORCE_FALLBACK=true` in `.env` to skip Playwright entirely and use requests+BeautifulSoup for navigation and content extraction. Fallback HTTP requests always verify TLS certificates; `BROWSER_VERIFY_SSL` is deprecated and ignored.

---

### Outlook Tools (18)

Used internally by OutlookAgent. Also available as **optional tools** for per-thread enabling. Defined in `tools/outlook_email.py` (13 email) and `tools/outlook_attachments.py` (1 attachment).
Microsoft tokens land in the credential vault as `kind=oauth_token` via the unified `request_credential(provider="outlook", kind="oauth")` flow; legacy file caches at `data/auth_tokens/<user_id>/microsoft.json` are still consulted as a read-only fallback for backwards compatibility.

**Authentication:**

The agent connects Outlook by calling `request_credential(provider="outlook", kind="oauth")`. Outlook supports both `auth_code` (browser redirect, default when `NYMERIA_PUBLIC_URL` is set) and `device_code` (RFC 8628 short code, used automatically when `NYMERIA_PUBLIC_URL` is unset). To disconnect an Outlook account the user removes the credential from Settings → Connections or the agent calls `auth_cleanup(operation="disable", credential_id=...)`.

**Email tools:**

| Tool | Signature | Description |
|------|-----------|-------------|
| `outlook_list_emails` | `(account_id?, limit=10, folder="inbox", unread_only=False)` | List recent emails with preview and thread ID. Limit max 50. |
| `outlook_get_email` | `(email_id?, email_ids?, account_id?)` | Get full email details including body and attachment metadata. Batch via comma-separated IDs. |
| `outlook_search_emails` | `(query?, queries?, sender?, to?, subject?, folder?, category?, days_back=0, has_attachments=False, thread_id?, kql?, account_id?, limit=10)` | Search emails with filters. Results include body preview (120 chars) and thread ID. Without `days_back`, results ranked by relevance not date. Use `thread_id` to pull full conversation chain. Use `kql` for raw KQL queries (OR logic, etc). Use `category` to find tagged emails. |
| `outlook_send_email` | `(to, subject, body, account_id?, cc?, bcc?, is_html=False)` | Send a new email. |
| `outlook_reply_email` | `(email_id, body, account_id?, reply_all=False)` | Reply to an email (sends immediately). |
| `outlook_draft_reply` | `(email_id, body, reply_all=False, is_html=False, account_id?)` | Create an unsent reply draft that preserves the email thread. You review and send it manually. |
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

### Gmail Authentication

Gmail uses the unified `request_credential(provider="google_gmail", kind="oauth")` flow. Tokens land in the vault as `kind=oauth_token` with the Gmail scope set. The provider descriptor has a `post_save_hook` that exports a google-auth-library compatible token file to `data/auth_tokens/<user_id>/mcp/gmail/credentials.json` whenever Gmail is connected; the matching `post_clear_hook` removes that file on disconnect.

Managed MCP install/retry/create automatically applies the exported file as `GMAIL_CREDENTIALS_PATH` for `@gongrzhe/server-gmail-autoauth-mcp` and applies `GOOGLE_OAUTH_CREDENTIALS` as `GMAIL_OAUTH_PATH`. Google scopes are fixed at consent time, so a Calendar or Docs token that does not already include Gmail scopes will not satisfy the MCP server; the agent has to ask the user to connect Gmail explicitly.

---

### Calendar Tools (11)

Native Python Google Calendar API client. Defined in `tools/calendar.py`. Uses `google-api-python-client` for direct API calls and the unified credential flow for OAuth.

**Authentication:**

The agent connects Google Calendar by calling `request_credential(provider="google_calendar", kind="oauth")`. Tokens land in the vault as `kind=oauth_token`. Calendar API calls go through `tools/auth_cache_utils.get_google_credentials`, which performs a vault-first lookup with legacy file-cache fallback at `data/auth_tokens/<user_id>/google_calendar.json`. Token refresh writes the new access token back to the vault.

**Event tools:**

| Tool | Signature | Description |
|------|-----------|-------------|
| `calendar_list_calendars` | `(account_id?)` | List all available Google calendars. |
| `calendar_list_events` | `(calendar_id="primary", max_results=10, time_min?, time_max?, account_id?)` | List events from a calendar. Output includes full event IDs for chaining into `calendar_get_event`. |
| `calendar_get_event` | `(event_id, calendar_id="primary", account_id?)` | Get full event details. |
| `calendar_search_events` | `(query, calendar_id="primary", max_results=10, account_id?)` | Search events by text. Output includes full event IDs for chaining into `calendar_get_event`. |
| `calendar_create_event` | `(summary, start_time, end_time, calendar_id="primary", description?, location?, attendees?, timezone?, account_id?)` | Create a new event. Supports all-day (date-only) and timed events. |
| `calendar_update_event` | `(event_id, calendar_id="primary", summary?, start_time?, end_time?, description?, location?, account_id?)` | Update an existing event (patch  -  only sends changed fields). |
| `calendar_delete_event` | `(event_id, calendar_id="primary", account_id?)` | Delete a calendar event. |
| `calendar_respond_to_event` | `(event_id, response, calendar_id="primary", account_id?)` | Respond to invitation: `"accepted"`, `"declined"`, `"tentative"`. |
| `calendar_get_freebusy` | `(time_min, time_max, calendars?, account_id?)` | Get free/busy info. `calendars` is comma-separated IDs. |
| `calendar_get_current_time` | `()` | Get current time in ISO 8601 (no API call  -  local system time). |
| `calendar_list_colors` | `(account_id?)` | List available event colors. |

**Requires:** `GOOGLE_OAUTH_CREDENTIALS` env var pointing to the OAuth Desktop App credentials JSON from Google Cloud Console. Tokens are stored per Nymeria user at `data/auth_tokens/<user_id>/google_calendar.json` with auto-refresh. Node.js/npx are **not** required.

---

### Google Workspace Authentication

The Google Docs, Drive, Sheets, Tasks, Contacts, Slides, and Chat tools share one OAuth connection. The agent connects it via `request_credential(provider="google_docs", kind="oauth")`. Tokens land in the vault as `kind=oauth_token` with the union of Workspace scopes. Workspace API calls read the vault first and fall back to the legacy file cache at `data/auth_tokens/<user_id>/google_docs.json` while older accounts are still being migrated.

### Google Workspace Service Tools (34)

These optional tools reuse the same Workspace OAuth connection. Existing Google accounts authenticated before Tasks/Contacts/Slides/Chat support may need to reconnect via `request_credential` so the stored token includes the broader Workspace scopes.

- Tasks: `google_tasks_list_tasklists`, `google_tasks_list_tasks`, `google_tasks_get_task`, `google_tasks_create_task`, `google_tasks_update_task`, `google_tasks_complete_task`, and `google_tasks_delete_task`. Listing/get are SAFE; create/update/complete/delete are MODERATE.
- Contacts: `google_contacts_list_contacts`, `google_contacts_get_contact`, `google_contacts_create_contact`, `google_contacts_update_contact`, and `google_contacts_delete_contact`. Listing/get are SAFE; create/update/delete are MODERATE.
- Drive: `google_drive_search_files`, `google_drive_get_file`, `google_drive_download_text`, `google_drive_create_folder`, `google_drive_upload_text_file`, and `google_drive_trash_file`. Search/get/download are SAFE; folder creation, upload, and trash updates are MODERATE.
- Slides: `google_slides_get_presentation`, `google_slides_list_slides`, `google_slides_get_page_thumbnail`, `google_slides_create_presentation`, `google_slides_create_slide`, `google_slides_replace_text`, and `google_slides_batch_update`. Get/list/thumbnail are SAFE; create and batch update operations are MODERATE.
- Chat: `google_chat_list_spaces`, `google_chat_get_space`, `google_chat_list_members`, `google_chat_get_member`, `google_chat_list_messages`, `google_chat_get_message`, `google_chat_send_message`, `google_chat_update_message`, and `google_chat_delete_message`. List/get tools are SAFE; send/update/delete are MODERATE.

---

### Google Analytics Authentication

Google Analytics uses its own OAuth connection so Analytics scopes do not force re-authentication for existing Google Docs/Drive accounts. The agent connects it via `request_credential(provider="google_analytics", kind="oauth")`. Tokens land in the vault; the legacy file cache at `data/auth_tokens/<user_id>/google_analytics.json` is consulted as a read-only fallback.

### Google Analytics Service Tools (4)

These optional tools reuse the Google Analytics OAuth connection and are read-only:
- `google_analytics_list_account_summaries(page_size?, page_token?, account_id?)` lists visible Analytics accounts and GA4 properties.
- `google_analytics_get_metadata(property_id, account_id?)` lists available GA4 dimensions and metrics.
- `google_analytics_run_report(property_id, metrics?, dimensions?, start_date?, end_date?, limit?, offset?, order_bys_json?, filters_json?, keep_empty_rows?, account_id?)` runs a GA4 Data API report.
- `google_analytics_run_realtime_report(property_id, metrics?, dimensions?, limit?, order_bys_json?, filters_json?, account_id?)` runs a GA4 realtime report.

---

### Google Business Profile Authentication

Google Business Profile uses its own OAuth connection and requires the Google Business Profile APIs to be enabled for the OAuth client. The agent connects it via `request_credential(provider="google_business_profile", kind="oauth")`. Tokens land in the vault; the legacy file cache at `data/auth_tokens/<user_id>/google_business_profile.json` is consulted as a read-only fallback.

### Google Business Profile Service Tools (11)

These optional tools reuse the Google Business Profile OAuth connection:
- `google_business_profile_list_profile_accounts(page_size?, page_token?, account_id?)` lists managed Business Profile accounts.
- `google_business_profile_list_locations(account_name, read_mask?, page_size?, page_token?, account_id?)` lists locations for an account.
- `google_business_profile_list_reviews(account_name, location_name, page_size?, page_token?, order_by?, account_id?)` and `google_business_profile_get_review(review_name, account_name?, location_name?, account_id?)` read reviews.
- `google_business_profile_reply_to_review(review_name, comment, account_name?, location_name?, account_id?)` and `google_business_profile_delete_review_reply(review_name, account_name?, location_name?, account_id?)` manage business replies.
- `google_business_profile_list_posts(account_name, location_name, page_size?, page_token?, account_id?)`, `google_business_profile_get_post(post_name, account_name?, location_name?, account_id?)`, `google_business_profile_create_post(...)`, `google_business_profile_update_post(...)`, and `google_business_profile_delete_post(...)` manage local posts.

---

### SelfModify Tools (8)

Used internally by SelfModifyAgent. Defined in `core/self_agent.py`. **Read** and **list** operations work on any path within the project root. **Write**, **delete**, and **reload** are enabled by default for admin-only maintenance tools; set `NYMERIA_ALLOW_SELF_EDIT=false` to disable them. Writes/deletes are still restricted to `nymeria/tools/`, `nymeria/agents/`, and `nymeria/triggers/sources/`.

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
- Google Docs tools: 4 auth + 17 document + 34 Workspace = 55 total
- Google Analytics tools: 4 auth + 4 report = 8 total
- Google Business Profile tools: 4 auth + 11 profile = 15 total
- Watchdog tools: `activity_feed`, `watchdog_dispatch`, `watchdog_read_notepad`, `watchdog_todo_overview` = 4
- Utility tools: `claude_code`, `tool_search`, `tool_manage`, `manage_mcp`, `skill_manage`, `http_request`, `api_discover`, `tool_create`, `skill_write`, `skill_edit` plus the admin-only diagnostic `hello_test` used for dynamic-load validation

**How it works:**
1. `OPTIONAL_TOOLS` in `tools/__init__.py` maps tool names to tool objects
2. Per-thread config has an `enabled_tools` list (tool names)
3. The profile-level `default_thread_tools` list is the default-bound core set for each thread; an empty list means no core tools
4. During `_build_graph_with_prompt()`, enabled optional tools are added to the thread's tool set
5. Users enable or disable optional tools via thread settings or `PATCH /threads/{id}/config`

Effective tools are resolved as `(default_thread_tools ∪ enabled_tools) - disabled_tools`.
This means promoting an optional tool to the profile defaults makes it available
to existing threads on the next graph build, unless that thread already lists the
tool in `disabled_tools`. The desktop UI preserves those disabled overrides even
when a tool is not currently in the default set, so a thread-specific opt-out
continues to apply if the tool is promoted later.

The desktop/mobile Thread Settings UI mirrors this split: the Tools tab shows non-MCP tools from `default_thread_tools` plus non-MCP optional tools, while the MCP tab shows MCP-discovered tools. Default MCP tools can be disabled per thread; non-default MCP tools can be enabled per thread. Tool discovery is role-filtered; `hello_test` remains in `OPTIONAL_TOOLS` for admin/test validation but is hidden from non-admin search/listing surfaces and rejected by non-admin enable paths.

**Important:** `OPTIONAL_TOOLS` currently includes more than just integrations. It also contains admin-only tools like `claude_code`, `reload_all`, and `self_modify_rollback`.

---

## Custom Tools

Custom tools extend Nymeria's capabilities through saved HTTP definitions,
managed MCP definitions, or subprocess-backed Python functions. They can be
created via the **Desktop UI** (Settings → Tools), the **REST API**, or the
agent-facing `tool_create` workflow.

### Tool Types

| Type | Description | Use Case |
|------|-------------|----------|
| **HTTP** | Makes REST API calls to external services | Integrate with APIs, webhooks, web services |
| **MCP** | Connects to Model Context Protocol servers | Use existing MCP tools, complex integrations |
| **Python** | Runs a tested entrypoint function in a subprocess | Small deterministic helpers that do not need a network API |

### HTTP Tools

HTTP tools make REST API calls with configurable:
- **Method**: GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS
- **URL**: Supports `${param}` interpolation for dynamic URLs
- **Headers**: Public headers can be plain strings. Sensitive headers must use
  credential-vault references such as `${credential:cred_id.value}`.
- **Body Template**: JSON template with parameter placeholders
- **Response Path**: JSONPath to extract specific data from response

Agent-created HTTP tools reject raw secrets and `${env:...}` secret references
in URLs, headers, query params, and bodies. Store credentials in the vault,
bind them to `custom_tool:<tool_id>`, and reference them with
`${credential:<credential_id>.<field>}`.

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
      "Authorization": "Bearer ${credential:cred_weather.value}"
    },
    "response_path": "$.data.temperature"
  }
}
```

### MCP Tools

MCP tools connect to external MCP servers via JSON-RPC over stdio or HTTP.

The MCP paste installer accepts Claude Desktop/Cursor/Windsurf-style JSON,
raw server objects, multi-server JSON, fenced commands, README prose,
env-prefixed commands, HTTP/SSE URLs, npm package pages, PyPI package pages,
Git repository URLs, registry ids, bundle URLs, and uploaded
`.mcpb`/`.dxt`/`.zip` bundles. Preview is the UI entrypoint and does not run
anything. It returns candidate server definitions, risk signals, install
steps, credential requirements, and warnings. Install requires a selected
preview candidate and explicit risk approval for downloaded code runners such
as `npx`, `uvx`, Git, local paths, and bundles.

Pasted MCP secrets are stored through the credential vault, not in MCP server
JSON. Secret-like env vars and HTTP headers are converted to
`${credential:<id>.value}` references; missing secrets create
`pending_setup` credentials scoped to `mcp_server:<server_id>`. New managed
installs do not write plaintext tokens/API keys/passwords or legacy
`encrypted_env_vars`.

Managed MCP servers and their discovered tools are configured from the dedicated **Settings → MCP** tab on desktop and mobile. Legacy user-created MCP custom tools remain under **Settings → Tools** with the other custom tools.

Managed server definitions track install phases: `previewed`, `approved`,
`preparing`, `discovering`, `ready`, `failed`, `needs_config`, and `disabled`.
Logs and last errors are saved with secret redaction so failed installs can be
inspected and retried.

Discovered MCP tools are dynamic registry tools internally named
`mcp__<server_id>__<tool_name>`. They are not entries in `OPTIONAL_TOOLS`.
Management surfaces should show readable labels like `<server name> / <tool>`
or the raw MCP tool name, and reserve the internal `mcp__...` identifier for
saved tool bindings, registry metadata, and troubleshooting.
Metadata remains available for installed servers with live/enabled flags so
the UI can distinguish known-but-unavailable tools from callable tools.
Reload, disable, delete, and failed rediscovery unregister stale live tools and
prune stale `mcp__...` names from user defaults and thread tool bindings.

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

**Storage:** Custom tools are stored as JSON files in `data/custom_tools/`, one `.json` per tool ID. HTTP and MCP tools are declarative definitions. Python tools store source code in the same JSON manifest and execute through the subprocess wrapper.

**Agent-created tools:** `tool_create(action="publish")` writes the same JSON definition format into `data/custom_tools/`, then reloads the custom-tool loader and registers metadata so `tool_search(query=...)` can find the new tool. Agent-created tools are global but remain opt-in per thread. The publishing thread is auto-enabled with the requested TTL through the same hot-load path as `tool_manage`.

### HexStrike MCP Sidecar

HexStrike AI is available as an optional Kali-based sidecar through
`docker-compose.hexstrike.yml`. It exposes a curated subset of upstream
HexStrike tools over Streamable HTTP at `http://hexstrike-mcp:8889/mcp` for
Nymeria containers and `http://localhost:8889/mcp` for local MCP clients. See
`docs/hexstrike-mcp.md` for build, registration, and allowlist details.

---

## Scheduled TODO Execution

Nymeria operates autonomously 24/7 through **scheduled TODOs**  -  TODOs with a `scheduled_for` datetime that are automatically executed when due.

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

### Integration sub-grouping

The `integrations` category holds roughly 1,000 tools, so it carries a second
level of structure for the tool menus: a functional **group** (level 1, e.g.
`crm_sales`, `messaging_chat`) and a **service**/provider (level 2, e.g.
`salesforce`, `aws_ses`). These come from `tools/integration_taxonomy.py`
(`get_integration_grouping(tool_name)`, re-exported through `metadata.py`); the
UI-facing builders spread `metadata.integration_grouping_fields(name, category)`
into their payloads (`group`, `group_label`, `service`, `service_label`), null
for every non-integration tool. `category` itself stays `integrations`, so
security inference and plugin category overrides are unaffected.

`service` is the longest underscore-delimited tool-name prefix in
`SERVICE_REGISTRY` (so `aws_ses` wins over `aws`); a tool added before the
registry is regenerated falls back to its first segment under the `other` group,
so nothing is hidden. The file is a generated artifact (an agent swarm derives
the service labels and group rollup, then a deterministic pass validates 100%
coverage and prefix determinism). The frontend owns each group's icon and order
in `nymeria-desktop/src/lib/utils/toolCategories.ts` (`GROUP_INFO`); keep it in
sync with `GROUP_META` when regenerating.

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

**SAFE:** `file_read`, `web_search_perplexity`, `web_search_tavily`, `web_search_exa_ai`, `web_search_firecrawl`, `web_search_brave`, `web_search_searxng`, `fetch_url_nymeria`, `consult`, `memory_add`, `memory_edit`, `memory_read`, `personality_set`, `rag_search`, `nym_todo`, `nym_todo_delete`, `nym_todo_list`

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
