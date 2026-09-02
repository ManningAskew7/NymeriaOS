# Triggers

Event-driven automations that react to external events  -  webhooks, emails, RSS feeds, Slack messages, URL changes, and more. Triggers complement recurring TODOs (time-based) with event-based autonomous work.

## Concepts

**Source**  -  watches for external events (webhook, email, RSS, etc.). Lightweight, no LLM calls. Poll sources are checked every 30s by the ticker; webhook sources fire on-demand.

**Action**  -  what to do when events arrive. Four types:
- `agent_prompt`  -  send a prompt to Nymeria in the trigger's bound thread
- `notify`  -  publish a notification via SSE
- `create_todo`  -  add a TODO item
- `run_workflow`  -  run a published nym-SDK workflow tool headlessly (no LLM call)

**Condition**  -  optional AND-logic filters applied to events before firing. Evaluated against event fields using operators like `contains`, `equals`, `matches_regex`.

**Thread binding**  -  every trigger is bound to exactly one thread. When the agent creates a trigger, it auto-binds to the current conversation thread. API-created triggers can specify `thread_id` explicitly, or leave it empty to get a dedicated `trigger-{id}` thread.

**Health**  -  automatic tracking of source AND action errors (one shared counter). 2 consecutive failures → `degraded`, 5 → `failing` with exponential backoff (only retries every 10th cycle). Resets to `healthy` on success. Repeated ACTION failures additionally alert the owner and then AUTO-PAUSE the trigger, which stops it polling and firing while leaving `enabled` alone; `/triggers resume <id>` clears it.

**Busy-thread deferral**  -  for `agent_prompt` actions, poll-sourced triggers check if the target thread is busy (non-blocking lock check). If busy, events are stored in `pending_events` and retried next cycle. No thread-pool slots are blocked. Webhook triggers bypass this  -  they POST to `/chat` which queues on the lock naturally.

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

A non-empty shared `secret` is required for public webhook fire requests. Fire via `POST /triggers/fire/{trigger_id}?secret=<value>` with a JSON body. Authenticated API callers may instead include `Authorization: Bearer <token>` to fire their own webhook trigger without placing the shared secret in the URL.

Template variables: any keys in the POST body, plus `{fired_at}`, `{source_ip}`, `{trigger_id}`, `{trigger_name}`.

### Secrets in source config

A source declares which of its config fields hold credentials with
`secret: true` in its config schema (today `webhook.secret`,
`slack.bot_token`, and `http_poll.headers`, whose VALUES are arbitrary
`Authorization` headers). Those are **fingerprinted on every read**: the
agent's `trigger_info` detail view, the REST read model, and both GUIs get
the first 4 and last 3 characters, never the full value. Enough to check
which secret is configured, useless to replay.

Two consequences worth knowing:

- **A fingerprint is not a secret.** Saving an untouched field back is safe:
  an incoming value equal to the fingerprint of the stored one is treated as
  unchanged, and the update SAYS SO rather than answering a bare "updated",
  so a caller can tell "my new secret was saved" from "the fingerprint I sent
  was ignored" without firing the webhook to find out. A fingerprint copied
  into a NEW trigger is refused with a 400 instead: create has no stored
  value to compare against, so it cannot tell a copy from a real secret, and
  storing one would turn an 11-character string from a transcript into a live
  credential.
- **There is no reveal.** Triggers are owner-scoped and an agent acts as the
  owner, so an owner-only reveal would gate nothing. If a secret is lost, set
  a new one and update whatever calls the webhook.

Masking is on RENDERING only. The store still holds the plaintext, because
the anonymous fire route derives a webhook's OWNER by matching the secret it
was given against stored secrets.

### Outlook Email

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `account_id` | string | yes | Microsoft account identifier. Must be visible to the trigger's thread: an account bound (`auth_bindings`, `thread:<id>`) to a different thread raises a selection error recorded as a source failure rather than being polled |
| `folder` | string | no | Folder to monitor (default: `Inbox`) |
| `from_filter` | string | no | Only match emails from this address |
| `subject_filter` | string | no | Regex match on subject line |
| `body_contains` | string | no | Search term in body preview |
| `importance_filter` | string | no | Filter by importance: `high`, `normal`, `low` |

Template variables: `{subject}`, `{from_address}`, `{from_name}`, `{preview}`, `{received_at}`, `{importance}`, `{has_attachments}`.

Requires Microsoft OAuth (shares Microsoft Graph infrastructure).

### RSS/Atom Feed

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | yes | RSS or Atom feed URL |
| `max_items` | integer | no | Max entries per check (default: 5) |

Template variables: `{title}`, `{link}`, `{summary}`, `{author}`, `{published}`, `{feed_title}`.

`{published}` falls back across feed dialects: `published`, then `updated`,
then `created`. Atom REQUIRES `<updated>` and makes `<published>` optional
(GitHub's releases, commits, and tags feeds all omit `<published>`), and
feedparser does not alias one onto the other, so reading `published` alone
delivered a blank date on those feeds. `fetch_url_nymeria` previews a feed
through the same reader, so what the preview shows is what the trigger
delivers.

RSS polling uses Nymeria's HTTP egress policy, so loopback, private, link-local,
metadata, and blocked-domain targets are rejected unless explicitly allowed by
the operator. Dependency: `feedparser` (included in requirements).

### HTTP Poll

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | yes | URL to monitor |
| `method` | string | no | HTTP method (default: `GET`) |
| `headers` | object (secret values) | no | Custom request headers; values are fingerprinted on read |
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
| `account_id` | string | yes | Microsoft account identifier. Must be visible to the trigger's thread: an account bound (`auth_bindings`, `thread:<id>`) to a different thread raises a selection error recorded as a source failure rather than being polled |
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

Calls the `notify` tool without an LLM turn. Routes through the user's
configured notification profile (or a specific named profile if you set
one). Always writes an audit-log row in the in-app feed.

```json
{
  "type": "notify",
  "config": {
    "message_template": "RSS update: {title} - {link}",
    "profile": "urgent"
  }
}
```

`profile` is optional; omit to use the per-thread or per-user default
profile. The legacy `"platform": "telegram"` key is still accepted for
backward compatibility. See [`notifications.md`](notifications.md).

### create_todo

Creates a TODO item for the user. Add an optional `scheduled_for` to schedule
it: a relative duration (`30m`, `1h`, `1d`), an ISO timestamp, or an absolute
`YYYY-MM-DD HH:MM` (interpreted in the user's timezone). A scheduled TODO is
registered in the ticker's schedule index and fires at that time like any other
scheduled TODO; without `scheduled_for` it is a plain unscheduled item.

```json
{
  "type": "create_todo",
  "config": {
    "task_template": "Review Slack message from {author}: {content}",
    "scheduled_for": "1h"
  }
}
```

### run_workflow

Runs a published workflow tool headlessly: no prompt, no LLM call, no agent
turn. The workflow body decides what to do with the event.

```json
{
  "type": "run_workflow",
  "config": {
    "workflow_id": "wf_slack_digest",
    "params": {"channel": "ops"}
  }
}
```

The binding is validated when the trigger is created or updated (the
workflow must exist, its revision must be admin-approved, `params` may only
name declared parameters, and every required parameter must be covered). At
fire time the raw event dict is passed as the workflow's `event` parameter
when its signature declares one; there is no per-field mapping config, the
authored body extracts what it needs. A run that suspends on `nym.approve`
counts as a successful fire. The run never delivers output anywhere by
itself: delivery is the workflow's explicit job (`nym.thread` /
`nym.notify`), so a headless fire cannot leak output to a guessed
destination.

## Scheduled (time-based) workflows

Triggers are event-based. For time-based recurring work, bind a published
workflow to a recurring **TODO** instead: set `workflow_id` (and optional
`workflow_params`) on the TODO and the ticker runs that workflow headlessly on
schedule, with no agent turn. A workflow that calls no AI verbs (`nym.llm` /
`nym.thread`) is genuinely zero-LLM, so a scheduled pure-tool job costs zero
tokens per fire, the counterpart to the `run_workflow` trigger action for a
clock rather than an event.

```
nym_todo(
    task="Watch the changelog",
    scheduled_for="30m",
    recurrence="30m",
    workflow_id="url_watcher",
    workflow_params={"url": "https://example.com/changelog"},
)
```

The same shape over REST is `POST /todos` with `workflow_id` /
`workflow_params`. The binding is validated at creation (the workflow must
exist, its revision must be admin-approved, and every required parameter must
be covered; a TODO-bound workflow may not declare an `event` parameter). The
binding is create-only, delete and recreate the TODO to rebind. Recurrence,
retries, and the missed-work policy apply exactly as they do to ordinary
scheduled TODOs; a non-recurring workflow TODO closes itself when the run
finishes (there is no agent turn to close it). Delivery is the workflow's own
job (`nym.notify` / `nym.thread`), the ticker only fires it.

`url_watcher` above is the bundled zero-LLM recipe: poll a URL, remember the
last content digest in workflow state, and notify only on a real change.
Install it (admin) with `tool_create(action="install_template",
template_id="url_watcher")`, or author your own zero-AI watcher from the
`workflow-authoring` skill kit's cookbook.

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

Operators: `equals`, `not_equals`, `contains`, `starts_with`, `matches_regex`,
plus the numeric comparisons `gt`, `gte`, `lt`, `lte` (both sides coerce via
`float()`; a non-numeric value on either side is a non-match, never an error).
The condition model is shared with the lifecycle-hooks stack
(`core/conditions.py`), so hook fire conditions and trigger conditions
serialize identically.

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
- **TriggerFeed**  -  lists all triggers (This Thread / Global tabs)
- **TriggerSetupWizard**  -  5-step guided creation (source → config → conditions → action → review)
- **TriggerItem**  -  toggle, test, view history, edit
- **TriggerHistoryPanel**  -  execution timeline with expandable details

## Testing Triggers

### Dry-run test endpoint (no execution)

```bash
curl -X POST http://localhost:8000/triggers/{trigger_id}/test?user_id=default \
  -H "Authorization: Bearer $API_KEY"
```

Returns a preview of what would happen if the trigger fired  -  sample event data, rendered action template, whether conditions would pass  -  without actually executing the action.

### Webhook fire (real execution)

The webhook fire endpoint is **public** so external services can call it, but public calls must include the trigger's shared `secret` query parameter. A trigger without a configured secret is not publicly fireable; use Bearer auth to fire it from a trusted Nymeria client.

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

Health status is tracked per trigger. Source-check outcomes AND action
outcomes (the agent turn, notify, create_todo, or workflow run the trigger
fires) feed one shared counter, so a trigger whose action errors on every
fire degrades the same way as one whose source is broken:

| Status | Condition | Behavior |
|--------|-----------|----------|
| `healthy` | 0 consecutive errors | Normal 30s polling |
| `degraded` | 2+ consecutive errors | Normal polling, warning logged |
| `failing` | 5+ consecutive errors | Exponential backoff  -  only checks every 10th cycle (~5 min) |

The reset is scoped by which plane last failed (`last_error_kind`): a
successful source check heals a source-failure streak but NOT an
action-failure streak (source checks succeed on every poll cycle, which
would otherwise zero the action streak before it could reach a threshold);
a successful action fire heals either, since it proves the whole pipeline
works. Health status and last error are visible in the UI and API
responses. On the transition into `failing`, a SOURCE-plane episode sends
the owner one alert (in-app plus the external destinations of their default
notification profile), once per episode, since only the scoped success
resets the counter.

### The action-failure policy: alert, then stop

Health labels alone never stopped anything: a trigger firing into a broken
action stayed enabled and kept consuming events indefinitely (backlog #264,
measured at 16 days and 89 consumed-and-dropped emails on a real Outlook
watcher). The ACTION plane is therefore governed by a policy on its own
counter, `action_failures`:

| Threshold | Setting (0 disables) | What happens |
|---|---|---|
| alert | `TRIGGER_FAILURE_ALERT_AFTER` (default 2) | One owner alert naming the trigger, the streak and the last error |
| pause | `TRIGGER_FAILURE_PAUSE_AFTER` (default 5) | `auto_paused_at` is stamped and a second alert says it gave up |
| alert cooldown | `TRIGGER_FAILURE_ALERT_COOLDOWN_MINUTES` (default 180) | Suppresses repeat ALERTS for one trigger inside the window; the pause alert is never suppressed. On the SOURCE plane the same window is the repeat interval for `[TRIGGER STILL FAILING]` |

`action_failures` is deliberately separate from `consecutive_errors`: the
shared counter also counts backed-off polls while failing (that is what
makes the backoff lapse), so it reads far higher than the real failure
count and no honest threshold can sit on it.

An auto-paused trigger is skipped by the poll loop entirely, so its source
is never checked and it consumes nothing, and its webhook fire endpoint
answers 409. The `/triggers/{id}/test` dry run still works, which is how
you verify a repair before resuming.

**Coverage gap worth knowing.** The policy counts failures recorded by
`fire_action`/`fire_action_batch`, which is the POLL path. A webhook
trigger with an `agent_prompt` action fires through a different route
(`POST /triggers/fire/{id}` relays into `POST /chat`) that records an
execution row and no action health, so that combination never accumulates
`action_failures` and can never reach either threshold on its own. The 409
gate above still applies once such a trigger is paused by some other
means, but nothing will pause it automatically.

Neither threshold releases a trigger that is already paused: setting
`TRIGGER_FAILURE_PAUSE_AFTER=0` stops future auto-pauses and leaves
existing ones to be resumed explicitly. And with BOTH knobs at 0 the
action plane is fully silent, since the policy replaced the old
failing-transition alert on that plane.

**Auto-pause is not `enabled`.** The user's toggle is untouched, so a paused
trigger still reports `enabled: true` while doing nothing: clients must read
`auto_paused_at` to know whether a trigger actually runs. Re-enabling does
not resume it, and resuming does not enable it.

SOURCE failures never auto-pause. An outage (a token expiring, an API down)
usually self-heals, and the backoff already covers it; stopping the trigger
would leave one to resume by hand every time a feed blipped.

Instead, a failing source REPEATS its alert. The crossing into `failing`
sends `[TRIGGER FAILING]` as before, and while it stays failing each further
failed poll sends `[TRIGGER STILL FAILING]` once
`TRIGGER_FAILURE_ALERT_COOLDOWN_MINUTES` has elapsed since the last one, so a
source that will never recover (a revoked token, a deleted mailbox, a retired
endpoint) keeps reminding you rather than going silent. Setting that knob to
`0` disables the reminder and restores one alert per episode. The reminder
points at fixing or disabling, not resuming: resume clears health, and a
genuinely dead source just fails its way back to `failing`.

A trigger whose source type is not registered at all (its plugin removed from
the build) is treated as a failing source rather than skipped, so it reports
`failing` and alerts instead of sitting `healthy` and silent.

### Resuming

`POST /triggers/{id}/resume` (agent: `trigger_config(action="resume")`; CLI
and chat: `/triggers resume <id>`) clears the pause marker, the action
streak, the health counters and the last error in one act. Use it after
fixing whatever the trigger was failing on, or it will simply pause again.

It is also the repair for a trigger that is merely `failing` rather than
paused: before it existed, a trigger whose cause you had already fixed kept
skipping 9 of 10 polls until an action happened to succeed, and editing
`data/triggers/<user>.json` by hand was the only way to clear it.

Resume is explicit, never inferred from some other field being written. It
also clears the alert cooldown, so a repaired trigger that fails again
speaks immediately rather than waiting out a window from its last episode.

**Known hazard on the repair path (backlog #249).** `check_triggers` holds
the whole trigger store in memory across every source's network check and
then writes it back whole, and the trigger store is written by two
processes in the Docker shape (the worker polls, the API serves resume and
CRUD). So a resume that lands while a poll is mid-flight can be reverted by
the worker's stale write: the call answers 200 and the trigger stays
paused. Retrying works, and any listing shows the true state. This is
pre-existing and applies equally to enable/disable/delete; it is recorded
here because resume is the repair verb, so this is where a user meets
it.

## Chat-Bot Emoji Reaction Triggers

Discord and Telegram bot messages can act as a lightweight trigger surface: a
user reacting with an emoji on one of the bot's own messages fires an agent
turn in the mapped thread. This is a bot-level event path, not a trigger
source: there is no trigger definition, no conditions, and nothing appears in
the trigger list or execution history. The turn carries the standard source
taxonomy (`source="trigger"`, `trigger_override="reaction"`, and a
`source_label` like `reaction 👍`), so lifecycle hooks can target or exclude
reaction turns via `fire_conditions` the same way they filter trigger fires.

Per-platform opt-in toggles (both default off):

| Setting | Platform |
|---------|----------|
| `DISCORD_REACTION_TRIGGER_ENABLED` | Discord (`on_raw_reaction_add`; the bot enables the reactions gateway intent automatically) |
| `TELEGRAM_REACTION_TRIGGER_ENABLED` | Telegram (`MessageReactionHandler`; private chats only, because Telegram reaction updates do not identify the reacted message's author) |

Scope and loop guards: only reactions **added** by a human to a message
**authored by the bot** fire (removals are ignored, the bot's own reactions
are ignored, other bots are ignored), and a repeat of the same (message,
reactor, emoji) within 45 seconds is debounced so emoji toggling cannot burn
repeated agent turns. Unlinked platform users are silently
dropped, matching the bots' existing access model. The agent receives a
synthetic prompt of the form `[Reaction] Alice reacted with 👍 to your
message: "..."` plus, when the `react` tool is not bound in the thread, a
guidance block containing the tool's compact args schema and a `tool_invoke`
recipe so it can react back without a graph rebuild.

The outbound half (the `react` catalog tool, `reaction_request` bus events,
and `suppress_reply` for emoji-only responses) is documented per platform in
`docs/chat-apps/discord-bot.md` and
`docs/chat-apps/telegram-bot.md`, with the wire contract in
`docs/api.md`.

## Streaming & Autonomous Events

When an `agent_prompt` trigger fires, the agent's response streams live into the frontend chat UI (same visual treatment as autonomous TODO executions). This section documents how that works and the timing hazards it avoids.

### Two execution paths

Triggers fire through **two different code paths** depending on the source:

**Path A  -  Poll sources (email, RSS, HTTP poll, Slack, Teams)**
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

**Path B  -  Webhook source**
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
- Path A: `TriggerManager._stream_live()` in `trigger_manager.py`  -  accepts `task_started_data` and publishes `task_started` on the first non-`queued` chunk.
- Path B: `/chat` endpoint in `triggers/api.py`  -  tracks `autonomous_started` and publishes `task_started` on the first non-`queued` chunk of `agent.astream()`.

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

Subsequent `response`, `thinking`, `tool_call`, `tool_result` events then append to the placeholder  -  same treatment as TODO autonomous streams.

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

For the webhook path, these flow through two optional fields on `ChatRequest` (`trigger_id`, `trigger_name`) which the internal fire POST sets  -  the `/chat` event_generator merges them into every autonomous event it publishes.

### Pending events cap

Poll-sourced triggers that fire into a busy thread store events in `pending_events` for the next poll cycle. The list is capped at 50; when exceeded, the **newest** 50 are kept (stale alerts are less useful than fresh ones).

## Architecture

**Storage:**
- `data/triggers/{user_id}.json`  -  trigger definitions
- `data/triggers/{user_id}_executions.json`  -  execution log (rolling 200 entries, JSON array)

`TriggerManager.get_all_users_with_triggers()` filters out `*_executions.json` files when enumerating users  -  otherwise the ticker would treat the execution log as a user's trigger store, fail to parse it as `TriggerStore`, and `atomic_update`'s save-on-exit would clobber the log.

**Event bus:**
- `nymeria/core/event_bus.py`  -  in-memory `EventBus` with per-subscriber `Queue`
- `nymeria/core/event_bus_redis.py`  -  `RedisEventBus` for cross-container delivery; API + worker containers both subscribe
- All `publish_autonomous_event()` calls route through `get_event_bus()`

**Redis round-trip:** When `RedisEventBus.publish()` is called, it publishes to Redis only  -  local dispatch happens via the subscriber thread receiving the message back from Redis. This means API container events round-trip through Redis to reach that same container's SSE subscribers. Adds a few ms of latency but is what makes cross-container delivery work.

**Docker trigger firing (single agent runtime):** In Docker, the worker
container schedules polls but no longer runs the agent. When a poll-based
trigger fires, the worker calls `/chat` on the API container with
`publish_autonomous_events=False` and `trigger_id` / `trigger_name`
populated. The worker keeps publishing `task_started` /
`task_completed` and mirroring agent stream chunks itself with the
stable task id `f"trigger-{trigger.id}"`; the API suppresses its own
autonomous mirroring for the call to avoid duplicates. Webhook fires
(`POST /triggers/fire/{id}`) are unaffected  -  they already enter the
API directly and use `publish_autonomous_events=True` (the default).

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
| `nymeria/core/event_bus_redis.py` | `RedisEventBus`  -  pub/sub across containers |
| `nymeria-desktop/src/lib/stores/autonomous.svelte.ts` | Desktop autonomous SSE subscriber. Uses fetch streaming with Bearer auth, reconnect/idle guards, reconnect catch-up, sampled diagnostics, and `handleEvent()` dispatch by type. `classifyAutonomousSource()` labels triggers via `event.trigger_id \|\| event.trigger_name` |
| `nymeria-mobile/src/lib/stores/autonomous.svelte.ts` | Mobile autonomous SSE subscriber. Uses fetch streaming with Bearer auth, reconnect/idle guards, reconnect catch-up, Capacitor Network offline/online handling, and mobile lifecycle pause/resume. Keeps the legacy `api_key` query fallback for WebView compatibility. |
| `nymeria-desktop/src/lib/components/triggers/` | UI: `TriggerFeed`, `TriggerSetupWizard`, `TriggerItem`, `TriggerHistoryPanel` |

## Webhook Integration with External Services

The webhook source works with any service that can send HTTP POST requests:

- **IFTTT**  -  Use the Webhooks service to POST to `http://your-server:8000/triggers/fire/{trigger_id}?secret=<value>`
- **Zapier**  -  Use the Webhook action to POST JSON payloads with the secret as a query parameter
- **GitHub**  -  Configure repository webhooks to send events to your trigger endpoint
- **Workflow tools / custom scripts**  -  Any `curl` or HTTP client can fire a webhook trigger

The secret query parameter is required for public webhook calls. Treat webhook URLs as secrets because the shared secret is embedded in the URL for many third-party webhook integrations.
