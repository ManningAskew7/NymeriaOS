# User TODO Management with Recurring Schedules

## Overview

This feature allows users to create, edit, complete, and delete TODOs for Nymeria through the desktop UI, with support for recurring schedules. The LLM agent can also manage TODOs via tools.

**Key Features:**
- Full CRUD operations for TODOs via REST API
- Arbitrary recurrence intervals as duration strings (`5m`, `2h`, `1d`, `1w`, `1mo`, etc.); minimum 60s. Calendar months (`Nmo`) use calendar arithmetic so monthly TODOs don't drift; everything else is a fixed duration. Legacy preset names (`hourly`, `daily`, `weekly`, `monthly`, `5min`/`10min`/`15min`/`30min`) are still accepted on input and normalised to canonical form on storage (note: `monthly` now resolves to `1mo`, not `30d`).
- Visual distinction between user-created vs agent-created TODOs
- Visual distinction between recurring vs one-time TODOs
- Thread selection and per-thread task counts for scheduled TODO output

---

## Architecture

### Data Flow

```
┌─────────────────────┐
│   Desktop UI        │
│   (Svelte/Tauri)    │
└──────────┬──────────┘
           │ REST API
           ▼
┌─────────────────────┐      ┌─────────────────────┐
│   API Endpoints     │─────▶│   TodoManager       │
│   (FastAPI)         │      │   (JSON files)      │
└──────────┬──────────┘      └──────────┬──────────┘
           │                            │
           ▼                            ▼
┌─────────────────────┐      ┌─────────────────────┐
│   TodoScheduleDB    │◀────▶│   TODO JSON files   │
│   (SQLite index)    │      │   data/todos/*.json │
└──────────┬──────────┘      └─────────────────────┘
           │
           ▼
┌─────────────────────┐
│   Ticker            │
│   (5s polling)      │
│   Executes due      │
│   TODOs via agent   │
└─────────────────────┘
```

### Key Components

| Component | File | Purpose |
|-----------|------|---------|
| `TodoItem` model | `nymeria/core/todo_manager.py` | Pydantic model with `created_by`, `recurrence` fields |
| CRUD endpoints | `nymeria/api/routers/todos.py` | REST API for create/update/delete/complete |
| Schedule DB | `nymeria/core/todo_schedule_db.py` | SQLite index for efficient polling |
| Ticker | `nymeria/core/ticker.py` | Polls and executes due TODOs |
| Frontend form | `nymeria-desktop/src/lib/components/todos/TodoForm.svelte` | Create/edit modal |
| Frontend item | `nymeria-desktop/src/lib/components/todos/TodoItem.svelte` | Display with badges |

---

## Backend Implementation

### TodoItem Model Extensions

**File:** `nymeria/core/todo_manager.py`

```python
class TodoItem(BaseModel):
    # ... existing fields ...

    # User management & recurrence fields
    created_by: str = Field(default="agent", description="Who created: 'agent' or 'user'")
    recurrence: Optional[str] = Field(default=None, description="Canonical duration string (e.g. '5m', '2h', '1d', '1w', '1mo'). Calendar months (Nmo) use calendar arithmetic; everything else is a fixed duration. Validated by core.todo_constants.validate_recurrence; legacy preset names are accepted on input.")

    # Scheduled workflow TODOs (create-only; delete and recreate to rebind)
    workflow_id: Optional[str] = None      # published workflow tool to run headlessly
    workflow_params: Optional[dict] = None # parameters bound to the run
```

A TODO with `workflow_id` set runs that published nym-SDK workflow tool at
the scheduled time instead of waking the agent: no prompt, no LLM turn. The
binding is validated at create time on every surface (`nym_todo` tool and
`POST /todos`): the workflow must exist, its revision must be
admin-approved, and every required parameter must be covered by
`workflow_params` or defaults. Both fields are create-only; updates that try
to change them are refused. The run never delivers output by itself
(delivery is the workflow's job via `nym.thread`/`nym.notify`); the owner
gets the usual scheduled-task status notification. Recurrence and retry
behave exactly like agent TODOs, and since no agent turn exists to close
the item, the ticker marks a successful non-recurring workflow TODO done
itself. A run that suspends on `nym.approve` counts as a successful
execution.

### REST API Endpoints

**File:** `nymeria/api/routers/todos.py`

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/todos` | GET | List TODOs for user |
| `/todos` | POST | Create new TODO |
| `/todos/{todo_id}` | PATCH | Update TODO |
| `/todos/{todo_id}` | DELETE | Delete TODO |
| `/todos/{todo_id}/complete` | POST | Mark as done |

### Request Models

```python
class TodoCreateRequest(BaseModel):
    task: str
    notes: Optional[str]
    scheduled_for: Optional[str] # "45s", "17m", "2h", "1w", absolute, or ISO datetime
    recurrence: Optional[str]    # Duration string: "5m", "2h", "1d", "1w", "1mo" (min 60s). Calendar months use calendar arithmetic. Legacy names "hourly", "daily", "weekly", "monthly", "5min" ... "30min" still accepted.
    thread_id: Optional[str]     # Thread for scheduled execution output

class TodoUpdateRequest(BaseModel):
    task: Optional[str]
    status: Optional[str]        # pending, in_progress, done
    notes: Optional[str]
    scheduled_for: Optional[str]
    recurrence: Optional[str]
    thread_id: Optional[str]
    clear_schedule: bool = False
    clear_recurrence: bool = False
```

### Timezone Handling

**Critical Implementation Detail:**

The `_parse_scheduled_for` helper handles timezone conversion:

```python
def _parse_scheduled_for(scheduled_for: Optional[str]) -> Optional[datetime]:
    # Relative times: any positive duration like "45s", "17m", "2h", "1d", "1w"
    # Uses timezone-aware UTC: datetime.now(timezone.utc) + timedelta(...)

    # Absolute times: "2026-01-15T14:37" or ISO with timezone
    # Naive absolute times are interpreted in the configured user timezone
```

The `_datetime_to_timestamp` function in `todo_schedule_db.py` handles naive datetimes:

```python
def _datetime_to_timestamp(dt: datetime) -> float:
    if dt.tzinfo is None:
        # Naive datetime - assume it's UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()
```

### Recurring TODO Execution

**File:** `nymeria/core/ticker.py`

In Docker, the `worker` container drives `Ticker` but no longer constructs
a `NymeriaAgent`  -  it relays each due TODO to the `api` container via
`POST /chat` with `publish_autonomous_events=False`. The worker stays the
sole publisher of `task_started` / `task_completed` and per-chunk events
for that TODO, using `todo.id` as the stable task id. Slim runs the ticker
in-process against the local agent (no change in behaviour). The injection
point is the `TurnExecutor` passed to `Ticker.__init__`  -  `LocalAgentExecutor`
in slim, `APIClientExecutor` in the Docker worker. See
`nymeria/core/turn_executor.py`. Workflow TODOs ride the same seam through
its `run_workflow` method: slim runs the workflow engine in-process, the
Docker worker relays to `POST /workflows/{id}/execute` on the API container.

### Startup missed-work handling

The ticker rebuilds `todo_schedule.db` from TODO JSON files before its poll
thread starts. This makes scheduled TODO recovery independent of whether the
SQLite schedule index was current at shutdown.

By default, missed scheduled TODOs run automatically on startup. Local
desktop-managed slim launches can set `--missed-work-policy ask` so TODOs
that were already overdue at startup are held in `data/scheduler_state.json`
until an admin calls `POST /scheduler/missed-work/run`. While ask-mode missed
work is pending, the first poll-based trigger catch-up pass is also paused.
Future TODOs that become due after startup continue to run normally.

`GET /scheduler/status` reports the missed-work policy, pending missed TODOs,
trigger catch-up pause, last startup and clean-shutdown timestamps, and active
scheduled-TODO execution marker count.

If the process stops while a scheduled TODO is executing, its
`active_todo_executions` marker is left behind. Because exactly one ticker
owns `todo_schedule.db` (slim: the API-agent; Docker: the worker, with the
API-agent's ticker disabled), any marker present at startup is by definition
orphaned  -  an in-flight execution runs on a daemon thread that cannot survive
the process boundary. `prepare_startup_recovery` therefore clears **all**
execution markers before the poll thread starts, so a TODO interrupted moments
before a restart re-fires immediately instead of being held for up to a day.
The 24-hour stale window (`--active-execution-stale-minutes`, default 1440)
still guards the *live* in-flight path (`mark_execution_started` /
`is_execution_active`) against a wedged execution thread inside a still-running
process.

When a scheduled TODO succeeds:

1. If `recurrence` is set, calculate next execution time
2. Update `scheduled_for` to next time
3. Reset status to `pending`
4. Re-sync to TodoScheduleDB

If a scheduled run fails, it is retried on subsequent polls up to
`MAX_RETRIES` (3). On give-up, a **recurring** TODO skips only the failed
occurrence and re-arms at its next slot (so a transient outage cannot silently
kill the schedule by leaving `recurrence` set with a null `scheduled_for`); a
**one-time** TODO has its schedule cleared and is left `pending` with a failure
note.

Recurring failures also feed an escalation policy so a permanently broken
schedule cannot loop silently forever. Each exhausted occurrence increments
`consecutive_failures` on the TODO (with `last_failure`/`last_failure_at`);
any success resets it. At `SCHEDULER_FAILURE_ALERT_AFTER` consecutive
failures (default 2) the owner gets one alert, in-app plus their external
notification destinations. At `SCHEDULER_FAILURE_PAUSE_AFTER` (default 5)
the schedule auto-pauses instead of re-arming: `scheduled_for` clears,
`recurrence` is KEPT, `schedule_paused_at` marks the pause, the pause
reason is prepended to the notes (the original notes survive; the prefix
is stripped again on resume), and a second alert explains how to resume.
Explicitly rescheduling a paused TODO (command, API, or tool) clears the
pause and the streak and re-arms it; marking a paused TODO done completes
it WITHOUT silently resuming the schedule. The failure state is visible on
the wire (`GET /todos`) and in the todo tool and `/todos list` renderings.
Either threshold set to 0 disables that stage.

DELIVERY failures have a parallel streak with the same policy
(`delivery_failures` / `last_delivery_failure` / `last_delivery_failure_at`,
`core/delivery_accounting.py`): a turn can succeed backend-side while the
chat bot bound to its thread cannot send the output (wrong binding, peer
never started the bot). Bots report each TODO turn's outcome to
`POST /todos/{todo_id}/delivery-report`; consecutive `failed` reports
alert at the same alert threshold and auto-pause at the same pause
threshold (one-shot TODOs alert immediately), and a delivered report or
an explicit reschedule clears the episode. The two streaks are separate
fields because a successful execution resets `consecutive_failures`
before the bot has finished delivering.

The interval calculation lives in `core/todo_constants.py`:

```python
def parse_recurrence_interval(value: Optional[str]) -> Optional[timedelta | relativedelta]:
    """Accept canonical durations ("5m", "2h", "1d", "1w", "30s"), calendar
    months ("1mo", "3mo"), and legacy preset names ("hourly", "daily",
    "weekly", "monthly", "5min" ... "30min"). Returns a relativedelta for
    month intervals and a timedelta for everything else; None for
    unparseable input."""

def validate_recurrence(value: str) -> str:
    """Return the canonical duration string, raising ValueError if the
    interval is below MIN_RECURRENCE_SECONDS (60s) or malformed.
    Month intervals are exempt from the 60s floor."""

def calculate_next_recurrence_time(recurrence: str, anchor: datetime, *,
                                   now=None, origin=None):
    """Advance the anchor by parse_recurrence_interval(recurrence), skipping
    any intervals that have already passed. For calendar-month intervals with
    `origin` given, each slot is derived from the stable origin
    (origin + N months) rather than the previous (clamped) anchor, so a
    month-end day (29-31) clamps to short months WITHOUT drifting downward
    over successive fires. `origin` is ignored for fixed-duration intervals
    and when None (which keeps the anchor-relative behaviour)."""
```

Legacy preset names map to canonical durations via `LEGACY_RECURRENCE_ALIASES`:
`hourly → 1h`, `daily → 1d`, `weekly → 1w`, `monthly → 1mo`,
`5min → 5m` ... `30min → 30m`. The API, agent tool and command service all
call `validate_recurrence(...)` and persist the returned canonical string
(the CLI rides the command service since #143 retired its local family).
Existing TODO data stored as `30d` (the previous canonical for `monthly`)
keeps working as a 30-day fixed interval; only new TODOs and re-saved ones
pick up the calendar-month behaviour.

### Completed TODO Retention

Completed TODOs stay in the user's JSON list until ticker cleanup removes old
completed items. The cleanup runs about once per hour and uses
`TODO_AUTO_ARCHIVE_DAYS` from settings (default 7, valid range 1-30). The age is
measured from the TODO's `updated_at`, so marking an item done resets the
retention window.

"Archive" here means removal from `data/todos/{user_id}.json`; Nymeria does not
write completed TODOs to a separate archive file. Before cleanup, completed
items can be inspected with `GET /todos?filter_status=done`, `filter_status=all`,
or `nym_todo_list(filter_status="done")`. After cleanup, only secondary history
surfaces such as activity entries and indexed completed-TODO outcomes may retain
context, depending on their own retention/indexing settings.

---

## Frontend Implementation

### TypeScript Types

**File:** `nymeria-desktop/src/lib/types/index.ts`

```typescript
// Canonical duration string ("5m", "2h", "1d", "1w"). Legacy preset names
// arrive from older data and are accepted by the backend.
export type TodoRecurrence = string;
export type TodoCreatedBy = 'agent' | 'user';

export interface TodoItem {
    // ... existing fields ...
    createdBy: TodoCreatedBy;
    recurrence?: TodoRecurrence;
    threadId?: string;
}
```

### API Service

**File:** `nymeria-desktop/src/lib/services/api.svelte.ts`

```typescript
async createTodo(request: TodoCreateRequest): Promise<TodoItem>
async updateTodo(todoId: string, request: TodoUpdateRequest): Promise<TodoItem>
async deleteTodo(todoId: string): Promise<void>
async completeTodo(todoId: string): Promise<TodoItem>
```

### TodoForm Component

**File:** `nymeria-desktop/src/lib/components/todos/TodoForm.svelte`

Modal form with:
- Task input (required)
- Notes textarea
- Schedule datetime-local input
- Recurrence select (appears when schedule is set)
- Thread selector dropdown (appears when schedule is set)
- Delete button with confirmation (edit mode)

### Thread Selection

When a user creates a scheduled TODO, they can select which thread the execution output appears in:

1. **"+ New conversation"** - Creates a new thread via `threadsStore.createThread()`
2. **Existing thread** - Uses the selected thread ID

This ensures users can see and interact with scheduled TODO responses.

---

## Potential Failure Points

### 1. Database Lock Contention

**Risk:** Multiple simultaneous API requests could cause SQLite lock timeouts.

**Mitigation:**
- TodoScheduleDB uses `threading.Lock` for all operations
- SQLite timeout set to 30 seconds
- Consider connection pooling for high-traffic scenarios

### 2. Ticker-API Race Condition

**Risk:** Ticker executes TODO while user is editing it.

**Mitigation:**
- Ticker claims an `active_todo_executions` marker in `TodoScheduleDB` before reading the TODO body and clears it when the scheduled run exits.
- REST update, complete, and delete endpoints check that marker and return `409 Conflict` while the TODO is actively executing.
- All markers are cleared at startup (any marker outliving the single ticker's process is orphaned), and within a running process markers older than 24 hours are removed automatically, so neither a crash nor a mid-run restart can lock a TODO.

### 3. Thread Deletion

**Status:** Handled. Deleting a thread cascades through `core/thread_deletion.py`,
which reaps every TODO carrying that `thread_id` (items via
`TodoManager.delete_todos_for_thread`, schedule-index rows via
`TodoScheduleDB.remove_for_thread`) alongside triggers, hooks, RAG chunks,
checkpoints, and the owner row. All delete entry points go through the cascade
(`DELETE /threads/{id}`, the `/thread delete` command, and `spawn_thread`
cleanup), so a deleted thread has no TODOs left. If a delete races an
already-due entry, the ticker's item-not-found guard in `_execute_scheduled_todo`
skips the run and removes the stale schedule row instead of firing.

**Residual (thin-client "ghost" thread):** A client can drop a thread from its
local list without calling `DELETE`. The backend thread and its TODOs then
persist, so the scheduled run still executes against the (still-valid) backend
thread and emits the usual autonomous notification; the output is retained and
reappears when the client re-syncs, rather than firing into limbo. A
thread-existence precondition before firing is deliberately NOT used: it cannot
distinguish a genuinely deleted thread from a `legacy` / `todo-*` /
not-yet-materialized one, so it would drop legitimate autonomous work. Making
ghost-thread output more discoverable is a client/delivery concern, not a
scheduler fire-gate.

### 4. Recurrence Drift

**Status:** Fixed for monthly recurrence, including multi-cycle month-end
anchoring. The legacy `monthly` alias resolves to `1mo` and
`calculate_next_recurrence_time` advances via `dateutil.relativedelta`, so a
TODO anchored at 11:00 on the 15th fires at 11:00 on the 15th of every
subsequent month. For month-end anchors each slot is derived from a stable
origin (`TodoItem.recurrence_anchor`, adopted lazily from the first fired slot)
rather than the previous clamped slot, so a TODO on the 31st runs Jan 31 ->
Feb 28 -> Mar 31 -> Apr 30 -> May 31 and recovers the 31st in longer months,
instead of drifting down to the 28th permanently. Anchors clamp to the last
day of shorter months (Feb 28/29), matching Google Calendar's "monthly on the
31st" convention. Existing data stored as `30d` keeps its prior 30-day-fixed
behaviour until edited or recreated.

### 5. Timezone Edge Cases

**Risk:** Daylight saving time transitions could cause missed or double executions.

**Current Behavior:** All times stored as UTC timestamps.

**Mitigation:** Using UTC internally avoids most DST issues, but display in `USER_TIMEZONE` may show unexpected values during transitions.

---

## Verification Tests

### Test 1: Create Scheduled TODO via UI
1. Click "+ Add Task" button
2. Fill in task, set schedule for 1 minute from now
3. Select thread (new or existing)
4. Save
5. **Verify:** TODO appears in list with schedule badge
6. **Verify:** Logs show `[SCHEDULE DB] Adding TODO...`
7. **Wait:** TODO triggers at scheduled time
8. **Verify:** Response appears in selected thread

### Test 2: Edit Scheduled TODO
1. Click edit on existing scheduled TODO
2. Change schedule time
3. Save
4. **Verify:** Schedule badge updates
5. **Verify:** Logs show schedule DB sync

### Test 3: Recurring TODO
1. Create TODO with recurrence (for example `"5m"` or the legacy alias `"5min"`), scheduled for 1 minute
2. Wait for execution
3. **Verify:** TODO status resets to pending
4. **Verify:** Schedule updated to the next cadence slot based on the prior scheduled fire time, not completion time (for example a `5m` TODO due at 10:00 moves to 10:05 even if marked done at 10:01)

### Test 4: Visual Distinction
1. Create TODO via UI (should show user icon badge)
2. Have Nymeria create TODO via tool (should show no user badge)
3. **Verify:** Recurring TODOs show repeat icon

---

## Configuration

No new TODO-specific configuration options were introduced.
Scheduling behavior uses existing runtime settings such as:
- `TICKER_POLL_INTERVAL`
- `MAX_CONCURRENT_AUTONOMOUS`
