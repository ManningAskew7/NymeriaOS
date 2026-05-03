# Triggers

Event-driven automations that react to external events — webhooks, emails, RSS feeds, Slack messages, URL changes, and more. Triggers complement recurring TODOs (time-based) with event-based autonomous work.

## Concepts

**Source** — watches for external events (webhook, email, RSS, etc.). Lightweight, no LLM calls. Poll sources are checked every 30s by the ticker; webhook sources fire on-demand.

**Action** — what to do when events arrive. Three types:
- `agent_prompt` — send a prompt to Nymeria in the trigger's bound thread
- `notify` — publish a notification via SSE
- `create_todo` — add a TODO item

**Condition** — optional AND-logic filters applied to events before firing. Evaluated against event fields using operators like `contains`, `equals`, `matches_regex`.

**Thread binding** — every trigger is bound to exactly one thread. When the agent creates a trigger, it auto-binds to the current conversation thread. API-created triggers can specify `thread_id` explicitly, or leave it empty to get a dedicated `trigger-{id}` thread.

**Health** — automatic tracking of source errors. 2 consecutive failures → `degraded`, 5 → `failing` with exponential backoff (only retries every 10th cycle). Resets to `healthy` on success.

**Busy-thread deferral** — for `agent_prompt` actions, poll-sourced triggers check if the target thread is busy (non-blocking lock check). If busy, events are stored in `pending_events` and retried next cycle. No thread-pool slots are blocked. Webhook triggers bypass this — they POST to `/chat` which queues on the lock naturally.

## Available Sources

| Source | Type Key | Category | Description |
|--------|----------|----------|-------------|
| Webhook | `webhook` | custom | Receives HTTP POST payloads from external services |
| Outlook Email | `outlook_email` | communication | Monitors Microsoft 365 inbox for new emails |
| RSS/Atom Feed | `rss` | monitoring | Polls RSS/Atom feeds for new entries |
| HTTP Poll | `http_poll` | monitoring | Monitors URLs for changes, status codes, or content matches |
| Slack | `slack` | communication | Watches Slack channels for new messages |
| Microsoft Teams | `teams` | communication | Monitors Teams channels for new messages |

Sources auto-register on startup. Use `POST /triggers/sources/reload` to reload without restart.

## Source Configuration

### Webhook

Optional `secret` for authentication. Fire via `POST /triggers/fire/{trigger_id}?secret=<value>` with a JSON body.

Template variables: any keys in the POST body, plus `{fired_at}`, `{source_ip}`, `{trigger_id}`, `{trigger_name}`.

### Outlook Email

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `account_id` | string | yes | Microsoft account identifier |
| `folder` | string | no | Folder to monitor (default: `Inbox`) |
| `from_filter` | string | no | Only match emails from this address |
| `subject_filter` | string | no | Regex match on subject line |
| `body_contains` | string | no | Search term in body preview |
| `importance_filter` | string | no | Filter by importance: `high`, `normal`, `low` |

Template variables: `{subject}`, `{from_address}`, `{from_name}`, `{preview}`, `{received_at}`, `{importance}`, `{has_attachments}`.

Requires Microsoft OAuth — shares auth infrastructure with the Outlook add-in.

### RSS/Atom Feed

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | yes | RSS or Atom feed URL |
| `max_items` | integer | no | Max entries per check (default: 5) |

Template variables: `{title}`, `{link}`, `{summary}`, `{author}`, `{published}`, `{feed_title}`.

Dependency: `feedparser` (included in requirements).

### HTTP Poll

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | yes | URL to monitor |
| `method` | string | no | HTTP method (default: `GET`) |
| `headers` | object | no | Custom request headers |
| `fire_on` | string | no | When to fire: `change`, `status_code`, `contains`, `always` (default: `change`) |
| `expected_status` | integer | no | Status code to match (for `status_code` mode) |
| `contains_text` | string | no | Text to search for (for `contains` mode) |

Template variables: `{url}`, `{status_code}`, `{response_body}`, `{changed_at}`.

### Slack

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `bot_token` | string (secret) | yes | Slack Bot User OAuth Token |
| `channel_id` | string | yes | Channel ID to monitor |
| `keyword_filter` | string | no | Only match messages containing this text |
| `exclude_bots` | boolean | no | Skip bot messages (default: true) |

Template variables: `{channel_name}`, `{author}`, `{content}`, `{message_url}`, `{thread_ts}`.

### Microsoft Teams

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `account_id` | string | yes | Microsoft account identifier |
| `team_id` | string | yes | Teams team ID |
| `channel_id` | string | yes | Teams channel ID |
| `keyword_filter` | string | no | Only match messages containing this text |
| `exclude_bots` | boolean | no | Skip bot messages (default: true) |

Template variables: `{channel_name}`, `{team_name}`, `{author}`, `{content}`, `{message_url}`, `{created_time}`.

Shares Microsoft OAuth with Outlook source.

## Action Types

### agent_prompt

Sends a prompt to Nymeria in the trigger's bound thread. Template variables from the source event are interpolated into the prompt.

```json
{
  "type": "agent_prompt",
  "config": {
    "prompt_template": "New email from {from_name}: {subject}\n\nPreview: {preview}\n\nPlease summarize and flag if urgent."
  }
}
```

Multiple events in one poll cycle are batched into a single LLM call to avoid flooding.

### notify

Calls the `notify` tool without an LLM turn. By default this creates an
in-app notification and sends to any configured external notification
destinations.

```json
{
  "type": "notify",
  "config": {
    "message_template": "RSS update: {title} — {link}",
    "platform": "auto"
  }
}
```

### create_todo

Creates a TODO item for the user.

```json
{
  "type": "create_todo",
  "config": {
    "task_template": "Review Slack message from {author}: {content}"
  }
}
```

## Conditions

Filter events before they trigger actions. All conditions use AND logic.

```json
{
  "conditions": [
    {"field": "from_address", "operator": "contains", "value": "urgent", "case_sensitive": false},
    {"field": "subject", "operator": "not_equals", "value": "newsletter"}
  ]
}
```

Operators: `equals`, `not_equals`, `contains`, `starts_with`, `matches_regex`.

## Creating Triggers

### Via REST API

```bash
curl -X POST http://localhost:8000/triggers?user_id=default \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Urgent email alert",
    "source_type": "outlook_email",
    "source_config": {"account_id": "main", "from_filter": "boss@company.com"},
    "action_type": "agent_prompt",
    "action_config": {"prompt_template": "Urgent email from {from_name}: {subject}. Draft a reply."},
    "cooldown_seconds": 60,
    "conditions": [
      {"field": "importance", "operator": "equals", "value": "high"}
    ]
  }'
```

Optional `thread_id` in the body binds the trigger to an existing thread instead of creating a dedicated `trigger-{id}` thread.

### Via LLM Conversation

Ask Nymeria directly: "Create a trigger that watches my inbox for emails from X and summarizes them." The agent uses `trigger_config` for create/update/delete and `trigger_info` for list/detail/test/history/source schemas. Triggers created this way auto-bind to the current conversation thread.

### Via Desktop/Mobile UI

The Triggers section in the right panel provides:
- **TriggerFeed** — lists all triggers (This Thread / Global tabs)
- **TriggerSetupWizard** — 5-step guided creation (source → config → conditions → action → review)
- **TriggerItem** — toggle, test, view history, edit
- **TriggerHistoryPanel** — execution timeline with expandable details

## Testing Triggers

### Dry-run test endpoint (no execution)

```bash
curl -X POST http://localhost:8000/triggers/{trigger_id}/test?user_id=default \
  -H "Authorization: Bearer $API_KEY"
```

Returns a preview of what would happen if the trigger fired — sample event data, rendered action template, whether conditions would pass — without actually executing the action.

### Webhook fire (real execution)

The webhook fire endpoint is **public** (no API key required) so external services can call it. Authentication is via an optional per-trigger `secret` query parameter.

```bash
curl -X POST "http://localhost:8000/triggers/fire/{trigger_id}?secret=<value>&user_id=default" \
  -H "Content-Type: application/json" \
  -d '{"message": "Test webhook payload"}'
```

Returns `{"status":"fired",...}` immediately; actual agent execution happens in a background thread.

## Execution History

Every trigger execution is logged with status, duration, event summary, and response/error details. Rolling 200 entries per user.

```bash
# Per-trigger history
curl "http://localhost:8000/triggers/{trigger_id}/executions?user_id=default&limit=50" \
  -H "Authorization: Bearer $API_KEY"

# All recent executions
curl "http://localhost:8000/triggers/executions/recent?user_id=default&limit=50" \
  -H "Authorization: Bearer $API_KEY"
```

Statuses: `success`, `error`, `partial`, `deferred` (thread was busy).

Both poll-source and webhook fires log `TriggerExecution` records. Webhook fires log from the background thread that drives the internal `/chat` POST (success when the stream drains cleanly, error with `error_message` on any exception).

## Health Monitoring

Health status is tracked per trigger based on source check outcomes:

| Status | Condition | Behavior |
|--------|-----------|----------|
| `healthy` | 0 consecutive errors | Normal 30s polling |
| `degraded` | 2+ consecutive errors | Normal polling, warning logged |
| `failing` | 5+ consecutive errors | Exponential backoff — only checks every 10th cycle (~5 min) |

Resets to `healthy` on the first successful check. Health status and last error are visible in the UI and API responses.

## Streaming & Autonomous Events

When an `agent_prompt` trigger fires, the agent's response streams live into the frontend chat UI (same visual treatment as autonomous TODO executions). This section documents how that works and the timing hazards it avoids.

### Two execution paths

Triggers fire through **two different code paths** depending on the source:

**Path A — Poll sources (email, RSS, HTTP poll, Slack, Teams)**
```
Ticker (30s loop)
  → TriggerManager.check_triggers()
    → Source.check() → events
    → TriggerManager.fire_action_batch() or fire_action()
      → TriggerManager._fire_agent_prompt()
        → TriggerManager._stream_live()
          → iter_agent_astream(..., _is_self_invoke=True)
          → publish_autonomous_event() for each chunk
```

**Path B — Webhook source**
```
POST /triggers/fire/{id}
  → trigger_api.py fire_trigger endpoint
    → background thread POSTs to http://localhost:8000/chat
      with is_self_invoke=True, trigger_override="trigger"
      → /chat endpoint (triggers/api.py)
        → agent.astream()
        → publish_autonomous_event() for each chunk
```

Both paths publish the same event sequence on the same event bus, so the frontend handles them identically.

### Event sequence

For each `agent_prompt` fire, these events are published to the `/autonomous/stream` SSE endpoint:

| Event | When | Purpose |
|-------|------|---------|
| `task_started` | First non-`queued` chunk after thread lock acquired | Frontend engages streaming UI |
| `thinking` | During reasoning | Shows reasoning tokens |
| `tool_call` / `tool_result` | Tool use | Shows tool invocations |
| `response` | Each text chunk | Incremental streaming of reply |
| `workspace_artifact` | File output | Attaches artifacts |
| `task_completed` | End of stream | Frontend exits streaming mode, reloads history |

### Why `task_started` fires on the first chunk (not immediately)

Earlier versions published `task_started` the moment a trigger fire began. That caused a race when a user was actively chatting in the same thread:

1. User chat is streaming → `chatStore.isStreaming = true` on the frontend.
2. Trigger fires → publishes `task_started` → frontend receives it.
3. Frontend's `task_started` handler guards on `!chatStore.isStreaming`, so it skips the autonomous-streaming handoff.
4. Trigger execution waits on the per-thread lock until user chat finishes, yielding `queued` chunks while contended.
5. Trigger starts streaming `response` chunks, but the frontend's `activeTaskId` was never set → chunks are silently dropped.
6. `task_completed` triggers a full history reload → user sees the final response appear all at once after a delay.

The fix: **defer `task_started` until the first non-`queued` streaming chunk actually arrives** from `agent.astream()` or the sync `iter_agent_astream()` bridge. By that point the thread lock has been acquired and any prior user chat has released it, so `chatStore.isStreaming` is false and the frontend engages streaming mode cleanly.

This is implemented in both execution paths:
- Path A: `TriggerManager._stream_live()` in `trigger_manager.py` — accepts `task_started_data` and publishes `task_started` on the first non-`queued` chunk.
- Path B: `/chat` endpoint in `triggers/api.py` — tracks `autonomous_started` and publishes `task_started` on the first non-`queued` chunk of `agent.astream()`.

The frontend treats `task_started` as the normal handoff, but it also has a
recovery path: if a current-thread autonomous `thinking`, `tool_call`,
`tool_result`, `tool_reload`, `workspace_artifact`, or `response` event arrives
while no autonomous assistant bubble is active, it creates/re-arms the streaming
bubble and replays any buffered events. This prevents a missed or delayed
handoff from degrading into "final answer only after history reload."

### Frontend streaming handoff

The desktop frontend (`nymeria-desktop/src/lib/stores/autonomous.svelte.ts`) keeps one long-lived `/autonomous/stream` connection open with `fetch()` + `ReadableStream`, `Accept: text/event-stream`, and Bearer auth headers. It reconnects accidental disconnects, stream ends, HTTP errors, and idle heartbeat/data timeouts. After reconnect it refreshes the current thread history/context and syncs the thread list so missed trigger/TODO output is reconciled from persisted state before the next visible event.

`task_started` is still the normal UI handoff:

```typescript
case 'task_started':
  if (isCurrentThread && !chatStore.isStreaming) {
    activeTaskId = event.task_id;
    // show "Trigger:" prompt bubble if showAutonomousPrompts is enabled
    // create assistant placeholder
    chatStore.setStreaming(true);
    chatStore.setIntermediateContent('Autonomous task started...');
  }
```

Subsequent `response`, `thinking`, `tool_call`, `tool_result` events then append to the placeholder — same treatment as TODO autonomous streams.

If the trigger fires into a thread the user is **not currently viewing**, events are still published but the streaming UI isn't engaged for that thread. The response is saved to history and visible next time the user opens the thread.

For debugging, follow the event path in logs:

1. Worker/API publishes to Redis: `[REDIS EVENT BUS] publish type=...`
2. API receives Redis pub/sub: `[REDIS EVENT BUS] message_received type=... local_subscribers=...`
3. API enqueues locally: `[REDIS EVENT BUS] queue_enqueue subscriber=...`
4. `/autonomous/stream` consumes and yields: `[AUTONOMOUS SSE] queue_receive ...` then `[AUTONOMOUS SSE] yield ...`
5. Desktop logs first byte/frame/data-event and sampled `[Autonomous] Event handled type=...`

### Cooldown & fire count

Before dispatching, the webhook fire endpoint atomically checks `cooldown_seconds` against `last_fired` and increments `fire_count` inside a single `TriggerManager.atomic_update()` block. This prevents two concurrent webhook hits from both passing the cooldown check or from both reading `fire_count=N` and each writing `N+1` (losing an increment). Returns `429` with remaining seconds if still cooling down.

### Event payload identity

Both execution paths include `trigger_id` and `trigger_name` in the `task_started` and `task_completed` payloads. The frontend's `classifyAutonomousSource()` uses these to label the prompt bubble as a trigger (vs. scheduler/watchdog/autonomous), and `threadsStore.ensureThread()` uses `trigger_name` on `task_started` to auto-create a sidebar entry for triggers bound to new threads.

For the webhook path, these flow through two optional fields on `ChatRequest` (`trigger_id`, `trigger_name`) which the internal fire POST sets — the `/chat` event_generator merges them into every autonomous event it publishes.

### Pending events cap

Poll-sourced triggers that fire into a busy thread store events in `pending_events` for the next poll cycle. The list is capped at 50; when exceeded, the **newest** 50 are kept (stale alerts are less useful than fresh ones).

## Architecture

**Storage:**
- `data/triggers/{user_id}.json` — trigger definitions
- `data/triggers/{user_id}_executions.json` — execution log (rolling 200 entries, JSON array)

`TriggerManager.get_all_users_with_triggers()` filters out `*_executions.json` files when enumerating users — otherwise the ticker would treat the execution log as a user's trigger store, fail to parse it as `TriggerStore`, and `atomic_update`'s save-on-exit would clobber the log.

**Event bus:**
- `nymeria/core/event_bus.py` — in-memory `EventBus` with per-subscriber `Queue`
- `nymeria/core/event_bus_redis.py` — `RedisEventBus` for cross-container delivery; API + worker containers both subscribe
- All `publish_autonomous_event()` calls route through `get_event_bus()`

**Redis round-trip:** When `RedisEventBus.publish()` is called, it publishes to Redis only — local dispatch happens via the subscriber thread receiving the message back from Redis. This means API container events round-trip through Redis to reach that same container's SSE subscribers. Adds a few ms of latency but is what makes cross-container delivery work.

## Code Map

Which file does what, for quick navigation:

| File | Responsibility |
|------|----------------|
| `nymeria/core/trigger_manager.py` | `TriggerManager` class, poll-source fire logic (`fire_action`, `fire_action_batch`, `_fire_agent_prompt`, `_stream_live`), condition evaluation, execution logging, `_safe_format` template helper |
| `nymeria/core/ticker.py` | 30s loop that calls `TriggerManager.check_triggers()` and iterates sources |
| `nymeria/triggers/trigger_api.py` | REST router mounted at `/triggers`: CRUD, `/fire/{id}` webhook endpoint (public, routes internal POST to `/chat`), `/test`, `/executions/recent`, `/sources/list` |
| `nymeria/triggers/api.py` | Main API app. `/chat` endpoint publishes autonomous events when `is_self_invoke=True`. `/autonomous/stream` SSE endpoint consumes the event bus queue |
| `nymeria/triggers/sources/` | Individual source implementations (webhook, outlook_email, rss, http_poll, slack, teams). Each exposes `check()`, `validate_config()`, `get_sample_event()` |
| `nymeria/tools/triggers.py` | Agent-callable trigger tools: `trigger_config` for configuration mutations and `trigger_info` for read-only inspection. Auto-binds created triggers to the current thread via `get_thread_id(config)` |
| `nymeria/core/event_bus.py` | `EventBus`, `AutonomousEvent`, `publish_autonomous_event`, `publish_sync_event`, factory `create_event_bus()` |
| `nymeria/core/event_bus_redis.py` | `RedisEventBus` — pub/sub across containers |
| `nymeria-desktop/src/lib/stores/autonomous.svelte.ts` | Desktop autonomous SSE subscriber. Uses fetch streaming with Bearer auth, reconnect/idle guards, reconnect catch-up, sampled diagnostics, and `handleEvent()` dispatch by type. `classifyAutonomousSource()` labels triggers via `event.trigger_id \|\| event.trigger_name` |
| `nymeria-mobile/src/lib/stores/autonomous.svelte.ts` | Mobile autonomous SSE subscriber. Uses fetch streaming with Bearer auth, reconnect/idle guards, reconnect catch-up, Capacitor Network offline/online handling, and mobile lifecycle pause/resume. Keeps the legacy `api_key` query fallback for WebView compatibility. |
| `nymeria-desktop/src/lib/components/triggers/` | UI: `TriggerFeed`, `TriggerSetupWizard`, `TriggerItem`, `TriggerHistoryPanel` |

## Webhook Integration with External Services

The webhook source works with any service that can send HTTP POST requests:

- **IFTTT** — Use the Webhooks service to POST to `http://your-server:8000/triggers/fire/{trigger_id}?secret=<value>`
- **Zapier** — Use the Webhook action to POST JSON payloads with the secret as a query parameter
- **GitHub** — Configure repository webhooks to send events to your trigger endpoint
- **Tasker / n8n / custom scripts** — Any `curl` or HTTP client can fire a webhook trigger

The secret query parameter (if configured on the trigger) is the only auth mechanism — do not expose the webhook URL publicly without one.
