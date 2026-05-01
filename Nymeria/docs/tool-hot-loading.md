# Tool Hot-Loading (In-Turn Auto-Reload)

How Nymeria enables and uses optional tools within a single user turn, without requiring a round-trip to the user.

## Problem

LangGraph binds tools to the LLM at graph compilation time via `llm.bind_tools()`. Once a graph invocation starts, the tool list is frozen. When the agent discovers it needs a tool it doesn't have (e.g. `pdf_write`), calling `tool_search(action="enable")` persists the enablement but the tool isn't callable until the **next** graph invocation — which normally means the next user message.

This breaks the autonomous "search, enable, use" flow:

```
tool_search("pdf")          → finds pdf_view, pdf_edit, pdf_write
tool_search(enable, [...])  → persists to thread config
pdf_write(...)              → fails: not bound to the LLM
```

## Solution: In-Stream Graph Rebuild

The fix reuses Nymeria's existing auto-compact primitive. When auto-compact fires, `astream()` finishes the current graph invocation, builds a fresh graph, injects a synthetic `HumanMessage`, and streams the second invocation into the **same SSE connection**. The client sees one continuous turn.

Tool hot-loading does the same thing:

```
User: "convert report.docx to PDF"
  │
  ├─ Graph invocation #1 (no pdf_* tools bound)
  │    ├─ tool_call: tool_search(action="search", query="pdf")
  │    ├─ tool_result: pdf_view, pdf_edit, pdf_write found
  │    ├─ tool_call: tool_search(action="enable", tools=["pdf_write"])
  │    │    ├─ Persists to thread config (with TTL)
  │    │    ├─ Sets agent._pending_tool_reload[thread_id]
  │    │    ├─ Invalidates cached graph
  │    │    └─ Returns Command(goto=END) with "STOP NOW" guidance → forces graph to finish
  │    └─ Graph ends (no final AIMessage — Command routes to __end__)
  │
  ├─ astream() reload loop fires
  │    ├─ Pops _pending_tool_reload[thread_id]
  │    ├─ Yields {type: "tool_reload", tools: [...]} SSE event
  │    ├─ Builds FRESH graph (now includes pdf_write)
  │    └─ Injects HumanMessage(internal_type="tool_reload_resume")
  │
  ├─ Graph invocation #2 (pdf_write now bound)
  │    ├─ tool_call: pdf_write(...)
  │    └─ Final response: "Done, here's your PDF."
  │
  └─ done event
```

From the client's perspective, the stream never stops. One user message in, one continuous response out.

From the model's perspective, enabling and using a new tool are two separate steps. After an enable result that queues a reload, the model must stop immediately: no final answer, no explanatory text, and no follow-up tool call. The system injects `tool_reload_resume` after the fresh graph has the new tools bound; only that resumed step should continue the user's task and call the newly enabled tools.

## Skill Kit Binding

Skill Kits use the same reload path as `tool_search`. When `Skill(name=...)`
activates a skill whose `metadata.nymeria.required_tools` list contains tools
that are not currently bound, the Skill tool:

1. Validates every required tool strictly (unknown, unloadable, or admin-only
   dependencies fail before any config write).
2. Writes the required tools to `temporary_tools` or `enabled_tools` using the
   skill's `metadata.nymeria.tool_ttl` (`2h` by default).
3. Removes required tools from `disabled_tools` when needed, matching
   `tool_search(action="enable")`.
4. Queues `_pending_tool_reload[thread_id]` with `source="skill_kit"`,
   `skill_name`, and `reason`.
5. Returns the skill body plus STOP guidance in a `Command(goto=END)` so the
   resumed graph has both the instructions and the newly bound tool schemas.

`allowed-tools` remains advisory Agent Skills metadata; it does not bind
Nymeria tools. Activation warnings are only emitted when an `allowed-tools`
entry is also a known Nymeria tool name that is missing from the current
thread; portable names such as `Read`, `Write`, and `Bash(...)` stay quiet.

## Tool Create Reloads

`tool_create(action="publish")` also uses the reload loop after it writes a
validated HTTP tool definition, reloads the custom-tool registry, and enables
the new tool on the publishing thread. These reloads carry
`source="tool_create"` and `reason="tool_published"` so history, resume text,
and frontend indicators can distinguish agent-authored tools from a normal
`tool_search(action="enable")` request.

## Skill Publish Reloads

`skill_config(action="publish", activate_current_thread=true)` uses the same
reload loop after it writes a validated `SKILL.md`, reloads `SkillManager`,
and updates `ThreadConfig.enabled_skills`. In this case the reload refreshes
the generated `Skill` meta-tool index rather than binding a normal tool, so
the emitted `tool_reload` event may have `tools: []` with
`source="skill_config"`, `skill_name`, and `reason="skill_published"`.

## TTL (Time-to-Live) Enablements

Not every tool enable should be permanent. A one-shot PDF conversion doesn't need `pdf_write` bound forever. TTL lets the agent pick the right lifetime.

### Presets

| Key | Duration | Use case |
|-----|----------|----------|
| `30m` | 30 minutes | One-shot operations |
| `2h` | 2 hours | Multi-step tasks (default) |
| `6h` | 6 hours | Sustained workflows |
| `24h` | 24 hours | Long-running sessions |
| `permanent` | Never expires | Standing capability |

### Storage Split

`ThreadConfig` has two fields for enabled tools:

- **`enabled_tools: List[str]`** — Permanent enablements. Written by the UI, API (`PATCH /threads/{id}/config`), `spawn_thread`, and `tool_search(ttl="permanent")`. Unchanged schema means zero back-compat risk for existing callers.

- **`temporary_tools: Dict[str, TemporaryToolEntry]`** — TTL'd enablements, agent-managed. Each entry has `enabled_at` and `expires_at` timestamps. This is the new field.

Both fields are merged at graph-build time: `extra_names = (set(tc.enabled_tools) | live_temp) - disabled`.

### Lazy Eviction

No background scheduler. At graph-build time, `_resolve_temporary_tools()` filters out expired entries and persists the cleaned config. A tool that was live when the graph was built stays callable for the whole invocation — no surprise mid-turn eviction.

### Sliding Renewal

Calling `tool_search(action="enable")` on a tool already in `temporary_tools` refreshes its `expires_at`. Calling with `ttl="permanent"` promotes it from `temporary_tools` into `enabled_tools`.

### Disable Preserves State

When a tool is disabled, it's added to `disabled_tools` but **not** removed from `enabled_tools` or `temporary_tools`. This preserves the original classification so un-disabling restores it as-is (permanent stays permanent, TTL keeps its original expiry).

## Code Reference

### Entry Point: `tool_search()` — `tools/tool_search.py:703`

The `@tool` function dispatches on `action`:

| Action | Handler | Returns |
|--------|---------|---------|
| `search` | `_search()` `:174` | String with up to 15 results, annotated with `[ENABLED Xh Ym left]` or `[DISABLED]` |
| `enable` | `_enable()` `:246` | `Command(goto=END)` if reload needed, plain string otherwise |
| `disable` | `_disable()` `:538` | String summary |
| `status` | `_status()` `:652` | Thread's full tool status (permanent, TTL, disabled sections) |
| `list_categories` | `_list_categories()` `:632` | All categories with tool counts |

### Enable Classification: `_enable()` — `tools/tool_search.py:246`

Each requested tool is classified into exactly one bucket (checked in this priority order):

| Bucket | Condition | What happens |
|--------|-----------|-------------|
| **invalid** | Not in catalog | Reported in `[Not found]` |
| **unloadable** | In catalog but can't be resolved (e.g. disabled MCP server) | Reported in `[Unloadable]` |
| **un_disabled** | In `tc.disabled_tools` | Removed from disabled list. Preserved state restored if it exists; otherwise fresh entry written with requested TTL |
| **already_permanent** | In `tc.enabled_tools` and not disabled | No-op |
| **already_default** | Part of thread's default-bound tool set | No-op (already callable) |
| **refreshed / promoted** | In `tc.temporary_tools` | `ttl="permanent"` promotes to `enabled_tools`; otherwise refreshes `expires_at` |
| **newly_added** | None of the above | Written to `enabled_tools` (permanent) or `temporary_tools` (TTL'd) |

After classification, if `newly_added` or `un_disabled` is non-empty **and** the reload cap hasn't been hit:

1. Sets `agent._pending_tool_reload[thread_id]` with the new tool names and TTL info (`:462`)
2. Returns `Command(goto=END, update={"messages": [ToolMessage(...)]})` (`:527`) with explicit "STOP NOW" wording — this forces the graph to end cleanly after the tool result, handing control back to `astream()`.

If the reload cap is already hit, returns a plain string instead. The enablement is still persisted, but the tool won't be bound until the next user message.

### Reload Loop: `astream()` — `core/agent.py:4132`

After the first graph invocation completes, `astream()` enters the reload loop:

```python
reload_count = 0
while reload_count < self.MAX_TOOL_RELOADS_PER_TURN:  # default 3
    if abort_event.is_set():
        break
    reload_info = self._pending_tool_reload.pop(thread_id, None)
    if not reload_info:
        break
    reload_count += 1
    self._turn_reload_count[thread_id] = reload_count
    ...
```

Each iteration:

1. **Yields a `tool_reload` SSE event** (`:4149`) — frontends use this to render the reload/resume message between the pre-reload and post-reload response segments.

2. **Invalidates the graph cache** and builds a fresh graph via `_get_async_graph_for_user()` (`:4159-4162`). The new graph has the just-enabled tools bound to the LLM.

3. **Constructs a resume prompt** (`:4175-4178`):
   ```
   [System: tools pdf_write are now loaded for the next 2h.
   Continue the user's task using the new tools.]
   ```

4. **Injects it as an internal HumanMessage** (`:4179-4184`) with `internal_type="tool_reload_resume"`. This is what the checkpointer sees as the "user message" that triggers the second invocation.

5. **Drives the fresh graph** via the same `_drive_graph_events()` inner function (`:4186`), streaming events into the same SSE connection.

6. **Reassigns `graph = reload_graph`** (`:4192`) so post-loop logic (token tracking, dangling-tool-call patching) operates on the most recent graph.

The sync `chat()` method has an identical loop at `:3211` using `graph.invoke()` instead of `astream_events()`.

### Graph Build: `_build_async_graph_with_prompt()` — `core/agent.py:2334`

At `:2398`, the graph builder calls `_resolve_temporary_tools(tc)` to get the set of live TTL'd tool names, then merges them with permanent enablements:

```python
live_temp = self._resolve_temporary_tools(tc)
extra_names = (set(tc.enabled_tools) | live_temp) - disabled
```

Tools are looked up in `ALL_TOOLS`, then `OPTIONAL_TOOLS`, then the tool registry (for MCP/custom tools).

### TTL Eviction: `_resolve_temporary_tools()` — `core/agent.py:2802`

```python
def _resolve_temporary_tools(self, tc) -> set:
    now = datetime.utcnow()
    live = {name: entry for name, entry in tc.temporary_tools.items()
            if entry.expires_at > now}
    if len(live) != len(tc.temporary_tools):
        # Log evicted tools, persist cleaned config
        tc.temporary_tools = live
        self.thread_config_manager.save_config(tc)
    return set(live.keys())
```

### History Filter: `get_conversation_history()` — `core/agent.py:4365`

The `tool_reload_resume` HumanMessage is internal plumbing and is not returned
as a normal user message. Its text is captured as reload metadata so frontends
can render a visible "resume message" between the two assistant bubbles. The AI
response that follows it (the tool calls, the report) **must** remain visible.

The internal message filter handles this by skipping the prompt but keeping `skip_until_next_human = False`:

```python
elif internal_type == 'tool_reload_resume':
    skip_until_next_human = False  # Show the agent's response
    continue                       # Hide the raw internal message
```

This matches the treatment of `autonomous_wakeup`. The AI messages from the second invocation are then picked up by the turn consolidation logic and rendered as a continuation of the assistant's turn — or as a separate message bubble if there was no active turn (which happens when `Command(goto=END)` ended the first invocation without a final AIMessage).

### ThreadConfig Model: `core/thread_config.py:42`

```python
class TemporaryToolEntry(BaseModel):
    """A tool enabled for this thread with a time-to-live.
    Eviction is lazy: expired entries are filtered out at graph-build time."""
    enabled_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
```

The `temporary_tools` field at `:65`:
```python
temporary_tools: Dict[str, TemporaryToolEntry] = Field(default_factory=dict)
```

### Metadata: `tools/metadata.py:983`

The `tool_search` entry's description was updated to mention auto-continue and TTL:

> "Search, enable, and disable optional tools for the current thread. Enabling auto-continues the turn with the new tools bound (no need to wait for the next user message). Enablements have a TTL (default 2h) — pick shortest needed or use 'permanent'."

## Safeguards

### Reload Cap

`MAX_TOOL_RELOADS_PER_TURN = 3` (`:386`). After this many reloads in one user message, `_enable()` still persists the enablement but returns a plain string instead of `Command(goto=END)`, deferring the bind to the next turn. This prevents pathological `enable -> enable -> enable -> ...` loops.

The cap is tracked via `_turn_reload_count[thread_id]` (`:510`), reset at the start of each turn (`:4125`), incremented by the reload loop (`:4140`), and read by `_enable()` (`:458`).

### Abort Signal

The reload loop checks `abort_event.is_set()` before each iteration (`:4134`). If the user hits Stop (`POST /threads/{id}/stop`), no further reloads are attempted.

### Cleanup

`_pending_tool_reload` is cleared in three places to prevent stale entries:

1. **Inside the loop** — `pop()` consumes the entry (`:4136`)
2. **After the loop** — drains any residual entry (`:4196`)
3. **In `finally`** — catches exceptions and early exits (`:4289`)

`_turn_reload_count` is cleaned up in `finally` at `:4288`.

At the start of every new `chat()`, `stream()`, and `astream()` turn, Nymeria
also discards any pre-existing pending reload for that thread before resetting
the per-turn counter. A pending reload is only valid inside the top-level turn
that created it; carrying it into the next user message would attach a stale
Tool Binding event to unrelated output.

The legacy sync `stream()` path is used by callable thread execution. It does
not run the in-turn reload loop; if a tool enable queues a reload there, the
enablement is still persisted to thread config for the next turn, and the
in-memory pending flag is cleared when the sync stream exits.

### Dangling Tool Calls

The `finally` block at `:4280` patches dangling tool calls for **both** invocations, since `graph` was reassigned to `reload_graph` (`:4192`).

### Idempotence

If `tool_search(action="enable")` is called with tools that are already enabled, no reload flag is set (they fall into `already_permanent`, `already_default`, or `refreshed` buckets). No auto-continue triggers, the turn proceeds normally.

## SSE Event Protocol

The new `tool_reload` event sits between the two graph invocations:

```
tool_call(tool_search enable) → tool_result → tool_reload → [second invocation events] → done
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | `"tool_reload"` | Event type |
| `tools` | `string[]` | Names of newly-loaded tools |
| `ttl` | `string` | TTL preset key (`"2h"`, `"permanent"`, etc.) |
| `ttl_seconds` | `int \| null` | TTL in seconds, or null for permanent |
| `source` | `string` | `"tool_search"`, `"tool_create"`, `"skill_kit"`, or `"skill_config"` |
| `skill_name` | `string \| null` | Skill Kit name when `source="skill_kit"` or `source="skill_config"` |
| `reason` | `string \| null` | Human-readable reload reason |

Existing clients can ignore `source`, `skill_name`, and `reason`; they are
metadata-only additions.

## Frontend Rendering

### Live SSE Stream

During streaming, the desktop and mobile frontends receive a `tool_reload` SSE event between the two graph invocations. The chat store:

1. Finalizes the current assistant message (sets `toolReloadInfo` with tool names, TTL, and source)
2. Creates a new streaming assistant message for the second invocation

The frontend renders a visible reload/resume message between the two message
bubbles. It shows the same resume text that the model receives, plus a compact
label such as "Tool Created", "Skill Kit Binding", or "Skill Kit Published".
This makes the graph-boundary explicit without drawing connector lines between
separate assistant messages.

### After Refresh

On refresh, the frontend calls `/threads/{id}/history` which invokes `get_conversation_history()`. The backend annotates the second assistant message with a `tool_reload_info` field (tools, TTL, source, optional skill name/reason, resume message text). The frontend maps this to `Message.toolReloadInfo` and renders the same reload/resume message.

The two bubbles appear separate because:

1. The first invocation ends with `Command(goto=END)` after the `tool_search` tool result — no final AIMessage.
2. The `tool_reload_resume` HumanMessage is filtered out (hidden), but its metadata is captured into a queue.
3. The second invocation's AIMessages start a new turn; the queued metadata is attached to it as `tool_reload_info`.

### Other Frontends

Discord and Telegram bots handle the `tool_reload` SSE event by flushing buffered text and sending a brief indicator message (embed or HTML) between the two response segments.

## Files Changed

| File | Lines changed | What |
|------|--------------|------|
| `tools/tool_search.py` | +492 | TTL support, classification buckets, `Command(goto=END)` return, reload cap logic, preserve-on-disable, status/search annotations |
| `core/agent.py` | +283 | `_pending_tool_reload`, `_turn_reload_count`, `MAX_TOOL_RELOADS_PER_TURN`, reload loop in `astream()`, `chat()`, and legacy `stream()`, `_resolve_temporary_tools()`, `tool_reload_resume` history filter case, merge temporary tools in graph builders |
| `core/stream_bridge.py` | new | Sync worker bridge that lets scheduled TODOs, triggers, callable threads, and spawned threads consume `astream()` live |
| `core/thread_config.py` | +19 | `TemporaryToolEntry` model, `temporary_tools` field on `ThreadConfig` |
| `tools/metadata.py` | +7 | Updated `tool_search` description |
| `docs/tools.md` | +46 | Updated tool_search section with TTL and auto-continue docs |
