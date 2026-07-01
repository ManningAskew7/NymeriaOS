# Lifecycle Hooks

Nymeria has a Claude-Code-style lifecycle-hooks engine: small pieces of logic that
run at defined moments in an agent turn and can observe or steer it. The engine spine
shipped first (the machinery, wired to in-process fixtures); the first product surface
then landed on top of it: a single canned action, **`inject_context`**, that a user or
the agent can attach to an event so it injects a string into the model's context. This
document covers both the engine and that first action. The wider action vocabulary and
the `nym` workflow substrate land in later passes.

Full design and rationale: `docs/private/plans/lifecycle-hooks.md`.

## The `inject_context` action (product surface)

One action ships today. A hook created with it injects a string into the model's
context when its event fires, over three of the four events:

| Event | Where the text lands |
| --- | --- |
| `prompt_submit` | appended to the model-facing message tail at every turn start |
| `post_tool_use` | appended to the matching tool's result (scope with `matcher`) |
| `done` | re-driven as one follow-up prompt when the turn finishes |

`PRE_TOOL_USE` is not an injection target. The text is **static or templated**:
`{placeholder}` tokens interpolate from the event context (`{tool_name}`,
`{tool_result}`, `{tool_status}`, `{tool_args}`, `{prompt}`, `{final_text}`,
`{thread_id}`, `{user_id}`, `{event}`); unknown placeholders and brace-free text pass
through verbatim (`core/text_format.py::safe_format`, shared with triggers).

The action is store-agnostic: `core/hooks/actions.py::inject_context` maps the event to
the matching outcome family, and `core/hooks/bridge.py::build_registry` turns a user's
enabled `HookDefinition` records into a per-turn `HookRegistry`. The engine below never
learns about the store.

### Authoring

Two surfaces write the same per-user JSON store (`data_dir/hooks/<user>.json`, one file
per user, `HookManager` in `core/hook_manager.py`, capped at 50 hooks/user):

- **Agent tools** (`tools/hooks.py`, opt-in `CATALOG_TOOLS`): `hook_config`
  (create/update/delete) and `hook_info` (list/detail/test). A create auto-binds the
  current thread for `scope="thread"` (including the real `default` thread); `test`
  renders the text against sample data without firing. The `hook-management` bundled
  skill front-loads these.
- **REST** (`api/routers/hooks.py`, mounted at `/hooks`): pure CRUD plus
  `POST /hooks/{id}/test`. Every handler pins `user_id` to the authenticated caller;
  scoped creates pass through the thread-access gate. There is no webhook/fire endpoint
  (hooks fire in-process only).

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

In the spine, only `DONE` has an observe fire point. `PROMPT_SUBMIT` / `PRE_TOOL_USE`
/ `POST_TOOL_USE` dispatch on the mutate plane only; an observe registration on those
events is inert until the observe seams are added (a follow-up).

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

- The wider ready-made **action vocabulary** beyond `inject_context` (block_if_matches,
  rewrite_arg, notify, webhook, run_command, ...) and presets. `PRE_TOOL_USE` therefore
  still has no product action (the spine can veto/modify, but nothing authors it yet).
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

- `core/hooks/`: `base.py` (contract), `registry.py` (in-process registry),
  `scratch.py` (per-thread store), `dispatch.py` (planes + reduction + fault policy),
  `actions.py` (`inject_context`), `bridge.py` (definitions → per-turn registry).
- `core/hook_manager.py`: `HookDefinition`/`HookLogic`/`HookStore` records + the
  per-user `HookManager` (store-only, no engine import).
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
