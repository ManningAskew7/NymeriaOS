# Triggers

Event-driven automations that react to external events — webhooks, emails, RSS feeds, Slack messages, URL changes, and more. Triggers complement recurring TODOs (time-based) with event-based autonomous work.

## Concepts

**Source** — watches for external events (webhook, email, RSS, etc.). Lightweight, no LLM calls. Checked every 30s by the ticker.

**Action** — what to do when events arrive. Three types:
- `agent_prompt` — send a prompt to Nymeria in the trigger's dedicated thread
- `notify` — publish a notification via SSE
- `create_todo` — add a TODO item

**Condition** — optional AND-logic filters applied to events before firing. Evaluated against event fields using operators like `contains`, `equals`, `matches_regex`.

**Health** — automatic tracking of source errors. 2 consecutive failures → `degraded`, 5 → `failing` with exponential backoff (only retries every 10th cycle). Resets to `healthy` on success.

**Busy-thread deferral** — for `agent_prompt` actions, triggers check if the target thread is busy (non-blocking lock check). If busy, events are stored in `pending_events` and retried next cycle. No thread-pool slots are blocked.

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

No config required. Fire via `POST /triggers/fire/{trigger_id}` with a JSON body.

Template variables: `{data}`, `{trigger_name}`, `{fired_at}`, plus any keys in the POST body.

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

Sends a prompt to Nymeria in the trigger's dedicated thread. Template variables from the source event are interpolated into the prompt.

```json
{
  "type": "agent_prompt",
  "config": {
    "prompt_template": "New email from {from_name}: {subject}\n\nPreview: {preview}\n\nPlease summarize and flag if urgent."
  }
}
```

Multiple events in one cycle are batched into a single LLM call to avoid flooding.

### notify

Publishes a notification via SSE to connected clients.

```json
{
  "type": "notify",
  "config": {
    "message_template": "RSS update: {title} — {link}"
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

### Via LLM Conversation

Ask Nymeria directly: "Create a trigger that watches my inbox for emails from X and summarizes them." The agent uses `manage_triggers` and `trigger_sources_info` tools to create and configure triggers through natural conversation.

### Via Desktop/Mobile UI

The Triggers section in the right panel provides:
- **TriggerFeed** — lists all triggers (This Thread / Global tabs)
- **TriggerSetupWizard** — 5-step guided creation (source → config → conditions → action → review)
- **TriggerItem** — toggle, test, view history, edit
- **TriggerHistoryPanel** — execution timeline with expandable details

## Testing Triggers

### Test endpoint

```bash
curl -X POST http://localhost:8000/triggers/{trigger_id}/test?user_id=default \
  -H "Authorization: Bearer $API_KEY"
```

Returns a preview of what would happen if the trigger fired, including sample event data and rendered action template — without actually executing the action.

### Webhook testing

```bash
curl -X POST http://localhost:8000/triggers/fire/{trigger_id} \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "Test webhook payload"}'
```

## Execution History

Every trigger execution is logged with status, duration, event summary, and response/error details. Rolling 200 entries per user.

```bash
# Per-trigger history
curl http://localhost:8000/triggers/{trigger_id}/executions?user_id=default&limit=50 \
  -H "Authorization: Bearer $API_KEY"

# All recent executions
curl http://localhost:8000/triggers/executions/recent?user_id=default&limit=50 \
  -H "Authorization: Bearer $API_KEY"
```

Statuses: `success`, `error`, `partial`, `deferred` (thread was busy).

## Health Monitoring

Health status is tracked per trigger based on source check outcomes:

| Status | Condition | Behavior |
|--------|-----------|----------|
| `healthy` | 0 consecutive errors | Normal 30s polling |
| `degraded` | 2+ consecutive errors | Normal polling, warning logged |
| `failing` | 5+ consecutive errors | Exponential backoff — only checks every 10th cycle (~5 min) |

Resets to `healthy` on the first successful check. Health status and last error are visible in the UI and API responses.

## Architecture

```
Ticker (30s loop)
  → TriggerManager.check_triggers()
    → For each enabled trigger:
      → Source.check(config, state) → events
      → Evaluate conditions → filter events
      → Merge pending_events from previous deferral
      → If agent_prompt + thread busy → defer to pending_events
      → Otherwise → fire_action() or fire_action_batch()
        → agent_prompt: send to LLM thread (is_self_invoke=true)
        → notify: SSE broadcast
        → create_todo: TodoManager.add_todo()
      → Log TriggerExecution
```

Storage: `data/triggers/{user_id}.json` (trigger definitions), `data/triggers/{user_id}_executions.json` (execution log).

## Webhook Integration with External Services

The webhook source works with any service that can send HTTP POST requests:

- **IFTTT** — Use the Webhooks service to POST to `http://your-server:8000/triggers/fire/{trigger_id}`
- **Zapier** — Use the Webhook action to POST JSON payloads
- **GitHub** — Configure repository webhooks to send events to your trigger endpoint
- **Custom scripts** — Any `curl` or HTTP client can fire a webhook trigger

Include an `Authorization: Bearer <api_key>` header and a JSON body with relevant data.
