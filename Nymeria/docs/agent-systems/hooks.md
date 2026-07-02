# Lifecycle Hooks

Nymeria has a Claude-Code-style lifecycle-hooks engine: small pieces of logic that
run at defined moments in an agent turn and can observe or steer it. The engine spine
shipped first (the machinery, wired to in-process fixtures); the product surface then
landed on top of it as a set of **canned actions** a user or the agent can attach to an
event. This document covers both the engine and those actions. The desktop and mobile
clients ship a GUI over the REST surface (a dashboard **Hooks** panel to author and toggle
hooks, plus a per-thread **Hooks** tab for enablement). The `run_command` action (a hook
that shells out, admin + deployment-flag gated) ships as the pathfinder for the eventual
`nym` workflow substrate, which lands in a later pass.

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
| `run_command` | mutate on `prompt_submit`/`pre_tool_use`, observe on `post_tool_use`/`done` | all four | run a shell command with the hook context as JSON on stdin (admin + flag gated) |

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

**`run_command`** runs a shell command and is the **one action whose plane flips per event**:
mutate on `prompt_submit`/`pre_tool_use` (its output can steer the turn), observe on
`post_tool_use`/`done` (fire-and-forget). It is the pathfinder for the `nym` subprocess
substrate, so it is deliberately hardened and **double-gated** (admin account AND the
`HOOKS_RUN_COMMAND_ENABLED` deployment flag, enforced both at authoring on every surface and
again at execution). The command receives the full hook context as **JSON on stdin** (never
argv) and runs with a **minimal environment** (`PATH`/`HOME`/`LANG`/`LC_ALL`/`TMPDIR` plus
`NYMERIA_HOOK_EVENT`/`_THREAD_ID`/`_USER_ID`/`_TOOL_NAME`; no inherited process secrets), in
its own process group (`start_new_session`), under the author-configured `timeout_seconds`
(1..300; the mutate/in-band events additionally cap it at 60s so a hook cannot stall a turn).
On timeout the whole process group is `SIGKILL`ed. The contract by event: on `prompt_submit`,
exit 0 stdout is injected as context (**fail-open**: a spawn error or non-zero exit injects
nothing); on `pre_tool_use`, exit 2 is a **deny** (stderr is the reason), exit 0 with empty
stdout is allow, exit 0 with a JSON decision object is that decision, any other failure or a
timeout is a **fail-closed deny**; on `post_tool_use`/`done` it is observe (output ignored).
Output is read-capped (50 KB captured, 10 KB injected).

Actions are store-agnostic: `core/hooks/actions.py` maps each action to its outcome/side
effect via `ACTIONS`/`ACTION_PLANES`, and `core/hooks/bridge.py::build_registry` turns a
user's enabled `HookDefinition` records into a per-turn `HookRegistry`, registering each hook
on its action's plane. The engine below never learns about the store. PRE guardrail actions
are written **never-raise** so a malformed condition is a no-op (allow), not a fail-closed
block of every tool call.

### Authoring

Three surfaces write the same per-user JSON store (`data_dir/hooks/<user>.json`, one file
per user, `HookManager` in `core/hook_manager.py`, capped at 50 hooks/user):

- **Agent tools** (`tools/hooks.py`, opt-in `CATALOG_TOOLS`): `hook_config`
  (create/update/delete) and `hook_info` (list/detail/test/log). The action is picked with
  `hook_action` (default `inject_context`); text actions take `text`, the guardrail
  actions take a `params` dict (`{"conditions": [...], "reason": ...}` /
  `{"conditions": [...], "updates": {...}}`). A create auto-binds the current thread for
  `scope="thread"` (including the real `default` thread); `test` renders/describes the
  hook without firing; `log` reads the execution log (below). The `hook-management`
  bundled skill front-loads these. Like every surface, `scope` is create-only
  (a re-scope is a delete + create: an update cannot supply the access-gated
  thread binding).
- **Slash command** `/hook` (`core/command_service.py`, catalog in `core/registry_defaults.py`):
  `list` / `show` / `create` / `edit` / `enable` / `disable` / `delete` / `test` / `log`, reaching
  the same store through `POST /commands/execute` (so it works in the desktop/mobile command
  bar, the terminal CLI, and any chat bot wired to forward it: Telegram forwards the raw
  `/hook ...` line verbatim, Discord's `hook` slash-command group assembles the flag grammar
  from typed UI fields; the other native/webhook bots do not yet forward any backend command,
  tracked as backlog #73). A deterministic authoring
  path that does not depend on the model calling the tool. The grammar is flag-based (a
  single line, so it round-trips through chat surfaces): `/hook create <name> --event E
  --action A [--text ..|--url ..|--cond "field op value"..|--reason ..|--set arg=value..]
  [--matcher A|B] [--scope thread|global] [--disabled]`; `--cond`/`--set` repeat; `edit`
  takes `key=value` scalars plus `--cond`/`--set`. It reuses the same flat-field mapping
  (`params_from_fields` / `build_update_kwargs` in `core/hook_manager.py`) as the REST
  surface, so the three authoring paths cannot drift. The mutating subcommands are
  `agent_allowed=False` (the agent authors via the tool) and hidden from chat command menus
  (only the bare `/hook` lists there). Known limitation: an option value that begins with
  `--` (e.g. a `--text` starting with two dashes) cannot be expressed on the command line
  (shared arg-parser behavior); use the tool, REST, or GUI for such content.
- **REST** (`api/routers/hooks.py`, mounted at `/hooks`): pure CRUD plus
  `POST /hooks/{id}/test`, `GET /hooks/executions` (the execution log, below), and
  `GET /hooks/schema` (the machine-readable taxonomy: per-event legal actions,
  per-action plane/events/params JSON schema, condition operators, the cap; derived
  from `core/hook_spec.py` so clients can render authoring forms from data). The
  request carries `action` plus the flat per-action fields
  (`text` / `conditions` / `reason` / `updates` / `url` / `command` / `timeout_seconds`); the
  response exposes the full `logic` object (discriminated on `action`). Every handler pins
  `user_id` to the authenticated caller; scoped creates pass through the thread-access gate;
  authoring (or switching to) `run_command` is rejected for non-admins (403) or when the
  deployment flag is off (400), and any behavior edit of an existing `run_command` hook
  (anything beyond `enabled`/`name`) re-passes the same gate on every surface (the shared
  `gated_update_action` rule), so authoring-time admin is not a permanent pass. There is
  no webhook/fire endpoint (hooks fire in-process only).

The **desktop and mobile GUI** are clients of that REST surface (not a fourth store
writer): a dashboard **Hooks** section (`components/hooks/`: category-grouped feed +
a single adaptive **HookForm** modal whose fields reflow by event and action) authors,
edits, tests, and toggles hooks; a per-thread **Hooks** tab drives the enable model
below. The action families are category-coded (Guardrails / Context / Reactions) so the
feed and form read as three families rather than one flat list.

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
`PATCH /settings` field. In the GUI, the per-thread **Hooks** tab (Thread Settings) surfaces
this as a tri-state master (Inherit / On / Off, where Inherit sends `clear_hooks_enabled`)
plus per-hook Default / On / Off overrides, folded into the panel's Save batch.

### Execution log

Every hook run is recorded to a bounded per-user log (cap 200, mirroring the
trigger execution log): hook id/name, event, plane, status, an outcome or error
`detail` summary, duration, and the thread/tool it ran against. Statuses: `ok`
(ran and produced an outcome or side effect; `detail` summarizes it, e.g.
`deny: <reason>`, `modify: command`, `inject 84 chars`, `continue`), `no_op`
(ran and produced nothing), `error`, `timeout` (overran the per-hook budget),
`saturated` (never got a dispatch worker), `illegal` (wrong outcome type for
the event; dropped). On `pre_tool_use` an `error`/`timeout`/`saturated` run
also denied the tool call (the fail-closed policy); observe-plane runs record
`ok` on success, never `no_op` (their return values are ignored). This is what
distinguishes "fired and did nothing" (a `no_op` entry) from "never fired" (no
entry), and a guardrail's fail-closed deny (`error`/`timeout`/`saturated`) from
a deliberate one.

Recording is write-behind: the engine reports each run to a recorder attached
to the per-turn registry (`HookRegistry.recorder`, set by the bridge; the empty
`default_registry` has none, so the zero-hook path records nothing), and the
store buffers entries in memory and flushes them on a single worker off the
turn, so recording adds no file I/O to a tool call. Surfaced through
`hook_info(action="log")`, `/hook log [id] [--limit N]`, and
`GET /hooks/executions`; deleting a hook purges its entries.

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
| `DONE` | when a turn finishes (observe fires on normal completion and on error) | observe (fire-and-forget) and/or continue (force another turn) |

`PROMPT_SUBMIT` firing on autonomous turns too (scheduled TODOs, triggers, watchdog,
dreams) is a deliberate upgrade over Claude Code's user-only event: `HookContext`
carries `is_autonomous` / `holder_kind` so a hook can scope to a turn source.

## Two planes

- **Observe plane**: fire-and-forget side effects, **scheduled off-turn** by
  `schedule_observe` (a background loop task when a loop is running, else the dedicated
  `_observe_dispatch_pool`, sized by `HOOK_OBSERVE_DISPATCH_WORKERS`). The fire point returns
  immediately without awaiting the hooks; never blocks or affects the turn, and faults are
  logged and swallowed. The trade-off callers accept: a side effect may complete *after* the
  turn ends (and after the thread lock releases), and pending work is best-effort at process
  shutdown.
- **Mutate plane**: synchronous, in-band (`dispatch` / `adispatch`). The fire point
  awaits the reduced outcome and applies it. Fault policy: the veto path fails closed
  (a raising `PRE_TOOL_USE` hook becomes a `deny`); every other path fails open
  (logged and skipped). A hook can never crash a turn: dispatch isolates each hook's
  run *and* the application of its outcome (a malformed `scratch_patch` or
  `updated_args` is dropped, not propagated), and the final reduction is wrapped so it
  can never raise into a turn.

`DONE` and `POST_TOOL_USE` have observe fire points (the latter is scheduled after the mutate
POST apply, seeing the original tool result; `tool_hooks_active` activates the tool-node seam
for observe-only tool hooks too). `PROMPT_SUBMIT` / `PRE_TOOL_USE` dispatch on the mutate plane
only; an observe registration on those events is inert (no product action needs it yet).
Because observe now dispatches **off-turn** (`schedule_observe`), a slow observe hook no longer
adds latency to the tool call or the turn tail: the POST observe is scheduled and the tool
returns immediately. When observe hooks *do* run, the two planes run their sync hooks on
**separate thread pools** (`_mutate_pool` / `_observe_pool`, sized by `HOOK_MUTATE_POOL_WORKERS`
/ `HOOK_OBSERVE_POOL_WORKERS`), and the off-turn dispatchers use their **own** pool
(`_observe_dispatch_pool`, `HOOK_OBSERVE_DISPATCH_WORKERS`) so a dispatcher waiting on observe
workers cannot starve the very hooks it dispatches. A slow observe hook thus cannot starve the
worker a fast mutate-plane guardrail needs. If a hook still cannot get a worker within its
budget, the dispatcher distinguishes that queue-wait timeout from a genuine execution overrun:
it logs the saturation distinctly and, on `PRE_TOOL_USE`, still fails closed (a guardrail that
could not be evaluated must not silently pass).

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
- `DONE` observe: dispatched at the normal-completion site of `chat`/`astream`, **also on the
  error path** (`completed_normally=False`) so a `done` `notify`/`webhook` can react to a
  failed turn, **and now on cancel/GeneratorExit** (SSE disconnect / `POST /stop`). Because
  observe is fire-and-forget via `schedule_observe` (which awaits nothing), it is now safe to
  fire from the force-close `finally`: a `done_observe_fired` flag tracks whether an in-band
  site already fired, and the `finally` schedules a deferred `_fire_done_observe_sync`
  (`completed_normally=False`) only when it did not, so a `done` `notify` reliably fires
  exactly once even when the client disconnects mid-turn. Both paths share the
  `_fire_done_observe` / `_fire_done_observe_sync` helpers (the async one only schedules).
- `DONE` **continue**: fires on **both** paths. The async `astream` drain loop and the sync
  `chat` tail each defer `backend.begin_release` until after a DONE-continue check
  (`_maybe_done_continuation` / `_maybe_done_continuation_sync`, the sync twin using the sync
  `dispatch`), so a `done` hook re-drives identically on streaming and non-streaming
  transports (webhook bots, non-streaming `/chat`, callable threads). The reduced outcome's
  `DoneOutcome.user_message` is delivered out-of-band through the same notification surface
  as the `notify` action (`_deliver_hook_user_message`), whether or not a continuation is
  also requested.

## In-chat activity lines (desktop)

Meaningful **mutate-plane** hook runs surface as ephemeral, Claude-Code-style activity lines
in the chat, drawn with the desktop's curved-elbow treatment (the same `.elbow` the prompt-bar
hints use), tinted on a fault. They are emitted from the same dispatch seam as the execution
log: `adispatch`/`dispatch` take an optional `emit` sink, and `_record` feeds it a small
`hook_activity` record (`name`/`event`/`status`/`detail`/`tool_name`) only for a *meaningful*
run (a deny/modify/inject/rewrite or a fault; a bare `allow`/`no change`/`no_op` is skipped, so
a guarded tool call does not draw a line every time). The fire point streams it: the tool node
(`prompt_submit` under the user message, `pre`/`post_tool_use` around the tool block) rides the
`hook_activity` custom event; `astream` yields it directly. The lines are **live-only**:
nothing is persisted, so a reload shows the message without them (the durable record is
`/hook log`). Observe-plane runs are not surfaced this way (they dispatch off-turn, possibly
after the stream closes); the `done` line is likewise deferred. Mobile is a later render port;
the SSE event is app-agnostic and unknown-event-tolerant on the other clients.

## What is deferred (not yet shipped)

- The `nym` **workflow** logic substrate (sandboxed, out-of-process). `run_command` is the
  shipped subprocess pathfinder for it. The seven canned actions (`inject_context`,
  `block_if_matches`, `rewrite_arg`, `notify`, `create_todo`, `webhook`, `run_command`) all ship.
- Observe fire points for `PROMPT_SUBMIT` / `PRE_TOOL_USE` (a registration on those
  events is inert; no product action needs them yet). `POST_TOOL_USE` and `DONE` have
  observe fire points.
- In-chat activity lines for the `done` event and for the mobile client, and the presets
  library.

## Package

- `core/hooks/`: `base.py` (contract), `registry.py` (in-process registry, `has_mutating`/
  `has_observe`, the per-turn `recorder` slot), `scratch.py` (per-thread store),
  `dispatch.py` (planes + reduction + fault policy; `tool_hooks_active`; plane-scoped
  `_mutate_pool`/`_observe_pool` + the off-turn `schedule_observe` on its own
  `_observe_dispatch_pool` + the queue-wait-vs-execution timeout split; reports each run to
  the recorder and, on the mutate plane, to an optional `emit` sink for in-chat lines),
  `actions.py` (the seven actions incl. `run_command`; per-event planes via the spec's
  `plane_for`/`plane_by_event`), `bridge.py` (definitions → per-turn registry, registering
  each on its per-event plane with its `definition_id`, a per-registration timeout, + the
  recorder).
- `core/hook_spec.py`: the taxonomy single source (`ActionSpec`: base plane, legal events,
  `observe_events` for per-event plane flips, text-action flag; `plane_for`/`plane_by_event`).
  `EVENT_ACTIONS`/`TEXT_ACTIONS` (store) and `ACTION_PLANES` (engine) derive from it;
  `GET /hooks/schema` exposes it (including `plane_by_event` and a `gated` flag);
  `tests/test_hook_spec.py` pins the independent copies (the engine `ACTIONS` table,
  the logic variants, the frontend `HOOK_EVENT_ACTIONS`) in lockstep.
- `core/hook_manager.py`: `HookDefinition` + the `HookLogic` discriminated union (seven
  variants) + `HookStore` records + the per-user `HookManager` (store-only, no engine
  import; a corrupt store file is quarantined to `<user>.corrupt-*.json`, never
  silently overwritten), plus the execution log (`HookExecution`,
  `log_execution`/`get_executions` write-behind on a single worker,
  `make_execution_recorder`). Observe actions reuse `core/notifications.py` +
  `core/fcm.py` (notify), `core/todo_manager.py` (create_todo), and
  `core/http_policy.py` (webhook). `run_command`'s double gate is `GATED_ACTIONS` +
  `run_command_authoring_error(action, *, is_admin)` (checks `HOOKS_RUN_COMMAND_ENABLED`
  then admin) at create AND on any behavior update of a gated hook (the shared
  `gated_update_action` rule; enabled/name-only edits are exempt), on all three
  authoring surfaces; execution re-checks the deployment flag (not role).
- `core/conditions.py`: `HookCondition` + `evaluate_conditions` (shared with triggers,
  which re-export `TriggerCondition`).
- `core/text_format.py`: `safe_format` template substitution (shared with triggers).
- Authoring: `tools/hooks.py` (`hook_config`/`hook_info`), `api/routers/hooks.py`
  (`/hooks` CRUD), `skills_bundled/hook-management/`.
- Enable model: `Settings.hooks_enabled`, `ThreadConfig.hooks_enabled` /
  `hook_overrides`, `core/agent_safety.py::get_effective_hook_enabled`.

The per-turn wiring lives in `core/agent.py::_hook_registry_for_turn` (loads a user's
enabled hooks, mtime-cached, and builds the registry) and
`core/agent_safety.py::graph_run_config` (stamps `configurable["hook_registry"]` plus the
turn source `hook_is_autonomous`/`hook_holder_kind`/`hook_trigger_label`, which
`nodes.py::_build_tool_hook_ctx` surfaces so a tool hook can scope by autonomous-vs-interactive);
every fire point falls back to the empty `default_registry` when no per-turn registry is set,
so a user with no enabled hooks runs byte-identically to the spine.
