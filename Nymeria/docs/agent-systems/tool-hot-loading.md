# Tool Hot-Loading (In-Turn Auto-Reload)

How Nymeria enables and uses optional tools within a single user turn, without requiring a round-trip to the user.

## Problem

LangGraph binds tools to the LLM at graph compilation time via `llm.bind_tools()`. Once a graph invocation starts, the tool list is frozen. When the agent discovers it needs a tool it doesn't have (e.g. `pdf_write`), calling `tool_manage(action="enable")` persists the enablement but the tool isn't callable until the **next** graph invocation  -  which normally means the next user message.

This breaks the autonomous "search, enable, use" flow:

```
Skill("tool-management")    → binds tool_search/tool_manage and friends
tool_search("pdf")          → finds pdf_view, pdf_edit, pdf_write
tool_manage(enable, [...])  → persists to thread config
pdf_write(...)              → fails: not bound to the LLM
```

## Solution: In-Stream Graph Rebuild

The fix reuses Nymeria's existing auto-compact primitive. When auto-compact fires, `astream()` finishes the current graph invocation, builds a fresh graph, injects a synthetic `HumanMessage`, and streams the second invocation into the **same SSE connection**. The client sees one continuous turn.

Tool hot-loading does the same thing:

```
User: "convert report.docx to PDF"
  │
  ├─ Graph invocation #1 (no pdf_* tools bound)
  │    ├─ tool_call: Skill(name="tool-management")
  │    ├─ tool_call: tool_search(query="pdf")
  │    ├─ tool_result: pdf_view, pdf_edit, pdf_write found
  │    ├─ tool_call: tool_manage(action="enable", tools=["pdf_write"])
  │    │    ├─ Persists to thread config (with TTL)
  │    │    ├─ Sets agent._pending_tool_reload[thread_id]
  │    │    ├─ Invalidates cached graph
  │    │    └─ Returns Command(goto=END) with a marked ToolMessage and "STOP NOW" guidance
  │    └─ Post-tools router sees the reload marker and ends the graph
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

The graph does not rely on the model obeying the STOP wording. Reload-queued
tool results also carry a private `nymeria_tool_reload_queued` marker in
`ToolMessage.additional_kwargs`. After the tools node finishes, the ReAct graph
routes to `END` when the latest tool-result batch contains that marker;
otherwise it routes back to the agent normally. This guards the reload boundary
even when LangGraph also sees a regular post-tools edge.

## Deferred Execution: `tool_invoke` (cache-safe alternative to binding)

Binding a tool (`tool_manage(action="enable")` or a Skill Kit) mutates the
thread's tool list, which lives in the cached tools/system prefix, so the next
request re-pays the whole input as a cache miss. That is the right trade for a
tool used repeatedly over a long conversation and the wrong trade for a tool
needed once.

`tool_invoke(name, arguments)` is a single resident seed tool that runs any
discoverable catalog / MCP / custom / workflow tool by name WITHOUT writing
thread config, so the cached prefix never changes (Nymeria's provider-agnostic
emulation of Anthropic's deferred tool loading). The target's schema rides in
conversation history, delivered by `tool_search(include_schemas=true)`, a hook,
the user, or echoed back by `tool_invoke` on a validation error, which is
cache-safe.

The model-facing description opens with a hard precondition: call `tool_invoke`
only with the target's full schema already in context, and only for a target
that is not already bound first-class (a bound tool is called directly). The
validation-error schema echo is a recovery path, not a discovery mechanism.
Without that front-loaded rule, models pattern-match `tool_invoke` to the
generic dispatch idiom they are trained on and guess arguments by tool name
(observed in dogfooding, 2026-07-31).

- Same gates as binding: the management denylist
  (`PROTECTED_MANAGEMENT_TOOL_NAMES`), admin/developer role gates, and the
  thread's authoritative `disabled_tools`, resolving credentials as the calling
  user. The deferred path is never a gate bypass. Gate
  (`core/tool_execution.py::by_name_gate_reason`), resolver
  (`resolve_by_name`, the dispatch superset) and execution envelope
  (`run_tool_envelope` / `arun_tool_envelope`) are all shared with every other
  by-name spelling: the `nym.tools.*` workflow dispatcher, the workflow memory
  and thread-spawn verbs, and `self_invoke_tool`.
- Lifecycle hooks fire here as they do for a bound call, on the TARGET's name:
  `tool_invoke` is transport, so the graph tool node suppresses its own fire and
  the envelope fires on the tool actually dispatched. A `require_approval` or
  `block_if_matches` hook authored against `bash_execute` therefore also covers
  `tool_invoke(name="bash_execute")`, which it did not before.
- The gate carries a positive allowlist arm alongside the `disabled_tools`
  denylist (`core/tool_execution.py::tool_allowlist`). It is a seam: it returns
  `None` today, so no thread is narrowed. It governs the BY-NAME half only, not
  bound calls, which are decided at graph build; read its docstring before
  treating it as a per-tool permission model.
- Arguments are validated against the target's real schema server-side but,
  unlike a bound tool, are NOT grammar-constrained as the model types them.
  `tool_invoke` pre-checks the args against the target's call schema and, on a
  mismatch, returns that compact schema so the model self-corrects in one retry.
  A validation error raised inside the tool's own body (not by the args) surfaces
  as a plain failure, not as a misleading "fix your arguments" echo.
- Excluded from the deferred path: `Skill` (its own resident tool; returns a
  `Command`), `run_tools_in_order` (an inert ordering marker), `tool_invoke`
  itself (no self-nesting), and `install_skill` / `install_mcp_server` (their job
  is to bind and reload the tool set, so they belong on the binding surface).
- `Skill(name=..., defer=true)` is the kit-level expression: it loads the kit's
  instructions plus its tools' argument schemas and binds none of the kit's
  tools, for use via `tool_invoke`. `ttl` (bind) and `defer` are mutually
  exclusive. One exception to "binds nothing" (backlog #170): when the thread
  cannot call `tool_invoke` itself (e.g. an account whose curated
  `default_thread_tools` predates the tool, #164), the activation binds just
  `tool_invoke` on a self-cleaning 7-day TTL and announces it in the result.
  Under dynamic binding it is callable on the next model step; on the legacy
  rebuild path that one bind triggers the same stop-and-rebuild round trip as
  any enable, the one case where a deferred activation is not
  cache-preserving.
  A thread that explicitly disabled `tool_invoke` is never silently
  un-disabled: the result says so and steers to `ttl` binding, which needs no
  `tool_invoke`. Reachability is resolved from thread config
  (`tool_search.thread_tool_reachability`), never from the built tool list,
  which the permissive mode below deliberately strips `tool_invoke` from; in
  that mode the deferred result instructs calling the tools directly by name
  and no auto-bind fires.

Those compact schemas are all rendered in one place, `tools/schema_render.py`.
It reads the tool's `tool_call_schema` (so runtime-injected arguments such as
the config or the user id are excluded, leaving only what the model actually has
to supply) and serves four consumers: `tool_search(include_schemas=true)`, the
`tool_invoke` validation-error echo, `Skill(defer=true)`, and
`workflow_info(action="show")`. The `react` tool's guidance block renders a
target schema the same way. A new surface that has to show the model how to call
a tool it has not bound should render through that module rather than dump the
raw JSON schema.

Frontends attribute a `tool_invoke` call to the TARGET tool (the tool-call card
title is the target name with a small "deferred" marker), so a deferred call
reads like a real call to that tool rather than an opaque meta-call.

### Unbound-call enforcement and `allow_unbound_tool_calls`

In dynamic-binding mode the tool executor's dispatch table is the SUPERSET
(`compute_tool_superset`), which includes every catalog tool and applies no role
or `disabled_tools` gates. Without enforcement, a model that emits a call for a
tool NOT in its bound list would execute it ungated. `SafeToolNode` closes this:
it stashes the thread's EFFECTIVE (gated, visible) tool-name set per batch from
the resolver and, for a call whose tool exists but is not in that set:

- Strict (default): refuses with an error composed from what the thread can
  actually do (`nodes.unbound_call_refusal`): if the tool's TTL lapsed on this
  thread, the lead sentence names the Skill Kit (or `tool_manage`) that bound
  it, the window, and when it expired; else, if an installed kit's
  `required_tools` names it, the kit; and the remedies offered are only the
  reachable ones (`Skill(name=<kit>)` when the meta-tool is bound, otherwise
  the user's `/kit <kit>`; `tool_manage` when bound; `tool_invoke` when bound
  AND the tool is not a protected management tool, which `tool_invoke` refuses).
  A truly unknown name (absent from the superset too) still gets the parent's
  canonical "not a valid tool" error.
- Permissive (`allow_unbound_tool_calls=true`, dynamic binding only): applies the
  SAME deferred gates as `tool_invoke` and dispatches on pass, refuses on gate
  failure. This makes the direct unbound call a policy-equivalent twin of
  `tool_invoke`, and graph build drops `tool_invoke` from the bound schema to save
  its tokens (direct calls make it redundant). Only enable it on providers that
  reliably emit calls for tools not present in the schema.

Enforcement is skipped entirely in legacy rebuild mode (no resolver: the dispatch
table is already the exact bound set) and fails OPEN on a resolver error (a
transient failure never wrongly rejects a real call).

## Tool-Call Argument Boundary

Some model/provider paths can emit a list-typed tool-call argument as a
JSON-encoded string (for example the `tools` argument arriving as
`"[\"random_cat_fact\"]"` instead of a JSON array). Pydantic rejects that value
before the tool executes because the tool schema expects
`tools: Optional[List[str]]`. This is a boundary issue with list-typed tool-call
arguments at the LLM/tool-adapter layer, not with dynamic tool binding itself.

`SafeToolNode` normalizes this boundary before dispatch:

- It inspects the target tool's JSON schema.
- It only attempts JSON decoding for fields whose schema allows `array` or
  `object`.
- It only replaces the value when the decoded value has the expected JSON type.
- It leaves scalar string fields unchanged, including strings that merely look
  JSON-like but are not schema-declared arrays or objects.

This is intentionally a narrow guard. Pydantic still performs final validation,
and dependency pins in `requirements*.txt` and `pyproject.toml` prevent silent
LangChain/LangGraph/provider adapter drift from changing this behavior again
without an explicit update.

## Skill Kit Binding

Skill Kits use the same binding path as `tool_manage`. When `Skill(name=...)`
activates a skill whose `metadata.nymeria.required_tools` list contains tools
that are not currently bound, the Skill tool:

1. Validates every required tool strictly (unknown, unloadable, or admin-only
   dependencies fail before any config write).
2. Writes the required tools to `temporary_tools` or `enabled_tools` using the
   skill's `metadata.nymeria.tool_ttl` (`2h` by default; accepts `Nm`, `Nh`,
   `Nd`, `Nw`, or `never`/`permanent`).
3. Removes required tools from `disabled_tools` when needed, matching
   `tool_manage(action="enable")`.
4. In dynamic-binding mode, returns the skill body plus a binding-result block.
   The model node resolves the updated tool set on its next step, and
   `SafeToolNode` resolves any post-build tool from the live resolver before
   dispatch.
5. In legacy rebuild mode (`DYNAMIC_TOOL_BINDING=false`), queues
   `_pending_tool_reload[thread_id]` with `source="skill_kit"`, `skill_name`,
   and `reason`, then returns STOP guidance in a marked `Command(goto=END)`
   tool result so the resumed graph has both the instructions and the newly
   bound tool schemas.

`allowed-tools` remains advisory Agent Skills metadata; it does not bind
Nymeria tools. Activation warnings are only emitted when an `allowed-tools`
entry is also a known Nymeria tool name that is missing from the current
thread; portable names such as `Read`, `Write`, and `Bash(...)` stay quiet.

## Tool Create Reloads

`tool_create(action="publish")` writes a validated custom tool definition,
reloads the custom-tool registry, and enables the new tool on the publishing
thread. HTTP tools are declarative definitions; Python tools are validated and
then executed through a subprocess wrapper.

That subprocess is confined on two axes. Its environment is scrubbed to an
allowlist, so backend secrets are not inherited, and on Linux it runs inside
the Landlock filesystem sandbox (`EXEC_SANDBOX_ENABLED`). Two consequences are
worth knowing when writing tool code. `/proc` file content is denied, so
`ps`, `top` and `psutil` FAIL (`ps` exits non-zero advising you to mount
`/proc`, which is not the problem and will not help), while `pgrep` and
`pkill` are the case to watch: they do not error, they return empty, so "is X
running" answers no. And anything needing privilege (`sudo` and friends)
fails, because Landlock requires the kernel's `no_new_privs` flag. Third-party imports, network calls,
temp files, writes to the working directory and nested subprocesses all behave
normally.

The declarative models behind those definitions, and behind managed MCP
servers, live in `nymeria/tools/definitions/`:

| File | Models |
| --- | --- |
| `custom_tool_schema.py` | `CustomToolDefinition`, `HTTPToolConfig`, `ToolParameter` |
| `mcp_schema.py` | `MCPServerDefinition`, `MCPToolConfig`, `MCPTransport`, `MCPDiscoveredTool`, `MCPInstallStatus` |
| `schema.py` | Compatibility shim: re-exports both sets for older persisted imports and external callers. New code should import from the two modules above. |

The package `__init__.py` re-exports the same names, so
`from nymeria.tools.definitions import CustomToolDefinition` works. These models
are the shapes an on-disk definition under `data/custom_tools/` or
`data/mcp_servers/` is validated against, so they are the reference for what a
hand-written or agent-written definition file may contain.

Python tools additionally carry an **execution-time approval gate** (backlog
#75 Gap 1). Authoring a Python tool is admin-only, but the generic file tools
can write `data/custom_tools/<id>.json` directly, so the admin gate is enforced
again at run time: the admin publish/create/update/import paths stamp a content
hash over `{source_code, entrypoint, parameters}` into `approved_revision`, and
the loader-bound tool recomputes that hash from live source on every call,
refusing with `approval_required` unless it matches (mirrors the workflow gate).
An unapproved planted definition, or an edit to an approved tool's source, fails
closed on the next call with no reload. **Migration:** any Python custom tool
that predates this gate (already on disk, or imported from a pre-gate export)
must be re-published by an admin once, because a stamp cannot be safely
auto-applied on load (that would re-open the bypass). The gate is
tamper-evidence and fail-closed-by-default, not an unforgeable boundary: a
writer who reads the (open) hash code could forge a matching approval, so the
gate is not the hard multi-user boundary. Two of that boundary's three legs
have since shipped: the subprocess env scrub, and the Landlock sandbox above.
Write confinement (stopping the forging write in the first place) has not.

**`http` and `mcp` tools carry the same execution-time gate**
(`core/custom_tool_gate.py`), so all four implementation types are now covered.
It exists mostly for `mcp`: that config carries `server_command`, `server_args`
and `working_directory` directly, so an invocation on a planted record spawns a
process, and the same launch surface under `data/mcp_servers/` had been gated
since #75 while this one was not.

The hash covers where the call goes, what rides along, and what the caller may
leave unset:

- **`parameters`, for both types.** Not padding: an optional parameter's
  `default` is installed into the generated args schema, LangChain supplies it
  whenever the model omits the argument, and `interpolate_params` substitutes it
  into an HTTP tool's URL, headers, query params and body. So an edit to a
  default redirects an approved, credential-bearing tool, and flipping
  `required` to false lets a planted default apply where the model used to be
  asked. Both sibling gates hash parameters for the same reason.
- **`http`:** method, URL, headers, query params, body template.
- **`mcp`:** transport, command, args, URL, headers, working directory, tool
  name, `server_id` (the credential-vault target key, so an edit re-points which
  vault rows the tool satisfies) and `env_vars`/`encrypted_env_vars`.

Deliberately outside the hash, so the omissions are choices rather than
oversights: the http `timeout_seconds`/`response_path`/`response_format` and the
mcp `idle_timeout_seconds`/`startup_timeout_seconds`/`call_timeout_seconds`.
These shape how long a call waits and how much of an answer comes back, not
where it goes or what rides along.

Note that `env_vars` **is** covered here while the MCP SERVER gate excludes it.
That difference is load-bearing rather than an inconsistency: the startup
`migrate_mcp_encrypted_env_vars` pass rewrites `data/mcp_servers/` after save, so
covering the field there would invalidate stamps behind the author's back.
Nothing rewrites `data/custom_tools/`, so the field is stable and the launch
hijack it enables (`LD_PRELOAD`, `NODE_OPTIONS`, `BASH_ENV` injected without
touching the command) is closed on this side.

**Where the stamp is applied.** At the AUTHORING layer with the acting user, the
same as the other three gates: `build_custom_tool_definition` and
`apply_custom_tool_update` for the REST routes, the import route, and
`tool_create` publish for the agent's own `http` authoring. Never in
`CustomToolLoader.save_definition`. Stamping at the storage chokepoint looks
safer because it cannot be forgotten, and it was the first shape of this gate,
but a name-only `PUT /tools/custom/{id}` reads the record off disk, changes a
label and re-persists, so a stamp at that depth would approve a launch command
nobody authored. An update re-stamps only when the request actually carried a
config or `parameters`; a name, description, `enabled` or `tags` edit never
touches an approval, for any of the four types.

`POST /tools/custom/{tool_id}/test` is gated too, and returns `409` with
`approval_required` for an unapproved record of any type. That route reaches the
executors directly off the stored definition rather than through the
loader-bound tool, so without the check a planted record was one admin "Test"
click from running, and it appears in the admin tools list, which is what
invites the click.

Authoring is unchanged in reach. `tool_create` accepts `http` and the author
self-approves, so an agent that could publish an HTTP tool before still can.
`mcp` custom tools have always been admin-only to author (`tool_create` rejects
the type and every REST route that creates one requires an admin), so for that
type the stamp records a real admin decision, exactly as the Python gate's does.

**Pre-existing tools are grandfathered once**, on first start after upgrade,
rather than needing a manual re-publish. A marker file makes that a one-shot: a
definition appearing on disk after the backfill stays unapproved, where an
every-startup stamp would quietly approve planted files forever. If the marker
cannot be written the pass aborts rather than proceeding, because proceeding
without one re-arms the grandfathering on every subsequent start. The Python
gate cannot grandfather at all, because its stamp asserts an admin decision that
cannot be invented on load.

With dynamic binding enabled, publishing does not use the old graph
rebuild/resume loop. The publish result includes the thread-binding result, the
next model step sees the updated schema list from the live resolver, and
`SafeToolNode` can register the newly published tool from that resolver before
dispatch if the graph was built before the tool existed.

When legacy rebuild mode is enabled, publish reload metadata carries
`source="tool_create"` and `reason="tool_published"` or
`reason="python_tool_published"` so history, resume text, and frontend
indicators can distinguish agent-authored tools from a normal
`tool_manage(action="enable")` request.

`manage_mcp(action="install")` uses the same path after successful MCP tool
discovery. It enables discovered `mcp__...` tools on the current thread. In
dynamic-binding mode those tools become callable on the next model step without
a graph rebuild; in legacy rebuild mode the metadata uses
`source="mcp_install"`.

## Skill Publish Reloads

`skill_write` and `skill_edit` use the same
reload loop after it writes a validated `SKILL.md`, reloads `SkillManager`,
and updates `ThreadConfig.enabled_skills`. In this case the reload refreshes
the generated `Skill` meta-tool index rather than binding a normal tool, so
the emitted `tool_reload` event may have `tools: []` with
`source="skill_write"` or `source="skill_edit"`, `skill_name`, and a matching
reason.

`skill_manage(action="install"|"enable", activate_current_thread=true)` queues
`source="skill_install"`.

## TTL (Time-to-Live) Enablements

Not every tool enable should be permanent. A one-shot PDF conversion doesn't need `pdf_write` bound forever. TTL lets the agent pick the right lifetime.

### Duration Format

Tool TTL accepts `Nm`, `Nh`, `Nd`, `Nw`, or `never`/`permanent`.
Examples: `30m`, `2h`, `7d`, `4w`, `never`. Values must be greater than zero
and no longer than about one year (`365d` or `52w`).

### Storage Split

`ThreadConfig` has two fields for enabled tools:

- **`enabled_tools: List[str]`**  -  Permanent enablements. Written by the UI, API (`PATCH /threads/{id}/config`), `spawn_thread`, and `tool_manage(ttl="never")` or `tool_manage(ttl="permanent")`. Unchanged schema means zero back-compat risk for existing callers.

- **`temporary_tools: Dict[str, TemporaryToolEntry]`**  -  TTL'd enablements, agent-managed. Each entry has `enabled_at` and `expires_at` timestamps plus the provenance of the binding that set the current expiry: `source` (`skill_kit`, `tool_manage`, `slash_command`, ...) and `kit` (the Skill Kit name when a kit bound it). A direct `tool_manage` refresh overwrites both, so the kit no longer claims the window.

- **`expired_tools: Dict[str, ExpiredToolEntry]`**  -  TTL entries that lapsed and have not been bound again since (`expired_at`, `enabled_at`, `source`, `kit`, `notified`). Written by the eviction, cleared for a name by any bind of it, aged out after 7 days or past 50 records. Read by the expiry notice and the unbound-call refusal.

The enabled fields are merged wherever the live set is resolved: `extra_names = (set(tc.enabled_tools) | live_temp) - disabled`.

### Lazy Eviction

No background scheduler. `resolve_temporary_tools()` (`core/agent_tools.py`) filters out expired entries wherever the live set is resolved (graph build, the dynamic-binding resolver, the prompt hash, the bind path) and persists the cleaned config plus one `expired_tools` record per evicted entry. In dynamic-binding mode the resolver runs on EVERY model step and tool batch, so a TTL lapsing mid-turn IS evicted mid-turn: the tool leaves the bound list at the next step. That is not silent (next section).

### Expiry Notices

A lapse reaches the model three ways, all fed by `expired_tools`:

- **Next prompt.** The turn-input build (`agent_streaming_input._apply_tool_expiry_notice`) folds a one-line `[System: Skill Kit <kit>'s tools expired at <time> (<ttl> TTL) and are no longer bound: <names>. Re-activate it with Skill(name="<kit>") (or /kit <kit>) for a fresh window. ...]` directly under the `[Time:]/[Trigger:]` block (kits first, then direct binds). It persists in the checkpoint like that block and is stripped from history views by the same reader (`agent_history.SYSTEM_NOTICE_PATTERN`), the raw message carrying it on `additional_kwargs["tool_expiry_notice"]`.
- **Mid-turn.** An eviction inside a running turn flags the thread (`agent._tool_expiry_signal`); `route_after_tools` turns the flag into a `source="system"` pending prompt at the next sub-turn boundary, so the same text arrives through the queued-prompt absorb path (halt, drain, one internal `[Trigger: System Notice]` HumanMessage, re-drive). The `notified` flag on each record is the single once-only truth across both deliveries, and it flips at ABSORB (`build_queued_prompt_messages`), not at enqueue: a queued notice dropped before delivery (user stop, abort, inject failure, closing queue) is simply carried by the next prompt instead.
- **A call to the expired tool** gets the kit-aware refusal described under Unbound-call enforcement.

Humans see what the model saw: `/history` strips the line from the user bubble and re-emits it as a `system` entry with `kind: "tool_expiry_notice"` (placed before the prompt it rode on; the mid-turn form is that queued message itself, whitelisted in the internal-message filter so the sub-turn it introduced stays visible regardless of `show_autonomous_prompts`). Live clients get the mid-turn form from the `prompt_injected` event (`sources[i] == "system"`) and render the same card.

`/tools list` and `tool_manage(action="status")` both print the remaining window of every live TTL'd tool.

### Sliding Renewal

Calling `tool_manage(action="enable")` on a tool already in `temporary_tools` refreshes its `expires_at` (and restamps the provenance to the caller). Calling with `ttl="never"` or `ttl="permanent"` promotes it from `temporary_tools` into `enabled_tools`. Use does NOT refresh a TTL: a kit driven for three hours on a 2h window lapses mid-task and is announced, by design (developer call, 2026-09-03; refresh-on-use is an open option on backlog #232).

### Disable Preserves State

When a tool is disabled, it's added to `disabled_tools` but **not** removed from `enabled_tools` or `temporary_tools`. This preserves the original classification so un-disabling restores it as-is (permanent stays permanent, TTL keeps its original expiry).

## Code Reference

Line numbers below are intentionally omitted because `core/agent.py` and
`tools/tool_search.py` move with refactors. Search for the function/symbol
names in those modules; the named entry points are stable.

### Entry Points: `tool_search()` and `tool_manage()`  -  `tools/tool_search.py`

`tool_search()` is search-only. `tool_manage()` dispatches on binding actions:

| Action | Handler | Returns |
|--------|---------|---------|
| `tool_search` | `_search()` | String with up to `top_k` results, optionally annotated with `[ENABLED Xh Ym left]` or `[DISABLED]` |
| `tool_manage(action="enable")` | `_enable()` | `Command(goto=END)` if reload needed, plain string otherwise |
| `tool_manage(action="disable")` | `_disable()` | String summary |
| `tool_manage(action="prune")` | `_prune_tools()` | JSON summary of conservative cleanup actions |
| `tool_manage(action="status")` | `_status()` | Thread's full tool status (permanent, TTL, disabled sections) |
| `tool_manage(action="list_categories")` | `_list_categories()` | All categories with tool counts |

### Enable Classification: `_enable()`  -  `tools/tool_search.py`

Each requested tool is classified into exactly one bucket (checked in this priority order):

| Bucket | Condition | What happens |
|--------|-----------|-------------|
| **invalid** | Not in catalog | Reported in `[Not found]` |
| **unloadable** | In catalog but can't be resolved (e.g. disabled MCP server) | Reported in `[Unloadable]` |
| **un_disabled** | In `tc.disabled_tools` | Removed from disabled list. Preserved state restored if it exists; otherwise fresh entry written with requested TTL |
| **already_permanent** | In `tc.enabled_tools` and not disabled | No-op |
| **already_default** | Part of thread's default-bound tool set | No-op (already callable) |
| **refreshed / promoted** | In `tc.temporary_tools` | `ttl="never"` or `ttl="permanent"` promotes to `enabled_tools`; otherwise refreshes `expires_at` |
| **newly_added** | None of the above | Written to `enabled_tools` (permanent) or `temporary_tools` (TTL'd) |

After classification, if `newly_added` or `un_disabled` is non-empty **and** the reload cap hasn't been hit:

1. Sets `agent._pending_tool_reload[thread_id]` with the new tool names and TTL info.
2. Returns `Command(goto=END, update={"messages": [ToolMessage(...)]})` with explicit "STOP NOW" wording and the private reload marker. The post-tools router uses that marker to end the graph cleanly after the tool result, handing control back to `astream()`.

If the reload cap is already hit, returns a plain string instead. The enablement is still persisted, but the tool won't be bound until the next user message.

### Reload Loop: `astream()`  -  `core/agent.py`

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

1. **Yields a `tool_reload` SSE event**  -  frontends use this to render the reload/resume message between the pre-reload and post-reload response segments.

2. **Invalidates the graph cache** and builds a fresh graph via `_get_async_graph_for_user()`. The new graph has the just-enabled tools bound to the LLM.

3. **Constructs a resume prompt**:
   ```
   [System: tools pdf_write are now loaded for the next 2h.
   Continue the user's task using the new tools.]
   ```

4. **Injects it as an internal HumanMessage** with `internal_type="tool_reload_resume"`. This is what the checkpointer sees as the "user message" that triggers the second invocation.

5. **Drives the fresh graph** via the same `_drive_graph_events()` inner function, streaming events into the same SSE connection.

6. **Reassigns `graph = reload_graph`** so post-loop logic (token tracking, dangling-tool-call patching) operates on the most recent graph.

The sync `chat()` method has an identical loop using `graph.invoke()` instead of `astream_events()`.

### Graph Build: `_build_async_graph_with_prompt()`  -  `core/agent.py`

The graph builder calls `_resolve_temporary_tools(tc)` to get the set of live TTL'd tool names, then merges them with permanent enablements:

```python
live_temp = self._resolve_temporary_tools(tc)
extra_names = (set(tc.enabled_tools) | live_temp) - disabled
```

Tools are looked up in `SEED_TOOLS`, then `CATALOG_TOOLS`, then the tool registry (for MCP/custom tools).

### TTL Eviction: `resolve_temporary_tools()`  -  `core/agent_tools.py`

```python
def resolve_temporary_tools(agent, tc, *, persist=True) -> set:
    now = utc_now()
    live = {name: entry for name, entry in tc.temporary_tools.items()
            if ensure_aware_utc(entry.expires_at) > now}
    if len(live) != len(tc.temporary_tools):
        evicted = {name: entry for name, entry in tc.temporary_tools.items()
                   if name not in live}
        # Log, then (persist=True only): record each lapse on
        # tc.expired_tools, save, and flag the thread for the mid-turn notice.
        if persist:
            tc.temporary_tools = live
            record_tool_expiries(tc, evicted, now=now)
            agent.thread_config_manager.save_config(tc)
            agent._tool_expiry_signal.add(tc.thread_id)
    return set(live.keys())
```

`persist=False` is the read-only variant (the thread overview): same live set, no write, no record, no flag. The agent facade `agent._resolve_temporary_tools(tc)` delegates here.

### History Filter: `get_conversation_history()`  -  `core/agent.py`

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

This matches the treatment of `autonomous_wakeup`. The AI messages from the second invocation are then picked up by the turn consolidation logic and rendered as a continuation of the assistant's turn  -  or as a separate message bubble if there was no active turn (which happens when `Command(goto=END)` ended the first invocation without a final AIMessage).

### ThreadConfig Model: `core/thread_config.py`

```python
class TemporaryToolEntry(BaseModel):
    """A tool enabled for this thread with a time-to-live.
    Eviction is lazy: expired entries are filtered out wherever the live set
    is resolved (graph build, the dynamic resolver on every model step)."""
    enabled_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    source: Optional[str] = None   # "skill_kit" | "tool_manage" | ...
    kit: Optional[str] = None      # the Skill Kit name when a kit bound it


class ExpiredToolEntry(BaseModel):
    """What a TTL entry was when it lapsed, kept until the tool is bound again."""
    expired_at: datetime
    enabled_at: Optional[datetime] = None
    source: Optional[str] = None
    kit: Optional[str] = None
    notified: bool = False         # flipped once the model has been told
```

The fields on `ThreadConfig`:
```python
temporary_tools: Dict[str, TemporaryToolEntry] = Field(default_factory=dict)
expired_tools: Dict[str, ExpiredToolEntry] = Field(default_factory=dict)
```

### Metadata: `tools/metadata.py`

Built-in metadata is generated from the registered LangChain tool objects.
`tool_search` discovery text therefore follows the tool's own docstring instead
of a separate hand-maintained registry row. `metadata.py` still owns category,
security-level, default-enabled, and config-schema policy.

## Safeguards

### Reload Cap

`MAX_TOOL_RELOADS_PER_TURN = 3`. After this many reloads in one user message, `_enable()` still persists the enablement but returns a plain string instead of `Command(goto=END)`, deferring the bind to the next turn. This prevents pathological `enable -> enable -> enable -> ...` loops.

The cap is tracked via `_turn_reload_count[thread_id]`, reset at the start of each turn, incremented by the reload loop, and read by `_enable()`.

### Abort Signal

The reload loop checks `abort_event.is_set()` before each iteration. If the user hits Stop (`POST /threads/{id}/stop`), no further reloads are attempted.

### Cleanup

`_pending_tool_reload` is cleared in three places to prevent stale entries:

1. **Inside the loop**  -  `pop()` consumes the entry
2. **After the loop**  -  drains any residual entry
3. **In `finally`**  -  catches exceptions and early exits

`_turn_reload_count` is cleaned up in `finally`. Thread deletion also clears
the per-thread active superset snapshot used by dynamic binding.

At the start of every new `chat()` and `astream()` turn, Nymeria
also discards any pre-existing pending reload for that thread before resetting
the per-turn counter. A pending reload is only valid inside the top-level turn
that created it; carrying it into the next user message would attach a stale
Tool Binding event to unrelated output.

Sync workers such as ticker, triggers, callable thread execution, spawned
threads, and CLI use `core/stream_bridge.py` to consume `astream()` live, so
they share the same in-turn reload loop as regular chat streaming. The
autonomous workers also share `stream_and_collect()` for response collection
and iteration-limit bookkeeping; caller-specific event payloads remain in the
ticker, trigger, callable-thread, and spawned-thread modules.

The bridge owns one process-local asyncio loop for sync callers. Async graph
caches include the owning loop id, and provider HTTP pools in
`vendor/react_agent/providers.py` are loop-local for Anthropic plus
OpenAI-compatible providers. Do not replace this with per-call
`asyncio.run()`/fresh-loop execution; that can move cached provider clients
across closed or foreign loops during callable orchestration.

Both `_pending_tool_reload` and `_turn_reload_count` are intentionally process-local and ephemeral. A process restart loses any in-flight reload, but the underlying tool enablement is already persisted in the thread config before the reload flag is set. The next turn's graph build picks up the enabled tools normally.

### Dangling Tool Calls

The `finally` block patches dangling tool calls for **both** invocations, since `graph` was reassigned to `reload_graph`.

### Idempotence

If `tool_manage(action="enable")` is called with tools that are already enabled, no reload flag is set (they fall into `already_permanent`, `already_default`, or `refreshed` buckets). No auto-continue triggers, the turn proceeds normally.

## SSE Event Protocol

The new `tool_reload` event sits between the two graph invocations:

```
tool_call(tool_manage enable) → tool_result → tool_reload → [second invocation events] → done
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | `"tool_reload"` | Event type |
| `tools` | `string[]` | Names of newly-loaded tools |
| `ttl` | `string` | TTL key (`"2h"`, `"7d"`, `"4w"`, `"never"`, etc.) |
| `ttl_seconds` | `int \| null` | TTL in seconds, or null for permanent |
| `source` | `string` | `"tool_manage"`, `"tool_create"`, `"skill_kit"`, `"skill_write"`, `"skill_edit"`, `"mcp_install"`, or `"skill_install"` |
| `skill_name` | `string \| null` | Skill Kit/name context for skill-driven reloads |
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

1. The first invocation ends after the marked reload tool result  -  no final AIMessage.
2. The `tool_reload_resume` HumanMessage is filtered out (hidden), but its metadata is captured into a queue.
3. The second invocation's AIMessages start a new turn; the queued metadata is attached to it as `tool_reload_info`.

### Other Frontends

Discord and Telegram bots handle the `tool_reload` SSE event by flushing buffered text and sending a brief indicator message (embed or HTML) between the two response segments.
