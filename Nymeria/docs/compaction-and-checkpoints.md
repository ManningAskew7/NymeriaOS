# Compaction, Checkpointer, and Thread History

How Nymeria's conversation state is persisted, compacted, and displayed — and what to check when a thread behaves oddly.

---

## Big picture

A thread's conversation lives in three places:

1. **LangGraph state (`messages` channel)** — the live list the LLM sees on each turn. This is what `/compact` trims.
2. **LangGraph checkpoint history** — every state transition writes a new row to `checkpoints` and a new blob to `checkpoint_blobs`. These accumulate *forever* unless explicitly pruned.
3. **Display filter** — `get_conversation_history()` in `core/agent.py` transforms the raw message list into what the frontend actually renders (hides internal messages, consolidates tool calls into turns).

`/compact` only operates on (1). Until recently, (2) accumulated forever — and (3) has a few sharp edges around internal message types.

---

## Compaction flow

Triggered by `/compact`, `POST /threads/{id}/compact`, or automatically when token usage crosses the configured trigger. The trigger has two modes (`COMPACT_THRESHOLD_MODE`):

- **`percentage`** (default): fires when input tokens reach `COMPACT_THRESHOLD * context_limit`. `COMPACT_THRESHOLD` accepts `0.05` through `0.95`.
- **`tokens`**: fires when input tokens reach the absolute count `COMPACT_THRESHOLD_TOKENS` (1,000–2,000,000), clamped at runtime to the model's context window so an oversized setting never disables compaction.

The check uses **real provider-reported input tokens**, not character estimates. `core/token_tracker.py` records `last_input_tokens` from each AIMessage via `core/token_usage.py::extract_from_message`, which reads LangChain's `usage_metadata` (`input_tokens` / `prompt_tokens`) and falls back to the Anthropic-native `response_metadata.usage` block. `should_auto_compact_now` compares that value directly against the trigger. The `estimate_tokens()` helper in `agent_compaction.py` is only used by the context-overflow rewind fallback to size the prefix trim — never for the primary compaction trigger.

Per-thread `ThreadLLMConfig` may override `compact_threshold_mode`, `compact_threshold`, or `compact_threshold_tokens` independently — `None` on any field inherits the global setting.

Nymeria resolves bare OpenAI model IDs from CLIProxy (for example `gpt-5.5`) against provider-qualified metadata (`openai/gpt-5.5`) before falling back to static limits.

```
1. compact_now()  (or _do_auto_compact() / _do_compact_sync())
2.   _pre_trim_memory_flush()     — full pre-compact message list is indexed into
                                    the per-user RAG store (sqlite-vec + FTS5 at
                                    data/users/{uid}/memory.db). Defensive backup
                                    of the per-turn indexer; failures are logged
                                    and never block compaction.
3.   _generate_summary()          — LLM produces a structured summary of the
                                    untouched conversation, including 3-5 quoted
                                    RAG search queries for the resuming agent.
                                    Summary generation is capped at 15 minutes;
                                    timeout returns a failed compaction result
                                    without clearing messages.
4.   _clear_and_reset()           — RemoveMessage commands wipe all messages from state,
                                    then one HumanMessage with internal_type='compaction_marker'
                                    is written as a single-message placeholder with
                                    summary/messages_removed/auto_resumed/timestamp metadata
5.   prune_checkpoints_before()   — raw SQL DELETEs all pre-compact rows
6.   Manual/sync/pre-flight compact: _pending_summaries[thread_id] = summary
     Post-turn async auto-compact: stream compacted, then stream the resume turn immediately
7.   Restart recovery: if _pending_summaries is lost (process restart between
     steps 6 and the next user message), get_pending_summary() reads the
     compaction_marker from the checkpoint and recovers the summary from
     additional_kwargs["summary"]. Notepad is re-read from disk.
```

The compaction_marker exists because LangGraph's router accesses `messages[-1]` — an empty list would `IndexError`. It is also projected by `/history` as a visible `system` message with `kind="compaction_notice"` so desktop/mobile can show "Context compacted" with a collapsible summary. It also serves as the durable recovery source for pending summaries lost to process restart (see step 7).

The pre-compact RAG flush (step 2) means the conversation remains queryable via `rag_search` even after the in-context messages are cleared. See `tools.md` → `rag_search` for the full list of indexing hooks.

The summary prompt requires these exact sections:

- `## Active Goal`
- `## Progress`
- `## Pending Work`
- `## Key Context`
- `## Files & Resources`
- `## RAG Search Queries`
- `## Persistent Memory`

The `RAG Search Queries` section should contain 3-5 quoted search strings that target important decisions, findings, file paths, and task state from the compacted thread. These are hints for the next agent turn to retrieve the full preserved conversation from RAG when the summary alone is not enough.

### Overflow rewind recovery

If a provider rejects a turn because the request is already over the context window, normal compaction may also be impossible: the summary call would see the same oversized state. Nymeria now treats context overflow as a recoverable `auto_compact` condition:

```
1. Detect context-overflow provider errors such as context_length_exceeded,
   maximum context length, too many tokens, input is too long, or prompt is too long.
2. Flush the full current oversized state to RAG before mutating checkpoints.
3. Walk up to 50 checkpoints back and select a checkpoint roughly four user turns earlier.
4. Fork the active thread state from that checkpoint and add an internal context_rewind marker.
5. Run the normal compact_now() / _do_compact_sync() flow on the shorter state.
6. If no suitable checkpoint exists, RemoveMessage trims the oldest prefix until the state is below a conservative target, then compacts.
```

For async `/chat` streaming, the client receives `compacting` with `Context too large — rewinding and compacting...`, then `compacted` on success. For sync `chat()` callers, recovery stores the compacted summary as pending and returns a short instruction to send the message again; the next prompt resumes from the compacted state.

### Checkpoint pruning and deletion

LangGraph has no public checkpoint-delete API, so `core/checkpoint_cleanup.py` centralizes raw SQL cleanup behind a small `CheckpointCleaner` interface. Compaction calls `prune_checkpoints_before()` from that module, and thread deletion calls `delete_thread_checkpoints()` so SQLite and Postgres deletion logic lives in one backend-specific implementation. Pruning runs *inside* `_clear_and_reset`, *after* `verify_state` confirms exactly 1 message remains, so a prune failure never blocks the compaction itself.

Three deletes, each try/except-wrapped:

```sql
DELETE FROM checkpoint_writes WHERE thread_id=? AND checkpoint_ns='' AND checkpoint_id < :boundary;
DELETE FROM checkpoints       WHERE thread_id=? AND checkpoint_ns='' AND checkpoint_id < :boundary;
-- For each (channel, version) referenced by the post-compact checkpoint:
DELETE FROM checkpoint_blobs  WHERE thread_id=? AND channel=? AND CAST(version AS INTEGER) < :floor;
```

`:boundary` = the post-compact checkpoint's id (UUIDv7, monotonically increasing).  
`:floor` = the post-compact checkpoint's `channel_versions[channel]`.

Only the `messages` channel has blob rows in practice — `__start__`, `branch:to:agent`, `branch:to:tools` are stored inline in the checkpoint JSON.

**Why it's safe:**
- Runs while the caller holds the per-thread lock (`ThreadLockManager`) — no concurrent writes.
- `parent_checkpoint_id` links going dangling is harmless: Nymeria doesn't use time-travel or state forking.
- Current state (`get_state()`) reads only the latest checkpoint — unaffected.
- `get_state_history()` returns a shorter list — desired outcome.

---

## The `/threads/{id}/history` path

```
GET /threads/{id}/history
 └── api.py: get_thread_history()
     └── agent.get_conversation_history(thread_id, ...)
         ├── _default_graph.get_state(config)         — loads the latest state (1 blob hydration)
         ├── _build_message_timestamp_map(graph, ...) — walks get_state_history(limit=200)
         │                                             for per-message ISO timestamps
         └── filter + turn consolidation              — see "Display filter"
```

`_build_message_timestamp_map` is the expensive part: every item yielded by `get_state_history` hydrates `state.values`, which pulls that checkpoint's message blob from `checkpoint_blobs` via msgpack. Without pruning, a thread with thousands of pre-compact checkpoints deserialised tens of MB per `/history` call; with pruning, it walks ~10 rows.

Frontend calls `/history` on: thread switch, sync-poll every 5 s while a thread is active + not streaming, and after an autonomous task completes (`stores/autonomous.svelte.ts`).

---

## Display filter internal_types

`get_conversation_history()` filters out system-generated HumanMessages unless `include_internal=true`. Each is tagged with `additional_kwargs['internal_type']`:

| `internal_type`       | Origin                                 | Display behavior (default)                                                                 |
| --------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------ |
| `autonomous_wakeup`   | Ticker / watchdog / trigger wake-up    | Hide the prompt, **show the AI response** (user wants to see task output). `show_autonomous_prompts=True` reveals the prompt. |
| `compact_prompt`      | Pre-flight compact ("summarise this…") | Hide prompt **and** AI response (internal housekeeping).                                   |
| `auto_resume`         | Auto-compact follow-up prompt          | Hide the prompt, **show the resumed assistant output**.                                     |
| `compaction_marker`   | Single placeholder after `_clear_and_reset` | Show as a `system` `compaction_notice` with summary metadata, **don't suppress anything after**. |

**The filter uses a `skip_until_next_human` flag** that stays active until a non-internal HumanMessage arrives. The `autonomous_wakeup` hide-path now explicitly resets this flag — otherwise a preceding `compact_prompt` / `auto_resume` / (historically) `compaction_marker` would swallow the wakeup's response.

---

## Dashboard polling cost (2026-04-21 fix)

Three `/threads/*`-adjacent endpoints used to instantiate heavy stateful objects on every request — `ActivityLog`, `NotificationStore`, `TriggerManager` each did file I/O + directory setup per call. Under normal frontend polling (30 s intervals × several dashboard panels) this saturated the API event loop on the 1-vCPU VPS.

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

Then call `/history?include_internal=true` — if that returns messages but `/history` (default) doesn't, the display filter is the culprit. Likely causes:

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
- The thread was never compacted — trigger `/compact` and let pruning run automatically.
- A prior compaction ran before the pruning change was deployed — run the one-shot cleanup below.

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
- `Cleared N messages via RemoveMessage (1 compaction marker remains)` — `_clear_and_reset` succeeded.
- `Pruned pre-compact history — N checkpoints, N writes, N blobs` — pruning succeeded.
- `Sync clear verification failed — X messages remain (expected 1 marker)` — the RemoveMessage write didn't take. This is the bail-out path; token tracker is not reset and prune is skipped. Investigate LangGraph / Postgres connectivity.

### Symptom: prune call logs a warning

The helper logs `Thread X: prune <table> failed: <error>` on per-table failures and `Thread X: Checkpoint prune (postgres|sqlite) failed: <error>` on connection-level failures. Common causes:

- Stale DB connection after Postgres restart — transient, next compaction recovers.
- Wrong `settings.database_backend` value — check `.env.docker` has `DATABASE_BACKEND=postgres` (Docker) or unset/`sqlite` (local dev).
- Postgres permissions — the role must be able to `DELETE` on the three checkpoint tables. If you changed roles, re-grant.

The compaction itself still succeeds when prune fails — the thread's active state is correct, just the historical bloat persists. Safe to retry with another compact later or the one-shot cleanup above.

### Symptom: API CPU sits at 80%+ idle

Most likely the dashboard polling anti-pattern has regressed. Check that `/activity`, `/notifications`, `/triggers` routes are using singletons:

```bash
grep -nE "NotificationStore\(|ActivityLog\(|TriggerManager\(" \
  /opt/NymeriaOS/Nymeria/nymeria/api/routers/activity.py \
  /opt/NymeriaOS/Nymeria/nymeria/triggers/trigger_api.py
```

Only `core/` files should show activity/notification constructor calls. If you see them in `api/routers/activity.py` route handlers, a refactor brought back the per-request pattern.

---

## Known edge cases

- **Threads that have never been compacted** — they still pay the full `get_state_history` walk cost on `/history`, because there are no pre-compact checkpoints to prune. The default trigger is 80% of the model context window. A thread with very low volume over a long period can still accumulate many checkpoints without ever hitting the token threshold; if this becomes a problem, the next lever is caching `_build_message_timestamp_map` output keyed on `(thread_id, latest_checkpoint_id)` and invalidating on write.

- **Time travel and state forking are NOT supported.** The pruning relies on this: it deletes `checkpoint_id < boundary` outright, so LangGraph's `update_state(config, ..., as_node=...)` with an older `checkpoint_id` would fail to find the parent. Nymeria doesn't use this feature.

- **Multiple checkpoint_ns values** — LangGraph supports multiple namespaces per thread; Nymeria only uses `''`. The prune SQL scopes to `checkpoint_ns = ''` explicitly to avoid touching any future subgraph checkpoints.

- **Auto-compact firing during an async `/chat` stream** — if prior token usage already crossed the trigger, pre-flight compaction runs before the new user message is sent to the model and the summary is attached to that message. If usage crosses the trigger after a graph invocation finishes, the stream emits `compacting`, persists the `compaction_notice`, emits `compacted` with the full summary, then streams the internal auto-resume turn's normal `thinking`/`tool_call`/`tool_result`/`response` events.

- **Cross-thread contamination** — not possible; all SQL is scoped by `thread_id`.

---

## Relevant files

| Path | Purpose |
|------|---------|
| `core/agent_compaction.py` | `CompactionManager` — owns compaction policy, execution, and pending state |
| `core/checkpoint_cleanup.py` | `CheckpointCleaner`, `prune_checkpoints_before`, and `delete_thread_checkpoints` — raw SQL cleanup, per-backend (SQLite + Postgres) |
| `core/agent_compaction.py` | `create_compaction_marker` — durable history marker |
| `core/agent.py` | `NymeriaAgent` delegates to `self._compaction` (CompactionManager) |
| `core/agent.py` | `_build_message_timestamp_map` — checkpoint walker for timestamps |
| `core/agent.py` | display filter in `get_conversation_history` — internal_type branches |
| `triggers/api.py` | `/threads/{id}/history` endpoint |
| `triggers/api.py` | `_get_checkpoint_thread_ids` — reference pattern for raw SQL checkpoint reads |
