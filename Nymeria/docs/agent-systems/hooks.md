# Lifecycle Hooks

Nymeria has a Claude-Code-style lifecycle-hooks engine: small pieces of logic that
run at defined moments in an agent turn and can observe or steer it. The engine spine
shipped first (the machinery, wired to in-process fixtures); the product surface then
landed on top of it as a set of **canned actions** a user or the agent can attach to an
event. This document covers both the engine and those actions. A frontend UI, the
`run_command` action, and the `nym` workflow substrate land in later passes.

Full design and rationale: `docs/private/plans/lifecycle-hooks.md`.

## Actions (product surface)

A hook's logic is a **canned action**, selected by name. `HookLogic` is a pydantic
discriminated union on `action`, so each action carries its own typed params. The
actions that ship today, and the events they attach to (`EVENT_ACTIONS` in
`core/hook_manager.py` is the legality map):

| Action | Plane | Events | Effect |
| --- | --- | --- | --- |
| `inject_context` | mutate | `prompt_submit`, `post_tool_use`, `done` | inject a string into the model's context |
| `block_if_matches` | mutate | `pre_tool_use` | **deny** a tool call when conditions match its args |
| `rewrite_arg` | mutate | `pre_tool_use` | **modify** a tool call's args when conditions match |
| `notify` | observe | `post_tool_use`, `done` | send an in-app + push notification |
| `create_todo` | observe | `post_tool_use`, `done` | add a user TODO |
| `webhook` | observe | `post_tool_use`, `done` | POST a JSON payload to a URL |

**`inject_context`** appends its text to the model-facing tail (`prompt_submit`), to the
matching tool's result (`post_tool_use`, scope with `matcher`), or re-drives once as a
follow-up (`done`). The text is **static or templated**: `{placeholder}` tokens
interpolate from the event context (`{tool_name}`, `{tool_result}`, `{tool_status}`,
`{tool_args}`, `{prompt}`, `{final_text}`, `{thread_id}`, `{user_id}`, `{event}`);
unknown placeholders and brace-free text pass through verbatim
(`core/text_format.py::safe_format`, shared with triggers).

**`block_if_matches`** / **`rewrite_arg`** are the `pre_tool_use` guardrails. Two
independent gates apply: `matcher` filters by tool NAME (exact pipe-list, e.g.
`"Edit|Write"`), and `conditions` filter by the call's ARGS. Conditions are the shared
`HookCondition` model (`core/conditions.py`, also used by triggers): a list of
`field`/`operator`/`value` filters ANDed together, with operators `equals`,
`not_equals`, `contains`, `starts_with`, `matches_regex` (field supports dotted paths
for nested args, e.g. `input.command`). `block_if_matches` returns a deny with a
templated `reason`; `rewrite_arg` returns the changed args only (`updates`, templated),
which the seam shallow-merges over the call. Empty `conditions` = always fire.

**`notify`** / **`create_todo`** / **`webhook`** are the observe-plane side effects on the
"after something happened" events (`post_tool_use`/`done`). They run fire-and-forget: the
fire point ignores their return, and each wraps its side effect so a failure is logged, not
raised. `notify` delivers an in-app + push notification (bypassing the autonomous-suppression
gate, since a user-authored hook should always deliver); `create_todo` adds a user TODO;
`webhook` POSTs `{"text", "thread_id", "user_id"}` to a `{placeholder}`-templated URL through
the **SSRF-safe** `http_policy` egress helper (private/loopback/metadata targets are refused).

Actions are store-agnostic: `core/hooks/actions.py` maps each action to its outcome/side
effect via `ACTIONS`/`ACTION_PLANES`, and `core/hooks/bridge.py::build_registry` turns a
user's enabled `HookDefinition` records into a per-turn `HookRegistry`, registering each hook
on its action's plane. The engine below never learns about the store. PRE guardrail actions
are written **never-raise** so a malformed condition is a no-op (allow), not a fail-closed
block of every tool call.

### Authoring

Two surfaces write the same per-user JSON store (`data_dir/hooks/<user>.json`, one file
per user, `HookManager` in `core/hook_manager.py`, capped at 50 hooks/user):

- **Agent tools** (`tools/hooks.py`, opt-in `CATALOG_TOOLS`): `hook_config`
  (create/update/delete) and `hook_info` (list/detail/test). The action is picked with
  `hook_action` (default `inject_context`); text actions take `text`, the guardrail
  actions take a `params` dict (`{"conditions": [...], "reason": ...}` /
  `{"conditions": [...], "updates": {...}}`). A create auto-binds the current thread for
  `scope="thread"` (including the real `default` thread); `test` renders/describes the
  hook without firing. The `hook-management` bundled skill front-loads these.
- **REST** (`api/routers/hooks.py`, mounted at `/hooks`): pure CRUD plus
  `POST /hooks/{id}/test`. The request carries `action` plus the flat per-action fields
  (`text` / `conditions` / `reason` / `updates`); the response exposes the full `logic`
  object (discriminated on `action`). Every handler pins `user_id` to the authenticated
  caller; scoped creates pass through the thread-access gate. There is no webhook/fire
  endpoint (hooks fire in-process only).

### Enable model

A hook is active on a turn only if every layer says so, resolved by
`core/agent_safety.py::get_effective_hook_enabled` (never raises), mirroring
`sequential_tool_execution`:

1. **Master kill switch** — `Settings.hooks_enabled` (env `HOOKS_ENABLED`, default on),
   overridable per-thread by `ThreadConfig.hooks_enabled` (`Optional[bool]`). Off ⇒ no
   hook fires on that thread.
2. **Per-thread per-hook override** — `ThreadConfig.hook_overrides[hook_id]` (a
   `Dict[str, bool]`) flips one hook on/off for one thread.
3. **The hook's own flag** — `HookDefinition.enabled` (the global default).

Both thread fields are set/cleared through `PATCH /threads/{id}/config`
(`hooks_enabled`, `hook_overrides`, and their `clear_*` twins); the master switch is a
`PATCH /settings` field.

## The model: when → logic → return

A hook has three parts:

- **WHEN** (the trigger): one of a fixed set of lifecycle events, plus an optional
  matcher (e.g. a tool-name filter).
- **LOGIC** (the middle): a callable that, given the event context, returns a typed
  outcome (or `None`). In the spine this is any in-process Python callable; later it
  can be a ready-made parameterized action or a sandboxed `nym` workflow.
- **RETURN** (the effect): a fixed, typed outcome vocabulary the engine validates and
  applies to the turn. This closed set is the security/architecture boundary: logic
  can do anything, but its effect on the turn is constrained and enforced.

The engine owns the two ends (the fire points and the return contract); the logic in
the middle is pluggable. Everything crosses the same typed boundary.

## The four events

| Event | Fires | Can |
| --- | --- | --- |
| `PROMPT_SUBMIT` | at every turn start (user AND autonomous) | inject context onto the model-facing message tail |
| `PRE_TOOL_USE` | before a single tool call executes | allow / deny (veto) / modify the call's args |
| `POST_TOOL_USE` | after a single tool call executes | rewrite the tool result or append a note for the model |
| `DONE` | when a turn finishes normally | observe (fire-and-forget) and/or continue (force another turn) |

`PROMPT_SUBMIT` firing on autonomous turns too (scheduled TODOs, triggers, watchdog,
dreams) is a deliberate upgrade over Claude Code's user-only event: `HookContext`
carries `is_autonomous` / `holder_kind` so a hook can scope to a turn source.

## Two planes

- **Observe plane**: fire-and-forget side effects (`dispatch_observe`). Never blocks
  or affects the turn; hook faults are logged and swallowed.
- **Mutate plane**: synchronous, in-band (`dispatch` / `adispatch`). The fire point
  awaits the reduced outcome and applies it. Fault policy: the veto path fails closed
  (a raising `PRE_TOOL_USE` hook becomes a `deny`); every other path fails open
  (logged and skipped). A hook can never crash a turn: dispatch isolates each hook's
  run *and* the application of its outcome (a malformed `scratch_patch` or
  `updated_args` is dropped, not propagated), and the final reduction is wrapped so it
  can never raise into a turn.

`DONE` and `POST_TOOL_USE` have observe fire points (the latter runs after the mutate POST
apply, seeing the original tool result; `tool_hooks_active` activates the tool-node seam for
observe-only tool hooks too). `PROMPT_SUBMIT` / `PRE_TOOL_USE` dispatch on the mutate plane
only; an observe registration on those events is inert (no product action needs it yet). The
POST observe dispatch runs in-band on the tool path (fire-and-forget but synchronous, bounded
by the per-hook timeout), so a slow observe hook adds latency to that tool call.

Multiple hooks on one event all run ("run-all-then-reduce"), so side effects and
scratch writes are order-independent. Reduction is deterministic in registration
order: prompt injections concatenate; PreToolUse first-deny-wins then modify-merges;
PostToolUse notes concatenate and the last result-rewrite wins; DONE `continue_` ORs
and reasons concatenate.

## The contract (frozen)

- `HookContext` is **primitives only** (no live agent/thread objects), so it can later
  clone across an out-of-process sandbox boundary. Cross-event scratch state is passed
  as a read-only snapshot on the context (`ctx.scratch`) and written back via
  `scratch_patch` on the outcome, never as a live handle.
- Outcome families: `PromptOutcome`, `PreToolOutcome`, `PostToolOutcome`,
  `DoneOutcome`. An outcome must match its event or the engine drops it.
- `HookProvenance` carries the DONE-continuation loop-guard state from day one.

## DONE continuation and the loop guard

A `DONE` hook may return `continue_=True` with a `reason` to force another turn (the
mechanism behind a "run checks on finish, keep going if they fail" hook). It is bounded
by a two-layer guard, exactly like Claude Code:

1. Cooperative: `HookContext.provenance.done_continuation_active` / `continuation_depth`
   let a well-behaved hook self-limit.
2. Hard cap: `NymeriaAgent.MAX_DONE_CONTINUATIONS` (8) ends the turn regardless.

A continuation is enqueued like a normal steering prompt and absorbed by the existing
drain/re-drive path (checked before the queue closes), so it reuses proven machinery
rather than a parallel re-drive.

## Fire-point seams (implementation)

- `PROMPT_SUBMIT`: dispatched at the two turn-entry sites in `core/agent.py`
  (`chat` sync, `astream` async), right after `_prefix_turn_metadata`; injected text is
  wrapped in a strippable sentinel (`agent_history.wrap_hook_context`) and stripped from
  history/RAG by `strip_prompt_context`.
- `PRE_TOOL_USE` / `POST_TOOL_USE`: `SafeToolNode._run_one` / `_arun_one`
  (`vendor/react_agent/nodes.py`) override the vendored tool node. They call
  `_execute_tool_sync` / `_execute_tool_async` directly (preserving the parent's
  `GraphBubbleUp` re-raise) and fire on both the concurrent and sequential paths. When
  no PRE/POST tool hook is registered, they delegate straight to `super()`, so the hot
  path is unchanged by default.
- `DONE` observe: dispatched at the normal-completion site of `chat`/`astream` (not the
  `finally`, where `await`/`yield` are forbidden during GeneratorExit), so it fires
  exactly once per normal turn and *not* on error/cancel (a documented limitation; the
  error/cancel observe path is a follow-up). DONE **continue** lives only in the
  astream drain loop's settle point (`_maybe_done_continuation`, async). The sync `chat`
  path fires `DONE` observe but does not yet continue: it has a queued-prompt drain loop,
  but the continuation dispatch is async-only, so wiring it needs a sync dispatch twin and
  in-loop depth tracking (deferred). Practical impact: a `done` hook re-drives on the
  streaming (SSE/desktop) path but is inert on non-streaming transports (the webhook bots,
  non-streaming `/chat`, callable threads); `prompt_submit` and `post_tool_use` hooks work
  on both paths. `DoneOutcome.user_message` is reduced but not yet delivered out-of-band;
  it is reserved for a later pass.

## What is deferred (not yet shipped)

- The `run_command` action (dispatch a slash command from a hook) and presets. The other
  six actions (`inject_context`, `block_if_matches`, `rewrite_arg`, `notify`, `create_todo`,
  `webhook`) all ship.
- A **frontend UI** for authoring/toggling hooks (the tool + REST surfaces exist; no
  desktop/mobile panel yet).
- The `nym` **workflow** logic substrate (sandboxed, out-of-process).
- Full turn-source threading into the tool-hook context (tool hooks currently read
  `is_autonomous`/`holder_kind` only if a caller threaded them into the run config).
- Observe fire points for `PROMPT_SUBMIT`/`PRE_TOOL_USE`/`POST_TOOL_USE`, DONE-continue
  on the sync path, and `user_message` delivery (see "Two planes" above).
- Sync-hook pool hardening: sync hooks share one small `ThreadPoolExecutor`. A hook
  that hangs past its timeout keeps occupying its worker (Python cannot cancel a
  running thread), so under heavy real-hook load a saturated pool could turn queued
  `PRE_TOOL_USE` dispatches into spurious denies (fail-closed). Harmless in the spine
  (no production hooks run), but the product pass should size/scope the pool and
  distinguish a queue-wait timeout from a hook-execution timeout.

## Package

- `core/hooks/`: `base.py` (contract), `registry.py` (in-process registry, `has_mutating`/
  `has_observe`), `scratch.py` (per-thread store), `dispatch.py` (planes + reduction + fault
  policy; `tool_hooks_active`), `actions.py` (the six actions + `ACTION_PLANES`), `bridge.py`
  (definitions → per-turn registry, registering each on its plane).
- `core/hook_manager.py`: `HookDefinition` + the `HookLogic` discriminated union (six
  variants) + `HookStore` records + the per-user `HookManager` (store-only, no engine
  import). Observe actions reuse `core/notifications.py` + `core/fcm.py` (notify),
  `core/todo_manager.py` (create_todo), and `core/http_policy.py` (webhook).
- `core/conditions.py`: `HookCondition` + `evaluate_conditions` (shared with triggers,
  which re-export `TriggerCondition`).
- `core/text_format.py`: `safe_format` template substitution (shared with triggers).
- Authoring: `tools/hooks.py` (`hook_config`/`hook_info`), `api/routers/hooks.py`
  (`/hooks` CRUD), `skills_bundled/hook-management/`.
- Enable model: `Settings.hooks_enabled`, `ThreadConfig.hooks_enabled` /
  `hook_overrides`, `core/agent_safety.py::get_effective_hook_enabled`.

The per-turn wiring lives in `core/agent.py::_hook_registry_for_turn` (loads a user's
enabled hooks, mtime-cached, and builds the registry) and
`core/agent_safety.py::graph_run_config` (stamps `configurable["hook_registry"]`); every
fire point falls back to the empty `default_registry` when no per-turn registry is set,
so a user with no enabled hooks runs byte-identically to the spine.
