# User TODO Management with Recurring Schedules

## Overview

This feature allows users to create, edit, and delete TODOs for Nymeria through the desktop UI, with support for recurring schedules. Previously, only the LLM agent could manage TODOs via tools.

**Key Features:**
- Full CRUD operations for TODOs via REST API
- Simple recurrence presets (hourly, daily, weekly, monthly)
- Visual distinction between user-created vs agent-created TODOs
- Visual distinction between recurring vs one-time TODOs
- Thread selection for scheduled TODO output

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
| CRUD endpoints | `nymeria/triggers/api.py` | REST API for create/update/delete/complete |
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
    recurrence: Optional[str] = Field(default=None, description="'hourly', 'daily', 'weekly', 'monthly'")
```

### REST API Endpoints

**File:** `nymeria/triggers/api.py`

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
    task: str                    # Required
    priority: Optional[str]      # low, medium, high
    deadline: Optional[datetime]
    notes: Optional[str]
    scheduled_for: Optional[str] # "30m", "2h", or "2026-01-15T14:00"
    recurrence: Optional[str]    # hourly, daily, weekly, monthly
    thread_id: Optional[str]     # Thread for scheduled execution output

class TodoUpdateRequest(BaseModel):
    task: Optional[str]
    priority: Optional[str]
    status: Optional[str]
    deadline: Optional[datetime]
    notes: Optional[str]
    blocked_reason: Optional[str]
    scheduled_for: Optional[str]
    recurrence: Optional[str]
    thread_id: Optional[str]
    clear_schedule: bool = False
    clear_recurrence: bool = False
    clear_deadline: bool = False
```

### Timezone Handling

**Critical Implementation Detail:**

The `_parse_scheduled_for` function handles timezone conversion:

```python
def _parse_scheduled_for(scheduled_for: Optional[str]) -> Optional[datetime]:
    # Relative times: "30s", "5m", "2h", "1d", "1w"
    # Uses timezone-aware UTC: datetime.now(timezone.utc) + timedelta(...)

    # Absolute times: "2026-01-15T14:00"
    # Parsed as LOCAL time, then converted to UTC:
    # 1. datetime.strptime() -> naive datetime
    # 2. .astimezone() -> add local timezone
    # 3. .astimezone(timezone.utc) -> convert to UTC
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

When a scheduled TODO is executed:

1. If `recurrence` is set, calculate next execution time
2. Update `scheduled_for` to next time
3. Reset status to `pending`
4. Re-sync to TodoScheduleDB

```python
def _calculate_next_execution(self, recurrence: str, from_time: datetime) -> Optional[datetime]:
    if recurrence == "hourly":
        return from_time + timedelta(hours=1)
    elif recurrence == "daily":
        return from_time + timedelta(days=1)
    elif recurrence == "weekly":
        return from_time + timedelta(weeks=1)
    elif recurrence == "monthly":
        return from_time + timedelta(days=30)  # Approximate
    return None
```

---

## Frontend Implementation

### TypeScript Types

**File:** `nymeria-desktop/src/lib/types/index.ts`

```typescript
export type TodoRecurrence = 'hourly' | 'daily' | 'weekly' | 'monthly';
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
- Priority select
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

## Critical Bug Fixes

### 1. Schedule Sync Timing Bug

**Problem:** User-created TODOs weren't triggering because the schedule wasn't being synced to the database.

**Root Cause:** `sync_schedule_to_db()` was called inside `atomic_update` context manager, but it reads from disk - and the file hadn't been saved yet.

**Fix:** Move `sync_schedule_to_db()` call OUTSIDE the `atomic_update` context:

```python
# WRONG - sync inside atomic_update
with todo_manager.atomic_update(user_id) as todo_list:
    item = todo_list.add_item(...)
    todo_manager.sync_schedule_to_db(user_id, item.id, schedule_db)  # File not saved yet!

# CORRECT - sync after atomic_update
with todo_manager.atomic_update(user_id) as todo_list:
    item = todo_list.add_item(...)
    created_item = item

# File is now saved
todo_manager.sync_schedule_to_db(user_id, created_item.id, schedule_db)
```

### 2. Timezone Conversion Bug

**Problem:** Scheduled times were off by hours due to timezone mishandling.

**Root Cause:** `datetime.utcnow()` returns a naive datetime, but `timestamp()` interprets naive datetimes as local time.

**Fix:** Use timezone-aware datetimes:

```python
# WRONG
datetime.utcnow() + timedelta(hours=1)  # Naive, timestamp() assumes local

# CORRECT
datetime.now(timezone.utc) + timedelta(hours=1)  # Aware, timestamp() correct
```

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
- Ticker uses `_processing` set to track in-flight TODOs
- API should check if TODO is currently executing before allowing edits

### 3. Thread Deletion

**Risk:** User deletes a thread that a scheduled TODO references.

**Current Behavior:** TODO executes with `thread_id` that doesn't exist in frontend localStorage.

**Impact:** Response appears in a "ghost" thread not visible in sidebar.

**Potential Fix:** Validate thread existence before execution, or create thread if missing.

### 4. Recurrence Drift

**Risk:** Monthly recurrence uses 30-day approximation, causing drift over time.

**Current Behavior:** `timedelta(days=30)` for monthly recurrence.

**Potential Fix:** Use `dateutil.relativedelta` for accurate month calculations.

### 5. Timezone Edge Cases

**Risk:** Daylight saving time transitions could cause missed or double executions.

**Current Behavior:** All times stored as UTC timestamps.

**Mitigation:** Using UTC internally avoids most DST issues, but display in local time may show unexpected values during transitions.

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
1. Create TODO with "hourly" recurrence, scheduled for 1 minute
2. Wait for execution
3. **Verify:** TODO status resets to pending
4. **Verify:** Schedule updated to +1 hour

### Test 4: Visual Distinction
1. Create TODO via UI (should show user icon badge)
2. Have Nymeria create TODO via tool (should show no user badge)
3. **Verify:** Recurring TODOs show repeat icon

---

## Configuration

No new configuration options. Uses existing settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `CONTEXT_WINDOW_CYCLES` | 5 | Cycles to keep in thread after scheduled execution |

---

## Files Modified

### Backend
- `nymeria/core/todo_manager.py` - Added `created_by`, `recurrence` fields
- `nymeria/triggers/api.py` - Added CRUD endpoints, fixed sync timing
- `nymeria/core/ticker.py` - Added recurrence handling, debug logging
- `nymeria/core/todo_schedule_db.py` - Added debug logging

### Frontend
- `nymeria-desktop/src/lib/types/index.ts` - Added types
- `nymeria-desktop/src/lib/services/api.svelte.ts` - Added CRUD methods
- `nymeria-desktop/src/lib/stores/todos.svelte.ts` - Added mutations
- `nymeria-desktop/src/lib/components/todos/TodoForm.svelte` - New component
- `nymeria-desktop/src/lib/components/todos/TodoItem.svelte` - Added badges, actions
- `nymeria-desktop/src/lib/components/todos/TodoFeed.svelte` - Added create button
- `nymeria-desktop/src/lib/components/common/Icon.svelte` - Added icons
