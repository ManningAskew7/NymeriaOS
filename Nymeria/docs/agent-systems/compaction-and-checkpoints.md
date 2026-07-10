# Compaction, Checkpointer, and Thread History

How Nymeria's conversation state is persisted, compacted, and displayed  -  and what to check when a thread behaves oddly.

---

## Big picture

A thread's conversation lives in three places:

1. **LangGraph state (`messages` channel)**  -  the live list the LLM sees on each turn. This is what `/compact` trims.
2. **LangGraph checkpoint history**  -  every state transition writes a new row to `checkpoints` and a new blob to `checkpoint_blobs`. These accumulate *forever* unless explicitly pruned.
3. **Display filter**  -  `get_conversation_history()` in `core/agent.py` transforms the raw message list into what the frontend actually renders (hides internal messages, consolidates tool calls into turns).

`/compact` only operates on (1). Until recently, (2) accumulated forever  -  and (3) has a few sharp edges around internal message types.

---

## Compaction flow

Triggered by `/compact`, `POST /threads/{id}/compact`, or automatically when token usage crosses the configured trigger. Auto-compaction fires at three points: **pre-flight** (before a new user turn), **sub-turn** (mid-loop, after a tool batch, see "Sub-turn trigger" below), and as a last resort on a context-overflow exception. The trigger has two modes (`COMPACT_THRESHOLD_MODE`):

- **`tokens`** (default): fires when input tokens reach the absolute count `COMPACT_THRESHOLD_TOKENS` (1,000–2,000,000, default 200,000), clamped at runtime to the model's context window so an oversized setting never disables compaction. Absolute counts stay put when you switch models with different context windows, which is why this is the default.
- **`percentage`**: fires when input tokens reach `COMPACT_THRESHOLD * context_limit`. `COMPACT_THRESHOLD` accepts `0.05` through `0.95` (default 0.8).

The check uses **real provider-reported input tokens**, not character estimates. `core/token_tracker.py` records `last_input_tokens` from each AIMessage via `core/token_usage.py::extract_from_message`, which reads LangChain's `usage_metadata` (`input_tokens` / `prompt_tokens`) and falls back to the Anthropic-native `response_metadata.usage` block. `should_auto_compact_now` compares that value directly against the trigger. The `estimate_tokens()` helper in `agent_compaction.py` is only used by the context-overflow rewind fallback to size the prefix trim  -  never for the primary compaction trigger.

Per-thread `ThreadLLMConfig` may override `compact_threshold_mode`, `compact_threshold`, or `compact_threshold_tokens` independently  -  `None` on any field inherits the global setting.

Nymeria resolves bare OpenAI model IDs from CLIProxy (for example `gpt-5.5`) against provider-qualified metadata (`openai/gpt-5.5`) before falling back to static limits.

Compaction is a **retained-turn rebuild** (`_run_compact_turn_and_prune` / `_run_compact_turn_and_prune_sync`): it runs the real compaction turn, then discards that turn's messages and rebuilds the thread to a small resume tail. The agent's memory writes (side effects on the profile/notepad files) and its summary text are what survive; the read-back is injected deterministically by the framework, so it never depends on the agent choosing to call `memory_read`.

```
1. compact_now()  (or _do_auto_compact() / _do_compact_sync())
2.   _flush_memories_before_trim()  -  full pre-compact message list is indexed into
                                    the per-user RAG store (sqlite-vec + FTS5 at
                                    data/users/{uid}/memory.db). Defensive backup
                                    of the per-turn indexer; failures are logged
                                    and never block compaction.
3.   _generate_summary()           -  runs the real compaction turn: the agent
                                    persists durable facts -> global memory and
                                    working state (+ key file paths) -> the thread
                                    notepad, then emits the structured summary.
                                    Capped at 15 minutes; a timeout or an empty
                                    summary aborts WITHOUT pruning (the injected
                                    prompt + partial turn delta is removed).
4.   build_resume_compaction_tail() -  reads the POST-edit memory and builds the
                                    retained tail (agent_memory_seed.py):
                                      [0] HumanMessage internal_type='memory_seed_marker'
                                          -> "[Session resume] ... <summary> ..."
                                             (summary inline; also stamped into
                                             additional_kwargs for the UI notice)
                                      [1] AIMessage tool_calls=[memory_read global, thread]
                                      [2] ToolMessage  (authentic global memory)
                                      [3] ToolMessage  (authentic thread notepad)
                                    No trailing assistant message.
5.   aupdate_state(RemoveMessage(all) + tail)  -  the whole pre-rebuild state is
                                    removed and replaced by the 4-message tail.
6.   prune_checkpoints_before()    -  raw SQL DELETEs all pre-compact rows.
7.   reset_after_compact(retained_tail_tokens)  -  token tracking reseeded to the
                                    retained tail size, not 0.
```

There is no pending-summary stash and no restart-recovery step: the carried context is the retained tail itself, persisted in the checkpoint, so it survives process restarts natively.

**Resume ("hit play"):** because the tail ends on the `memory_read` ToolMessages (no trailing assistant message), re-invoking the graph with `{"messages": []}` re-enters the agent node and continues the task from the reloaded memory. The resume opener (a HumanMessage) is the turn boundary, so `max_iterations` resets cleanly across the seam. This resume is used by the sub-turn trigger and overflow recovery (mid-task). It is **not** used after a normal post-turn auto-compact: the turn already ended (the agent produced a final response = done), so the next user/autonomous turn simply continues from the retained tail. Manual `/compact` likewise does not re-drive.

The `memory_seed_marker` opener is projected by `/history` as a visible `system` message with `kind="compaction_notice"` so desktop/mobile can show "Context compacted" with a collapsible summary. (Legacy threads compacted before this redesign may still carry a single `compaction_marker` HumanMessage; the history projection handles both.)

The pre-compact RAG flush (step 2) means the conversation remains queryable via `rag_search` even after the in-context messages are cleared. See `tools.md` → `rag_search` for the full list of indexing hooks.

The summary prompt requires these exact sections:

- `## Active Goal`
- `## Progress`
- `## Pending Work` (records what the agent was mid-task on + the exact next step)
- `## Key Context`
- `## Files & Resources` (exact paths to re-read after compaction)
- `## RAG Search Queries`

The `RAG Search Queries` section should contain 3-5 quoted search strings that target important decisions, findings, file paths, and task state from the compacted thread. These are hints for the next agent turn to retrieve the full preserved conversation from RAG when the summary alone is not enough.

The prompt also frames the summary as an internal handoff, not a reply: the output is notes to the agent's future self, typically never shown to the user, and the model is explicitly told not to answer, greet, or address the user or respond to a still-pending question (open questions belong under `## Pending Work` for the resumed session to answer). Without this directive, a compaction firing while a user question was still open (common on the sub-turn path) tended to produce an answer to the user instead of the handoff, and that answer was then lost with the discarded compaction turn.

### Steering the summary (`/compact <focus instruction>`)

A manual `/compact` may carry an optional free-text focus instruction, for example `/compact keep the exact auth-flow decisions and the failing test names`. The text is normalized (trimmed, control-character stripped, `<<<`/`>>>` fence markers removed, capped at 1,000 chars; empty collapses to none) and, when present, inserts a one-line primer before the section list and appends a focus addendum after the base prompt.

The framing is deliberately "prioritize, not filter": the agent still produces every required section and still persists all durable facts to memory, the focus only changes emphasis and ordering, and the addendum explicitly lets the under-1500-word target yield rather than displace other content. So a focus never narrows the summary to just that focus.

It is wired only on the manual (async) path. `api/routers/chat.py` parses the trailing text from the raw (case-preserved) message and threads it through `agent.compact_now(..., priority=...)` to the single `_summary_input` chokepoint in `core/agent_compaction.py` (`_build_compact_prompt`). Auto-compaction and overflow recovery pass no priority, so the base `COMPACT_PROMPT` is byte-identical on those paths. The same `priority` is also accepted as a query param on `POST /threads/{id}/compact`, by the `nymeria_compact_thread` MCP tool, and by the Telegram/Discord `/compact` commands.

### Sub-turn trigger

Auto-compaction can fire **mid-turn**, not just at turn boundaries. The vendored router `route_after_tools` runs after each tool batch and before the next LLM call; there it asks `agent.should_halt_for_subturn_compaction(thread_id, messages)`, which reads the most recent AIMessage's provider-reported `input_tokens` (via `token_usage.extract_last_from_messages` -- `TokenTracker` is stale mid-loop) and compares against the per-thread trigger. If crossed, it flags the thread and returns `"end"` to halt the graph at that sub-turn boundary.

`astream()` / `chat()` then run the normal compaction (`_do_auto_compact` / `_do_compact_sync`) and **re-drive with `{"messages": []}`** so the agent continues from the reloaded memory. This is always a genuine mid-task boundary: the agent has a pending LLM call to process the tool results, so it is never "done" here (a final, no-tool-call response routes via `END`, where post-turn compaction handles it without a re-drive). The loop repeats if the continuation crosses the trigger again, capped by `MAX_COMPACTIONS_PER_TURN` (default 3); once the cap is hit, `should_halt_for_subturn_compaction` stops flagging so the continuation runs to completion. Per-turn state (`_subturn_compact_requested`, `_compactions_this_turn`) is cleared when the turn's lock releases.

Clients see a `compacting` event, then a `compacted` event carrying `subturn: true`, then the resumed assistant output, all within the same turn.

### Overflow rewind recovery

If a provider rejects a turn because the request is already over the context window, normal compaction may also be impossible: the summary call would see the same oversized state. Nymeria now treats context overflow as a recoverable `auto_compact` condition:

```
1. Detect context-overflow provider errors such as context_length_exceeded,
   maximum context length, too many tokens, input is too long, or prompt is too long.
2. Flush the full current oversized state to RAG before mutating checkpoints.
3. Walk up to 50 checkpoints back and select a checkpoint roughly four user turns earlier.
4. Fork the active thread state from that checkpoint and add an internal context_rewind marker.
5. Run the normal compact_now() / _do_compact_sync() flow on the shorter state.
6. If no suitable checkpoint exists, RemoveMessage trims the oldest prefix down
   to a conservative target, then compacts. The trim cuts ONLY on a user-turn
   (HumanMessage) boundary so the retained head stays a valid turn start: a
   ToolMessage head (orphaned tool_result) or first-message assistant turn is a
   provider 400, and the trim is committed to the durable checkpoint before the
   summary call runs. If the removable range has no such boundary (e.g. a single
   autonomous wake-up driving one long tool loop), recovery declines to trim and
   reports failure rather than persist an invalid head; the thread is left
   oversized but intact (a manual /prune can still shrink it).
```

For async `/chat` streaming, the client receives `compacting` after recovery reaches the compaction step, then `compacted` on success. After overflow recovery the thread holds the retained resume tail; the next user message continues from it.

### Checkpoint pruning and deletion

LangGraph has no public checkpoint-delete API, so `core/checkpoint_cleanup.py` centralizes raw SQL cleanup behind a small `CheckpointCleaner` interface. Compaction calls `prune_checkpoints_before()` from that module, and thread deletion calls `delete_thread_checkpoints()` so SQLite and Postgres deletion logic lives in one backend-specific implementation. Pruning runs *inside* `_run_compact_turn_and_prune`, *after* `_verify_retained` confirms the retained tail is present and ends cleanly, so a prune failure never blocks the compaction itself.

Three deletes, each try/except-wrapped:

```sql
DELETE FROM checkpoint_writes WHERE thread_id=? AND checkpoint_ns='' AND checkpoint_id < :boundary;
DELETE FROM checkpoints       WHERE thread_id=? AND checkpoint_ns='' AND checkpoint_id < :boundary;
-- For each (channel, version) referenced by the post-compact checkpoint:
DELETE FROM checkpoint_blobs  WHERE thread_id=? AND channel=? AND CAST(version AS INTEGER) < :floor;
```

`:boundary` = the post-compact checkpoint's id (UUIDv7, monotonically increasing).
`:floor` = the post-compact checkpoint's `channel_versions[channel]`.

Only the `messages` channel has blob rows in practice  -  `__start__`, `branch:to:agent`, `branch:to:tools` are stored inline in the checkpoint JSON.

**Why it's safe:**
- Runs while the caller holds the per-thread lock (`ThreadLockManager`)  -  no concurrent writes.
- `parent_checkpoint_id` links going dangling is harmless: Nymeria doesn't use time-travel or state forking.
- Current state (`get_state()`) reads only the latest checkpoint  -  unaffected.
- `get_state_history()` returns a shorter list  -  desired outcome.

---

## The `/threads/{id}/history` path

```
GET /threads/{id}/history
 └── api.py: get_thread_history()
     └── agent.get_conversation_history(thread_id, ...)
         ├── _default_graph.get_state(config)          -  loads the latest state (1 blob hydration)
         ├── _build_message_timestamp_map(graph, ...)  -  walks get_state_history(limit=200)
         │                                             for per-message ISO timestamps
         └── filter + turn consolidation               -  see "Display filter"
```

`_build_message_timestamp_map` is the expensive part: every item yielded by `get_state_history` hydrates `state.values`, which pulls that checkpoint's message blob from `checkpoint_blobs` via msgpack. Without pruning, a thread with thousands of pre-compact checkpoints deserialised tens of MB per `/history` call; with pruning, it walks ~10 rows.

Frontend calls `/history` on: thread switch, sync-poll every 5 s while a thread is active + not streaming, and after an autonomous task completes (`stores/autonomous.svelte.ts`).

---

## Display filter internal_types

`get_conversation_history()` filters out system-generated HumanMessages unless `include_internal=true`. Each is tagged with `additional_kwargs['internal_type']`:

| `internal_type`       | Origin                                 | Display behavior (default)                                                                 |
| --------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------ |
| `autonomous_wakeup`   | Ticker / watchdog / trigger wake-up    | Hide the prompt, **show the AI response** (user wants to see task output). `show_autonomous_prompts=True` reveals the prompt. |
| `compact_prompt`      | The compaction turn's injected prompt  | Hide prompt **and** AI response (internal housekeeping; also pruned from state after the rebuild). |
| `memory_init`         | Fresh-thread memory seed opener        | Hide the opener **and** the following `memory_read` AI/Tool read-back (LLM still sees it from state). |
| `memory_seed_marker`  | Post-compaction resume opener          | Show as a `system` `compaction_notice` with summary metadata; **suppress** the following `memory_read` read-back from the UI (still in LLM context). |
| `compaction_marker`   | Legacy single placeholder (pre-redesign threads) | Show as a `system` `compaction_notice` with summary metadata. |
| `auto_resume`         | Legacy auto-compact follow-up prompt   | Hide the prompt, **show the resumed assistant output**.                                     |

**The filter uses a `skip_until_next_human` flag** that stays active until a non-internal HumanMessage arrives. `memory_init` and `memory_seed_marker` use it to suppress their read-back exchange from the UI; the `autonomous_wakeup` hide-path explicitly resets it so a preceding internal prompt can't swallow the wakeup's response.

---

## Dashboard polling cost (2026-04-21 fix)

Three `/threads/*`-adjacent endpoints used to instantiate heavy stateful objects on every request  -  `ActivityLog`, `NotificationStore`, `TriggerManager` each did file I/O + directory setup per call. Under normal frontend polling (30 s intervals × several dashboard panels) this saturated the API event loop under load.

**Fixed by:**
- `api/routers/activity.py`: `/activity`, `/notifications`, `/notifications/{id}/read`, `/notifications/read-all` use `get_activity_log()` / `get_notification_store()` singletons.
- `triggers/trigger_api.py`: `TriggerManager` cached in the router closure.
- Frontend (desktop + mobile): poll intervals bumped (notifications 30 s→60 s, activity 30 s→45 s, triggers 30 s→60 s), and `visibilitychange` gating added so backgrounded tabs skip scheduled fetches and catch up on resume.

---

## Troubleshooting

### Symptom: thread shows blank "send a message to start" after `/compact`

**Check:** The LangGraph state may have messages but the display filter is suppressing them.

```bash
docker exec nymeria-api python -c "
from nymeria.core.agent import NymeriaAgent
a = NymeriaAgent()
st = a._default_graph.get_state({'configurable': {'thread_id': 'YOUR_THREAD_ID'}})
msgs = st.values.get('messages', [])
print(f'Raw state: {len(msgs)} messages')
for m in msgs[:5]:
    k = getattr(m, 'additional_kwargs', {}) or {}
    print(type(m).__name__, 'internal=', k.get('internal'), 'itype=', k.get('internal_type',''))
"
```

Then call `/history?include_internal=true`  -  if that returns messages but `/history` (default) doesn't, the display filter is the culprit. Likely causes:

- A new `internal_type` was introduced without adding an explicit branch to the filter at `core/agent.py:3925`. Anything unrecognised falls into the catch-all that sets `skip_until_next_human=True`, dropping every message until a non-internal HumanMessage arrives.
- Two consecutive internals where the second didn't reset `skip_until_next_human`.
- The frontend is older than the `compaction_notice` history shape. Current desktop/mobile render the marker; if no marker is present but context stats show prior compaction, they fall back to a "Context compacted" empty state instead of "Start a conversation".

**Fix pattern:** add an explicit `elif internal_type == 'your_new_type':` branch to the filter that handles whether following responses should be suppressed.

### Symptom: `/history` takes 10–180 s; frontend sits on "Loading…"

**Check:** Checkpoint count for the thread.

```bash
docker exec nymeria-postgres psql -U nymeria -d nymeria -c \
  "SELECT COUNT(*) FROM checkpoints WHERE thread_id='YOUR_THREAD_ID';"
```

A healthy compacted thread sits in the low tens. If you see hundreds or thousands, either:
- The thread was never compacted  -  trigger `/compact` and let pruning run automatically.
- A prior compaction ran before the pruning change was deployed  -  run the one-shot cleanup below.

### Inspect persisted messages

`tools/inspect_thread.py` reads the same checkpoint backend as the runtime. It
honors `DATABASE_BACKEND`, `SQLITE_PATH`/`NYMERIA_DATA_DIR`, and `POSTGRES_URI`,
then uses LangGraph's checkpointer serializer to print the latest persisted
messages:

```bash
cd Nymeria
python tools/inspect_thread.py --list
python tools/inspect_thread.py YOUR_THREAD_ID --last 20
python tools/inspect_thread.py YOUR_THREAD_ID --full
```

Use `--backend`, `--sqlite-path`, or `--postgres-uri` when inspecting a database
other than the one selected by the current environment. Local PostgreSQL
inspection requires the Postgres checkpoint extras from
`requirements-postgres.txt`; Docker installs them through the
`requirements-docker.txt` include chain.

### One-shot cleanup for a single bloated thread

Run inside the API container:

```bash
docker exec nymeria-api python -c "
from nymeria.core.agent import NymeriaAgent
from nymeria.core.checkpoint_cleanup import prune_checkpoints_before

a = NymeriaAgent()
tid = 'YOUR_THREAD_ID'
st = a._default_graph.get_state({'configurable': {'thread_id': tid}})
cp_id = st.config['configurable']['checkpoint_id']
cp = a._default_graph.checkpointer.get_tuple(
    {'configurable': {'thread_id': tid, 'checkpoint_id': cp_id}}
)
floor = cp.checkpoint.get('channel_versions', {}) if cp else {}
print(prune_checkpoints_before(tid, cp_id, floor))
"
```

Prints `(checkpoints_deleted, writes_deleted, blobs_deleted)`. Verify afterward:

```bash
docker exec nymeria-postgres psql -U nymeria -d nymeria -c \
  "SELECT COUNT(*) FROM checkpoints WHERE thread_id='YOUR_THREAD_ID';"
```

### Symptom: `/compact` returns `success=true` but the thread looks unchanged

**Check:** The pre-flight verify may have failed silently.

```bash
docker logs nymeria-api 2>&1 | grep -E "Thread YOUR_THREAD_ID.*(Cleared|Pruned|clear verification failed)"
```

Look for:
- `Cleared N messages via RemoveMessage (1 compaction marker remains)`  -  `_clear_and_reset` succeeded.
- `Pruned pre-compact history  -  N checkpoints, N writes, N blobs`  -  pruning succeeded.
- `Sync clear verification failed  -  X messages remain (expected 1 marker)`  -  the RemoveMessage write didn't take. This is the bail-out path; token tracker is not reset and prune is skipped. Investigate LangGraph / Postgres connectivity.

### Symptom: prune call logs a warning

The helper logs `Thread X: prune <table> failed: <error>` on per-table failures and `Thread X: Checkpoint prune (postgres|sqlite) failed: <error>` on connection-level failures. Common causes:

- Stale DB connection after Postgres restart  -  transient, next compaction recovers.
- Wrong `settings.database_backend` value  -  check `.env.docker` has `DATABASE_BACKEND=postgres` (Docker) or unset/`sqlite` (local dev).
- Postgres permissions  -  the role must be able to `DELETE` on the three checkpoint tables. If you changed roles, re-grant.

The compaction itself still succeeds when prune fails  -  the thread's active state is correct, just the historical bloat persists. Safe to retry with another compact later or the one-shot cleanup above.

### Symptom: API CPU sits at 80%+ idle

Most likely the dashboard polling anti-pattern has regressed. Check that `/activity`, `/notifications`, `/triggers` routes are using singletons:

```bash
grep -nE "NotificationStore\(|ActivityLog\(|TriggerManager\(" \
  Nymeria/nymeria/api/routers/activity.py \
  Nymeria/nymeria/triggers/trigger_api.py
```

Only `core/` files should show activity/notification constructor calls. If you see them in `api/routers/activity.py` route handlers, a refactor brought back the per-request pattern.

---

## Known edge cases

- **Threads that have never been compacted**  -  they still pay the full `get_state_history` walk cost on `/history`, because there are no pre-compact checkpoints to prune. The default trigger is 80% of the model context window. A thread with very low volume over a long period can still accumulate many checkpoints without ever hitting the token threshold; if this becomes a problem, the next lever is caching `_build_message_timestamp_map` output keyed on `(thread_id, latest_checkpoint_id)` and invalidating on write.

- **Time travel and state forking are NOT supported.** The pruning relies on this: it deletes `checkpoint_id < boundary` outright, so LangGraph's `update_state(config, ..., as_node=...)` with an older `checkpoint_id` would fail to find the parent. Nymeria doesn't use this feature.

- **Multiple checkpoint_ns values**  -  LangGraph supports multiple namespaces per thread; Nymeria only uses `''`. The prune SQL scopes to `checkpoint_ns = ''` explicitly to avoid touching any future subgraph checkpoints.

- **Auto-compact firing during an async `/chat` stream**  -  if prior token usage already crossed the trigger, pre-flight compaction runs before the new user message is sent to the model and the summary is attached to that message. If usage crosses the trigger after a graph invocation finishes, the stream emits `compacting` only after the compaction path has passed its start checks, persists the `compaction_notice`, emits `compacted` with the full summary, then streams the internal auto-resume turn's normal `thinking`/`tool_call`/`tool_result`/`response` events.

- **Cross-thread contamination**  -  not possible; all SQL is scoped by `thread_id`.

---

## `/prune`  -  deterministic tool-result compression

`/prune` is a lightweight sibling of `/compact` that **only rewrites tool results**. It does not call an LLM, does not produce a summary, and does not erase the conversational narrative. Every `HumanMessage` and `AIMessage` (including each AIMessage's `tool_calls` block) stays intact; only the `ToolMessage.content` of large tool returns is replaced with a placeholder.

Use `/prune` when the conversation flow is still useful but the tool returns themselves (RAG dumps, web fetches, file listings, gmail searches) dominate the input token count. Use `/compact` when the whole conversation should be summarised.

### Flow

```
1. graph.aget_state(config)               -  read current message list
2. For each ToolMessage:
   - Skip if additional_kwargs.internal_type == 'pruned_tool_result' (idempotent)
   - Skip if len(content) <= 200 chars (compression wouldn't save anything)
   - Build marker: "[/prune placeholder - original tool result removed
                    (<N> chars, success|error). Call this tool again to get the
                    real result.]"
   - Detect status from msg.status == 'error' OR content.startswith('[Error]')
   - model_copy({content: marker, additional_kwargs: {..., internal_type:
                                                     'pruned_tool_result', ...}})
3. graph.aupdate_state(config, {"messages": replacements})
   - add_messages reducer replaces in-place by id
   - id, tool_call_id, and name are preserved → AIMessage linkage stays valid
```

### Key properties

- **No LLM**: the entire operation is a single state read + state update. Sub-second.
- **No data destruction**: the per-turn conversation indexer (`rag_search`) has already indexed each tool result into the per-user RAG store, so the agent can recover specific content via search even after pruning.
- **No token tracker reset**: unlike `/compact`, `/prune` does not call `reset_after_compact`. The next turn's real `input_tokens` reported by the provider will naturally overwrite the stale `last_input_tokens` cache.
- **No checkpoint pruning**: `/prune` does not delete historical checkpoint rows. It only mutates the latest state. Older `checkpoint_blobs` still contain the original tool results.
- **Idempotent**: running `/prune` twice on the same thread yields `pruned_count=0` on the second call.
- **Agent-blocked**: `agent_allowed=False` (matches `/compact`)  -  prevents the agent from pruning its own in-flight tool results mid-turn.

### Marker contract

The placeholder text is intentionally self-explanatory so a future LLM call can read it and know the result was pruned, not a real return value:

```
[/prune placeholder - original tool result removed (4523 chars, success). Call this tool again to get the real result.]
```

The deterministic machine-readable signal is `additional_kwargs.internal_type == 'pruned_tool_result'`, also carrying `original_chars`, `original_status`, and `pruned_at` ISO timestamp. Display layers can detect this and render a distinct visual treatment if desired.

### Return shape

```json
{
  "success": true,
  "pruned_count": 12,
  "skipped_already_pruned": 0,
  "skipped_too_short": 3,
  "chars_before": 38420,
  "chars_after": 1320,
  "chars_saved": 37100
}
```

On failure (state read or write error): `{"success": false, "reason": "..."}`.

### Entry points

- Slash command from any frontend: `/prune` (registered with `execution_kind="command"`)
- REST: `POST /threads/{id}/prune`
- MCP: `nymeria_prune_thread(thread_id, user_id)`
- Triggers HTTP client (bots): `api_client.prune(thread_id, user_id)`
- Python: `agent.prune_now(thread_id, user_id)` → `PruneManager.prune_now`

---

## Relevant files

| Path | Purpose |
|------|---------|
| `core/agent_compaction.py` | `CompactionManager`  -  owns compaction policy and execution (`_run_compact_turn_and_prune` retained-turn rebuild) |
| `core/agent_memory_seed.py` | `build_resume_compaction_tail` / `build_memory_exchange`  -  the resume opener + authentic memory read-back (shared with the fresh-thread seed) |
| `core/agent_prune.py` | `PruneManager`  -  owns `/prune` execution (deterministic tool-result compression) |
| `core/checkpoint_cleanup.py` | `CheckpointCleaner`, `prune_checkpoints_before`, and `delete_thread_checkpoints`  -  raw SQL cleanup, per-backend (SQLite + Postgres) |
| `core/agent.py` | `NymeriaAgent` delegates to `self._compaction` (CompactionManager) and `self._prune` (PruneManager) |
| `core/agent.py` | `_build_message_timestamp_map`  -  checkpoint walker for timestamps |
| `core/agent.py` | display filter in `get_conversation_history`  -  internal_type branches |
| `core/command_service.py` | `/compact` (chat_stream) and `/prune` (command) registration; `_CommandExecutor._cmd_prune`; `CommandBackendClient.prune_thread` |
| `api/routers/thread_operations.py` | `POST /threads/{id}/compact` and `POST /threads/{id}/prune` endpoints |
| `triggers/api.py` | `/threads/{id}/history` endpoint |
| `core/checkpointer_config.py` | `enumerate_checkpoint_thread_ids`  -  canonical guarded raw-SQL "distinct checkpoint thread ids" read (thread-list route, in-process CLI transport, startup backfill) |
