# Lifecycle Hooks (engine spine)

Nymeria has a Claude-Code-style lifecycle-hooks engine: small pieces of logic that
run at defined moments in an agent turn and can observe or steer it. This document
describes the **engine spine** that shipped first: the machinery only, wired to
in-process fixtures. The user-facing surface (creating/toggling hooks) and the
ready-made action vocabulary land in a later pass.

Full design and rationale: `docs/private/plans/lifecycle-hooks.md`.

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
  astream drain loop's settle point: the sync `chat` path fires `DONE` observe but
  cannot continue (it has no drain loop). `DoneOutcome.user_message` is reduced but not
  yet delivered out-of-band; it is reserved for the product pass.

## What is deferred (not in the spine)

- Persisted `HookDefinition` records + storage + a `@tool`/REST/UI authoring surface.
- The ready-made **action vocabulary** (inject_text, block_if_matches, rewrite_arg,
  append_result_note, notify, webhook, run_command, ...) and presets.
- The **enable model**: a per-hook global default + per-thread `Optional[bool]` override
  (the `sequential_tool_execution` pattern), resolved by a `get_effective_hook_enabled`
  helper. Until then, in-process registrations are active by being registered.
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

`core/hooks/`: `base.py` (contract), `registry.py` (in-process registry),
`scratch.py` (per-thread store), `dispatch.py` (planes + reduction + fault policy).
