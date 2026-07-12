# Lifecycle Hooks

Nymeria has a Claude-Code-style lifecycle-hooks engine: small pieces of logic that
run at defined moments in an agent turn and can observe or steer it. The engine spine
shipped first (the machinery, wired to in-process fixtures); the product surface then
landed on top of it as a set of **canned actions** a user or the agent can attach to an
event. This document covers both the engine and those actions. The desktop and mobile
clients ship a GUI over the REST surface (a dashboard **Hooks** panel to author and toggle
hooks, plus a per-thread **Hooks** tab for enablement). The `run_command` action (a hook
that shells out, admin + deployment-flag gated) shipped as the pathfinder for the `nym`
workflow substrate; that substrate now ships too, as the `run_workflow` action (backlog
#80): a hook whose logic is a published, approved nym workflow.

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
| `require_approval` | mutate | `pre_tool_use` | **hold** a tool call until the user approves or denies it; no answer = deny |
| `notify` | observe | `post_tool_use`, `done` | send an in-app + push notification |
| `create_todo` | observe | `post_tool_use`, `done` | add a user TODO |
| `webhook` | observe | `post_tool_use`, `done` | POST a JSON payload to a URL |
| `run_command` | mutate on `prompt_submit`/`pre_tool_use`, observe on `post_tool_use`/`done` | all four | run a shell command with the hook context as JSON on stdin (admin + flag gated) |
| `run_workflow` | mutate on `prompt_submit`/`pre_tool_use`, observe on `post_tool_use`/`done` | all four | run a published, approved `nym` workflow with the hook context as its `event` param |
| `turn_metadata` | mutate | `prompt_submit` (reserved: the system hook only) | render the built-in `[Time:]/[Trigger:]` block (see "The system turn-metadata hook" below) |

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

**`require_approval`** is the interactive `pre_tool_use` guardrail (backlog #77): when its
`conditions` (and the shared `matcher`) match, the tool call **pauses in-band** while every
surface is asked for a decision, then resolves from the first answer. The hold is an
awaitable future keyed by a durable pending record (`core/hook_approvals.py`, records under
`data_dir/hooks/approvals/`, capped at 20 pending per user); minting the record publishes a
`hook_approval` autonomous SSE event, an in-app notification, and a push (FCM), and every
resolution (any outcome, any surface) publishes `hook_approval_resolved` so all surfaces
retract their prompt. Resolve surfaces: desktop/mobile **Approve/Deny buttons on the
tool-call card** itself, `GET /hooks/approvals` + `POST /hooks/approvals/{record_id}/resolve`
(owner-or-admin; 404 for a record the caller may not resolve, 409 when no longer pending),
the `/hook approvals` / `/hook approve <id> [note]` / `/hook deny <id> [note]` commands
(`agent_allowed=False`: the agent can never approve its own calls), Telegram/Discord inline
buttons, and a Rich-CLI decision form. Params: `prompt` (templated, default
`"Approve tool call {tool_name}?"`), `conditions`, and the author-side `timeout_seconds`
window (10..600, default 180; the author picks it, never the agent). Outcomes: approved →
allow (logged as `allow: approved by <user>`); denied → deny, with the resolver's note and a
hard no-retry tail; **timeout → deny** with the hardened message ("the user did not approve
this tool call within N seconds... do not retry the same command; if it matters, notify the
user and wait for their explicit approval. Silence is not consent."); turn abort → deny; a
mint failure fails closed. The waiting action deletes its record on every exit shape; an
hourly API sweep purges crash-orphaned records, and a resolve that finds no live waiter
(restart, lost race) cleans up and answers 409. The action is `async def`, awaited on the
loop (sync fire points run it via `asyncio.run` on the calling thread), so a minutes-long
hold never occupies a dispatch-pool worker; the bridge derives the per-registration budget
from `timeout_seconds`, so the dispatcher never times the hold out before its own window.

**`notify`** / **`create_todo`** / **`webhook`** are the observe-plane side effects on the
"after something happened" events (`post_tool_use`/`done`). They run fire-and-forget: the
fire point ignores their return, and each wraps its side effect so a failure is logged, not
raised. `notify` delivers an in-app + push notification (bypassing the autonomous-suppression
gate, since a user-authored hook should always deliver); `create_todo` adds a user TODO;
`webhook` POSTs `{"text", "thread_id", "user_id"}` to a `{placeholder}`-templated URL through
the **SSRF-safe** `http_policy` egress helper (private/loopback/metadata targets are refused).
On graceful API shutdown a bounded drain (`triggers/api.py::_drain_observe_hooks`, 5s per
barrier: loop tasks via `adrain_observe`, pool futures via `drain_observe` off-loop) flushes
observe work the turn already accepted, so a restart does not silently drop a queued side
effect; a hung hook cannot stall shutdown past the bound.

**`run_command`** runs a shell command and is the **one action whose plane flips per event**:
mutate on `prompt_submit`/`pre_tool_use` (its output can steer the turn), observe on
`post_tool_use`/`done` (fire-and-forget). It is the pathfinder for the `nym` subprocess
substrate, so it is deliberately hardened and **double-gated** (admin account AND the
`HOOKS_RUN_COMMAND_ENABLED` deployment flag). Both halves are enforced at authoring on every
surface AND again at execution: at fire time the deployment flag is re-read and the hook
**owner's** admin role is re-resolved from `ctx.user_id` (the turn's auth identity), so a
`run_command` hook planted by a direct write to `data/hooks/<user_id>.json`, or one whose
owner was later demoted, is neutered (returns a no-op; on `pre_tool_use` that ALLOWS, so a
non-admin cannot plant a guardrail that blocks tool calls). The authoring-time admin gate
alone is bypassable by a direct store-file edit, which is why the owner re-check exists. The
command receives the full hook context as **JSON on stdin** (never
argv) and runs with a **minimal environment** (`PATH`/`HOME`/`LANG`/`LC_ALL`/`TMPDIR` plus
`NYMERIA_HOOK_EVENT`/`_THREAD_ID`/`_USER_ID`/`_TOOL_NAME`; no inherited process secrets), in
its own process group (`start_new_session`), under the author-configured `timeout_seconds`
(1..300; the mutate/in-band events additionally cap it at 60s so a hook cannot stall a turn).
On timeout the whole process group is `SIGKILL`ed. The contract by event: on `prompt_submit`,
exit 0 stdout is injected as context (**fail-open**: a spawn error or non-zero exit injects
nothing); on `pre_tool_use`, exit 2 is a **deny** (stderr is the reason), exit 0 with empty
stdout is allow, exit 0 with a JSON decision object is that decision, any other non-zero exit
is a script bug and allows WITH a diagnostic note (visible in `/hook log` as
`allow: guardrail exited N...` and as an activity line, instead of an indistinguishable bare
no-op), and a spawn failure or timeout is a **fail-closed deny**; on `post_tool_use`/`done`
it is observe (output ignored). Output is read-capped (50 KB retained per stream, 10 KB
injected) by an incremental bounded pump: one stdin-writer thread plus one capped
reader-drainer per pipe, so parent memory never scales with the child's output volume and a
two-pipe flood cannot deadlock; the wall clock still bounds pipe EOF, so a backgrounded
grandchild holding the pipes open past the deadline is a timeout (group-killed), exactly as
before.

**`run_workflow`** runs a published `nym` workflow as the hook's logic (backlog #80: the
workflow substrate `run_command` pathfound). Same plane shape as `run_command`: mutate on
`prompt_submit`/`pre_tool_use`, observe on `post_tool_use`/`done`. It is **not**
admin/flag gated; the control is the workflow platform's own per-revision approval gate:
authoring a hook validates the binding (`workflow_binding_error`: published workflow
exists, current revision admin-approved, bound `params` all declared, required params
covered), and every fire re-gates through `run_workflow_by_id` (revoking a workflow's
approval neuters every hook bound to it, immediately, with no hook edit). Params:
`workflow_id`, static `params` bound at authoring, `timeout_seconds` (5..600, default 60:
the engine wall clock, which the dispatcher budget rides via the shared
`logic.timeout_seconds` seam), and `on_fault` (`allow`|`deny`, default `allow`,
`pre_tool_use` only). Per-fire dynamics arrive through the workflow's optional `event`
parameter: when the signature declares `event`, the fire passes the full hook context as
a JSON-safe dict (event, tool name/args/result, prompt or final text capped at 16 KB,
scratch, context-usage stats), mirroring the trigger `run_workflow` action. The contract
by event: on `prompt_submit`, an ok run returns injected context (a plain-string output,
or the `inject_context` key of a dict output; empty = no-op); on `pre_tool_use` an ok
dict output is the decision (`{"decision": "deny", "reason": ...}` /
`{"decision": "modify", "updates": {...}}` / allow with optional `note` and
`scratch_patch`), empty output allows, and **every fault** (missing/unapproved workflow,
crash, timeout, refusal, non-dict output) maps through `on_fault`: `allow` proceeds with
a diagnostic note in `/hook log`, `deny` fails closed with the fault reason; on
`post_tool_use`/`done` it is observe (fire-and-forget side effects, output ignored). One
caveat: `nym.approve` cannot hold an in-band hook. A workflow that suspends for approval
counts as a **success on the observe events** (the approval resolves out-of-band, the
trigger precedent) but as a **fault on the mutate events** (mapped via `on_fault` on PRE,
raised on `prompt_submit`), so guardrail workflows should not call `nym.approve`
(use the `require_approval` action for interactive holds). Note that every suspending
fire still mints a durable pending-approval record (7-day expiry, hourly sweep), so a
frequently-firing hook bound to a suspending workflow accumulates them until resolved
or reaped.

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
  (create/update/delete/install) and `hook_info` (list/detail/test/log/templates). The
  action is picked with
  `hook_action` (default `inject_context`); text actions take `text`, the guardrail
  actions take a `params` dict (`{"conditions": [...], "reason": ...}` /
  `{"conditions": [...], "updates": {...}}`; `run_workflow` takes
  `{"workflow_id": ..., "params": {...}, "timeout_seconds": ..., "on_fault": ...}`);
  the definition-level fire gate is
  authored via `fire_conditions` (a list of condition objects) + `once` on both
  create and update, and the lifecycle flag via `single_use` (below). A create
  auto-binds the current thread for
  `scope="thread"` (including the real `default` thread); `test` renders/describes the
  hook without firing; `log` reads the execution log (below); `install` instantiates a
  bundled template by `template_id` (see Bundled templates). The `hook-management`
  bundled skill front-loads these. Like every surface, `scope` is create-only
  (a re-scope is a delete + create: an update cannot supply the access-gated
  thread binding).
- **Slash command** `/hook` (`core/command_service.py`, catalog in `core/registry_defaults.py`):
  `list` / `show` / `create` / `edit` / `enable` / `disable` / `delete` / `test` / `log` /
  `templates` / `install <template_id> [--scope thread|global] [--text ..] [--disabled]`, plus the
  resolve surface `approvals` / `approve <record_id> [note]` / `deny <record_id> [note]`
  (record-id prefix match; owner-or-admin; `agent_allowed=False` so the agent cannot
  approve its own held calls), reaching
  the same store through `POST /commands/execute` (so it works in the desktop/mobile command
  bar, the terminal CLI, and any chat bot wired to forward it: Telegram forwards the raw
  `/hook ...` line verbatim, Discord's `hook` slash-command group assembles the flag grammar
  from typed UI fields; the other native/webhook bots do not yet forward any backend command,
  tracked as backlog #73). A deterministic authoring
  path that does not depend on the model calling the tool. The grammar is flag-based (a
  single line, so it round-trips through chat surfaces): `/hook create <name> --event E
  --action A [--text ..|--url ..|--cond "field op value"..|--reason ..|--set arg=value..]
  [--fire-cond "field op value"]... [--once] [--single-use] [--matcher A|B]
  [--scope thread|global]
  [--disabled]`; `run_workflow` adds `--workflow <id> [--workflow-params
  '{"k": "v"}'] [--on-fault allow|deny]` (`--workflow-params` takes one JSON
  object); `--cond`/`--set`/`--fire-cond` repeat; `edit`
  takes `key=value` scalars (incl. `once=true|false`, `single_use=true|false`,
  `workflow=<id>`, `workflow_params='{...}'`, `on_fault=allow|deny`)
  plus `--cond`/`--set`/`--fire-cond`. It reuses the same flat-field mapping
  (`params_from_fields` / `build_update_kwargs` in `core/hook_manager.py`) as the REST
  surface, so the three authoring paths cannot drift. The mutating subcommands are
  `agent_allowed=False` (the agent authors via the tool) and hidden from chat command menus
  (only the bare `/hook` lists there). Known limitation: an option value that begins with
  `--` (e.g. a `--text` starting with two dashes) cannot be expressed on the command line
  (shared arg-parser behavior); use the tool, REST, or GUI for such content.
- **REST** (`api/routers/hooks.py`, mounted at `/hooks`): pure CRUD plus
  `POST /hooks/{id}/test`, `GET /hooks/executions` (the execution log, below),
  `GET /hooks/approvals` + `POST /hooks/approvals/{record_id}/resolve` (the
  `require_approval` resolve surface),
  `GET /hooks/templates` + `POST /hooks/templates/{template_id}/install` (the
  bundled-template surface, below), and
  `GET /hooks/schema` (the machine-readable taxonomy: per-event legal actions,
  per-action plane/events/params JSON schema, condition operators, the
  `fire_gate` field surface, the `lifecycle` field surface (`single_use`), the
  cap; derived
  from `core/hook_spec.py` so clients can render authoring forms from data). The
  request carries `action` plus the flat per-action fields
  (`text` / `conditions` / `reason` / `updates` / `url` / `command` /
  `workflow_id` / `workflow_params` / `on_fault` / `timeout_seconds`)
  and the definition-level `fire_conditions` / `once` / `single_use`; the
  response exposes the full `logic` object (discriminated on `action`) plus the
  fire-gate and lifecycle fields and the `template` provenance id. Every handler pins
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

### Bundled hook templates

Curated, ready-to-install hook definitions ship with the package in
`nymeria/hooks_bundled/*.json` (mirroring `skills_bundled/`), loaded and
dry-validated by `core/hook_templates.py` (each template's definition must pass
`HookDefinition.model_validate` before it is listed, so a bad file is skipped,
never installed). A template carries an `id`, display metadata, a default
`scope`, and the full definition body; installing stamps the template id into
the hook's `template` provenance field, and installs are idempotent per
(template, scope, thread binding): re-installing returns the existing hook
instead of duplicating it. Installed hooks are ordinary hooks (edit, disable,
delete as usual); the provenance field only records where they came from.

All three authoring surfaces expose the catalog: `hook_info(action="templates")` +
`hook_config(action="install", template_id=...)`, `/hook templates` + `/hook install
<template_id>`, and `GET /hooks/templates` + `POST /hooks/templates/{template_id}/install`.
Install-time knobs: `scope` (thread installs bind the current/supplied thread through the
same access gate as create), `text` (override the template's message), `enabled`. Templates
whose action is gated (`run_command`) pass the same authoring gates as a manual create.

Shipped templates:

- `context-checkpoint-advisory`: the backlog #72 recipe below, ready-made
  (global, `post_tool_use`, `once`, fires at 85% of the compact trigger).
- `turn-end-prompt`: a thread-scoped one-shot DONE prompt (`once` +
  `single_use`), the reusable shape behind `/done`.

### The fire gate: fire_conditions and once (the WHEN layer)

Every hook definition carries an optional engine-level fire gate, evaluated by the
bridge BEFORE the hook's logic runs, on any event and around any action (and any
future logic substrate, e.g. `nym` workflows). It is the declarative WHEN layer:
the platform ships the firing semantics, the logic slot stays swappable. Distinct
from the guardrail actions' per-logic `conditions`, which match tool args and are
part of that logic's semantics.

- `fire_conditions`: a list of `{"field", "operator", "value"}` conditions, ANDed
  (`core/conditions.py`, shared with triggers); empty = always fire. Evaluated
  against a per-event data dict (`bridge.fire_condition_data`):
  - Meta fields (top level): `event`, `thread_id`, `user_id`, `is_autonomous`,
    `holder_kind`, `trigger_label`, `tool_name`, `tool_status`, `prompt`,
    `final_text`.
  - Tool args, nested under `args.` (dotted paths resolve): `args.command`.
  - Context-usage signal (raw numbers; ABSENT when unknown, so a numeric
    condition on an unknown signal is a non-match, never a compare against 0):
    `context_tokens` (current occupancy), `context_limit` (model window),
    `compact_trigger_tokens` (resolved auto-compact trigger; absent unless
    `context_management=auto_compact`), and the derived
    `context_pct_of_trigger` / `context_pct_of_limit` percentages.
- Numeric operators: `gt` / `gte` / `lt` / `lte` join the string operators
  (`equals`, `not_equals`, `contains`, `starts_with`, `matches_regex`). Both
  sides coerce via `float()`; non-numeric on either side is a non-match, never a
  raise. Being shared, the trigger stack gains them too.
- Dream turns are gateable: a dream cycle (see
  `docs/agent-systems/dreaming.md`) runs with `holder_kind` = `dream`
  and `trigger_label` = `Dream("<parent_thread_id>")`. Hooks fire on dreams
  like any autonomous turn (guardrails included); use a fire condition
  `holder_kind not_equals dream` to keep a noisy hook out of dream cycles, or
  `holder_kind equals dream` to scope a hook to dreams only.
- `once`: fire once per gate crossing. After firing, the hook stays silent while
  `fire_conditions` keep matching and re-arms when they stop matching (e.g. a
  context warning re-arms after compaction drops occupancy). With no
  `fire_conditions`, `once` fires once per thread. State is a per-thread scratch
  sentinel (`hook_once:<hook_id>`), written through the contract's
  `scratch_patch` channel, in-memory (a restart re-arms). `once` is best-effort,
  not a transactional guarantee: on the observe plane two near-simultaneous
  off-turn fires can race the sentinel, and on the mutate plane a concurrent
  same-turn tool batch can double-fire (each tool call's dispatch snapshots the
  scratch before the other writes the sentinel). Both directions are benign:
  the worst case is one duplicate fire; re-arm is unaffected. An action that
  raises (or returns an illegal outcome type) does not consume the shot; the
  hook re-fires on the next matching event.
- Malformed `fire_conditions` make the hook a no-op (never a fail-closed block),
  the same posture as the guardrail actions. A gate that does not fire records a
  `no_op` in the execution log; a heavily-gated global tool hook therefore churns
  the 200-entry log while idle (working as intended: the log is what tells
  "gated" apart from "never fired").

The context-usage signal also feeds `{placeholder}` templating: `inject_context`
(and every text action) can render `{context_tokens}`, `{context_limit}`,
`{compact_trigger_tokens}`, `{context_pct_of_trigger}`, `{context_pct_of_limit}`
(empty string when unknown). Signal sources per event: `prompt_submit`/`done`
read the token tracker (`agent_compaction.hook_context_stats`); `pre`/
`post_tool_use` read the freshest mid-turn occupancy from the running state's
last AI message (the same source as sub-turn compaction), falling back to the
turn-entry stamp in `graph_run_config`. Stats are stamped only when the turn has
enabled hooks, so the zero-hook hot path is unchanged.

### Single-use hooks (lifecycle)

`HookDefinition.single_use` makes a hook delete itself after its first
successful run: when the execution recorder logs an `ok` status for a
single-use registration, the definition is removed synchronously (log entries
are kept, so `/hook log` still shows the fire). Distinct from `once`, which
silences a persistent hook per gate crossing via an in-memory sentinel:
`once` re-arms on restart (the sentinel is lost), while `single_use` cannot
re-fire because the definition itself is gone. One-shot hooks (e.g. `/done`,
the `turn-end-prompt` template) set both, belt and braces: `once` suppresses a
same-process double fire, `single_use` ends the lifecycle. The synchronous
delete is also a claim primitive: a caller that can still delete the
definition has proven it never fired (how the `/done` race is resolved).
Non-`ok` runs (`no_op`, `error`, `timeout`) do not consume a single-use hook.

### Recipe: context checkpoint advisory (backlog #72)

The flagship fire-gate recipe, which now ships as a bundled template: `/hook
install context-checkpoint-advisory` (or the tool/REST equivalents) installs
it ready-made, and the manual create below is the equivalent long-hand. It
warns the agent once per approach to the
auto-compaction trigger so it checkpoints working state (memory/notepad or a
file) before the context is summarized. Fires on `post_tool_use` so a long
tool-heavy turn still gets warned mid-turn; `once` + re-arm gives one advisory
per crossing (compaction drops occupancy, which re-arms it; a concurrent tool
batch can rarely duplicate the advisory, which is harmless).

```
/hook create context-checkpoint-advisory --event post_tool_use \
  --action inject_context --scope global --once \
  --fire-cond "context_pct_of_trigger gte 85" \
  --text "[Context advisory] Approaching auto-compaction: {context_tokens} of {compact_trigger_tokens} trigger tokens ({context_pct_of_trigger}%). Older messages will soon be summarized. Persist important working state now: durable facts via memory_add(scope=global), in-flight task state via memory_add(scope=thread) or a checkpoint file. Then continue naturally; do not rush to finish before compaction."
```

Tune the threshold by editing the condition (`/hook edit <id> --fire-cond
"context_pct_of_trigger gte 90"`). The same shape works on `prompt_submit`
(warns at turn entry instead of mid-turn), and a sibling `notify` hook with the
same gate can push the human a heads-up.

### Recipes: bash guardrails

`bash_execute` ships with a small always-on hardline guard against catastrophic
commands (see `tools.md`); everything softer is user policy, authored as
`pre_tool_use` hooks. Conditions match the call's args, so `field` is `command`
(dotted paths reach nested args on other tools). Canned examples, in `/hook`
grammar (the tool/REST/GUI express the same fields):

```
# Deny sudo in this thread
/hook create no-sudo --event pre_tool_use --action block_if_matches \
  --matcher bash_execute --cond "command contains sudo" \
  --reason "sudo is not allowed in this thread; ask the user to run it"

# Deny git push (review-before-push policy)
/hook create no-git-push --event pre_tool_use --action block_if_matches \
  --matcher bash_execute --cond "command matches_regex (^|[;&|]\s*)git\s+push" \
  --reason "Pushing is manual in this project: show the user the diff instead"

# Deny package installs globally
/hook create no-installs --event pre_tool_use --action block_if_matches \
  --matcher bash_execute --scope global \
  --cond "command matches_regex (pip3?|npm|apt(-get)?|uv)\s+(install|add)" \
  --reason "Installs are admin-only; ask the user"

# Pin risky commands to a scratch directory
/hook create pin-cwd --event pre_tool_use --action rewrite_arg \
  --matcher bash_execute --cond "command contains rm -r" \
  --set working_directory=/tmp/agent-scratch
```

```
# Hold rm -r for a human decision instead of hard-denying it
/hook create approve-rm --event pre_tool_use --action require_approval \
  --matcher bash_execute --cond "command contains rm -r" \
  --text "The agent wants to delete files. Allow it?" --timeout 300
```

Layering: the hardline guard is the non-negotiable baseline (cannot be disabled),
`block_if_matches`/`rewrite_arg` hooks are per-user/per-thread policy on top,
`require_approval` turns a would-be hard deny into a human review (no answer still
denies), and an admin `run_command` hook can implement arbitrary allow/deny logic
(exit 2 = deny).

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

### The system turn-metadata hook (backlog #66)

The `[Time:]/[Trigger:]` metadata block every turn carries on its message tail is
itself a hook: the reserved system definition `turn-metadata` (action
`turn_metadata`, event `prompt_submit`, global scope, `created_by: "system"`,
`system: true` on the REST payload). It appears in every user's hook list without
being stored anywhere: the definition is **virtual until edited**. The first edit
(any surface: `/hook edit turn-metadata ...`, `hook_config(action="update",
hook_id="turn-metadata", ...)`, `PATCH /hooks/turn-metadata`) materializes a
stored copy (copy-on-write, exempt from the per-user cap); **delete = reset**: it
removes the stored copy and the built-in default reappears (the hook is never
truly deletable; log entries survive a reset). With no stored copy the turn takes
a byte-identical built-in fast path (no engine dispatch at all), so the pristine
default is exactly the pre-#66 behavior.

What you can change: `text` (the template), `enabled`, `name`, `fire_conditions`,
and `once`. What is locked: `event`, `scope`, `action`, and `single_use` (and the
action can never be authored onto another hook). The template is constrained to a
fixed two-line frame, `[Time: <interior>]` newline `[Trigger: <interior>]`, with
non-empty interiors containing no `]` and no newlines; interiors may use
`{time}` (the wall clock in the user's timezone), `{trigger}` (the resolved
trigger label), and the standard hook vars. The frame guarantees the
history-strip regex (`core/agent_history.py`) keeps matching, so customized
metadata never leaks into compaction; the seam also re-validates the RENDERED
block against the strip pattern and, on any mismatch, dispatch fault, or
unbindable definition, **falls back to the built-in block** with a visible entry
in the execution log (a broken edit can never silently kill turn metadata).
Metadata is omitted only deliberately: `enabled=false`, a per-thread
`hook_overrides["turn-metadata"]` off, or an unfired `fire_conditions`/`once`
gate (e.g. gate on `is_autonomous` to stamp only autonomous wake-ups).

Enablement deviates from the standard model in ONE way: the master kill switch
(`hooks_enabled`, global or per-thread) does NOT apply. It governs user hook
logic; turn metadata predates it, and honoring it would silently strip metadata
on deployments that disabled hooks. Resolution is per-thread
`hook_overrides["turn-metadata"]`, else the definition's `enabled`
(`agent_safety.get_effective_system_hook_enabled`). Two more deltas from
ordinary prompt_submit hooks: the block lands at the message PREFIX (not the
`<hook_context>` sentinel tail), and the dispatch context carries no
context-usage signal (numeric context fire conditions are non-matches). A
customized hook records one execution-log entry per turn like any other
prompt_submit hook; the pristine fast path records nothing.
`AUTONOMOUS_MODE_RULES` and the queued-prompt drain headers stay hardcoded
(deliberate scope cut; see `docs/private/plans/metadata-injection-hook.md`).

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
`GET /hooks/executions`; deleting a hook purges its entries (except a system-hook
reset, which keeps them so fallback history survives).

## The model: when → logic → return

A hook has three parts:

- **WHEN** (the trigger): one of a fixed set of lifecycle events, plus an optional
  matcher (a tool-name filter) and the definition-level fire gate
  (`fire_conditions` + `once`, above), all declarative and engine-evaluated
  before any logic runs.
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
  `scratch_patch` on the outcome, never as a live handle. The context-usage signal
  (`context_tokens` / `context_limit` / `compact_trigger_tokens`, optional ints,
  best-effort) rides the same contract, so a future workflow-substrate hook
  receives it unchanged.
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
rather than a parallel re-drive. Queued user prompts win: the DONE-continue check runs
only when the pending queue drains empty, and multiple DONE hooks firing on one turn
produce a single continuation with their reasons joined.

### /done: a one-shot DONE prompt (backlog #70)

`/done <prompt>` is the user-facing packaging of that machinery: a chat_stream slash
command (like `/quick`, intercepted in the chat router, not the command registry) that
arms a one-shot follow-up on the current thread while a turn is running. Busy thread:
it creates a thread-scoped `done` + `inject_context` hook carrying the prompt (`once` +
`single_use`, `created_by="user"`) and acks without running a turn; the running turn's
DONE fire injects the prompt as a continuation and the hook deletes itself. Idle
thread: the prompt simply runs as a normal turn now (the degenerate case). The
arm/turn-end race is claimed by delete: if the turn finishes while `/done` is arming,
a successful hook delete proves it never fired (run the prompt now), a failed delete
proves the DONE fire consumed it. Works on every surface that reaches the chat
endpoints (GUI, CLI, bots); self-invoked agent turns are excluded.

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

- All nine canned actions ship (`inject_context`, `block_if_matches`, `rewrite_arg`,
  `require_approval`, `notify`, `create_todo`, `webhook`, `run_command`,
  `run_workflow`), including the `nym` workflow substrate. Deferred on
  `run_workflow`: a delivery path for the result of a workflow that suspends via
  `nym.approve` mid-hook (today: observe = out-of-band success, mutate = fault).
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
  `actions.py` (the nine actions incl. `require_approval`, `run_command`, and
  `run_workflow` with its `hook_event_payload` context dump; per-event planes via the spec's
  `plane_for`/`plane_by_event`; `context_usage_fields` + the context template vars),
  `bridge.py` (definitions → per-turn registry, registering
  each on its per-event plane with its `definition_id`, a per-registration timeout, + the
  recorder; the `_FireGate` wrapper evaluating `fire_conditions`/`once` against
  `fire_condition_data` before any logic runs).
- `core/hook_spec.py`: the taxonomy single source (`ActionSpec`: base plane, legal events,
  `observe_events` for per-event plane flips, text-action flag; `plane_for`/`plane_by_event`).
  `EVENT_ACTIONS`/`TEXT_ACTIONS` (store) and `ACTION_PLANES` (engine) derive from it;
  `GET /hooks/schema` exposes it (including `plane_by_event` and a `gated` flag);
  `tests/test_hook_spec.py` pins the independent copies (the engine `ACTIONS` table,
  the logic variants, the frontend `HOOK_EVENT_ACTIONS`) in lockstep.
- `core/hook_manager.py`: `HookDefinition` + the `HookLogic` discriminated union (nine
  variants) + `HookStore` records + the per-user `HookManager` (store-only, no engine
  import; `run_workflow` create/logic-edit validates the binding via
  `run_workflow_authoring_error`, delegating to the shared trigger-side
  `workflow_binding_error`; a corrupt store file is quarantined to
  `hooks/quarantine/<user>.corrupt-*.json` via the shared
  `core/storage_paths.py::quarantine_corrupt_file` helper, never silently
  overwritten), plus the execution log (`HookExecution`,
  `log_execution`/`get_executions` write-behind on a single worker,
  `make_execution_recorder`). Observe actions reuse `core/notifications.py` +
  `core/fcm.py` (notify), `core/todo_manager.py` (create_todo), and
  `core/http_policy.py` (webhook). `run_command`'s double gate is `GATED_ACTIONS` +
  `run_command_authoring_error(action, *, is_admin)` (checks `HOOKS_RUN_COMMAND_ENABLED`
  then admin) at create AND on any behavior update of a gated hook (the shared
  `gated_update_action` rule; enabled/name-only edits are exempt), on all three
  authoring surfaces; execution re-checks the deployment flag (not role).
- `core/hook_approvals.py`: the `require_approval` hold machinery: durable pending
  records under `data_dir/hooks/approvals/` (mint/list/load/delete, 20-per-user cap,
  stale sweep on an hourly API heartbeat), the `HookApprovalCoordinator`
  (`FutureRendezvous` keyed by record id; `resolve` wakes the waiting action,
  `abort_thread` is wired into the turn-abort cascade), and the `hook_approval` /
  `hook_approval_resolved` autonomous-event + notification + push announcers.
- `core/conditions.py`: `HookCondition` + `evaluate_conditions` (shared with triggers,
  which re-export `TriggerCondition`); string operators plus the numeric
  `gt`/`gte`/`lt`/`lte` (float coercion, non-numeric = non-match).
- `core/text_format.py`: `safe_format` template substitution (shared with triggers).
- `core/hook_templates.py` + `nymeria/hooks_bundled/`: the bundled-template
  catalog (`HookTemplate`, `load_templates` from `settings.bundled_hooks_dir`,
  `install_template` with the idempotency + gated-action checks) and the
  shipped template JSONs.
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
