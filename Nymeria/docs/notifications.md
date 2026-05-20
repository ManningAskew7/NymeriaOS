# Notifications

Nymeria has a pluggable notification system: users configure named
**destinations** (concrete delivery targets) and **profiles** (named bundles
of destinations), and the agent's `notify` tool picks profiles by name.
The agent never sees the underlying channel-type list, so new channels
plug in via a backend registry without touching the tool schema.

## Data model

```
Channel type (registry)      Destination (per-user)       Profile (per-user)
+--------------------+       +--------------------+       +--------------------+
| telegram           |  <-+--| my-phone           |  <-+--| default            |
| discord            |    |  | (type=telegram)    |    |  | dests: [my-phone]  |
| slack              |    |  +--------------------+    |  +--------------------+
| teams              |    |                            |
| webhook            |    +--| work-email         |    +--| urgent             |
| fcm                |       | (type=email_outlook)|   |  | dests:             |
| email_outlook      |       +--------------------+    |  |   [my-phone,       |
+--------------------+                                  |  |    work-email]    |
                                                       |  +--------------------+
                                                       |
                                          +-------------+
                                          |
                                  User-level preference:
                                  default_profile = "default"
                                  (per-thread override available)
```

## Resolution order

When the `notify` tool runs (or the trigger system fires a "notify" action),
the active profile is picked in this order:

1. **`profile=...` arg on the call** (per-call override).
2. **`ThreadConfig.notification_profile`** (per-thread override).
3. **`UserProfile.preferences['notifications']['default_profile']`** (per-user default).
4. **Built-in `"default"`** profile, auto-seeded from existing global env
   config (`TELEGRAM_BOT_TOKEN`, `DISCORD_WEBHOOK_URL`, etc.) the first time
   any code path looks it up.

If the resolved profile name doesn't exist, dispatch is treated as a no-op
(no destinations attempted, no errors). The in-app audit-log row is still
written so the user sees the notification.

## Channel types

Channel types are Python classes registered via
`core.notification_channels.register_channel_type(...)`. Each declares:

- `name`: unique identifier (e.g. `"webhook"`).
- `description`: human label shown in the UI.
- `config_fields`: list of `{key, label, secret, required, help}` UI hints.
- `send(...)`: delivery implementation.
- `auto_seed(settings)`: optional helper that bootstraps a default
  destination from existing env config (used during the BC transition).

Built-in types:

| Name | Description | Notable secrets |
|------|-------------|-----------------|
| `telegram` | Telegram bot. Preserves thread-binding routing -- if the current thread is bound to a Telegram chat, the message is queued via the event bus for the Telegram bot to deliver; otherwise falls back to the destination's configured chat. | `bot_token` |
| `discord` | Discord incoming webhook. | `webhook_url` |
| `slack` | Slack incoming webhook. | `webhook_url` |
| `teams` | Microsoft Teams Graph API. Uses the user's authenticated Outlook OAuth token. | (none -- uses OAuth token cache) |
| `webhook` | Generic JSON POST. Payload: `{message, summary, user_id, thread_id, task_id}`. Use for ntfy.sh, Home Assistant, IFTTT, custom backends. | `bearer_token`, `extra_headers_json` |
| `fcm` | Firebase Cloud Messaging push. Either to a specific device or broadcast to all the user's registered devices. | `device_token` |
| `email_outlook` | Email via the user's authenticated Outlook (Microsoft Graph) account. Reuses the same OAuth token cache as the `outlook_*` tools. | (none -- uses OAuth token cache) |

### Adding a new channel type

```python
# nymeria/core/notification_channels.py

class MyChannel(_BaseChannel):
    name = "my_channel"
    description = "Send via My Service"
    config_fields = [
        {"key": "endpoint", "label": "Endpoint", "secret": False, "required": True},
        {"key": "api_key", "label": "API key", "secret": True, "required": True},
    ]

    def send(self, message, destination, ctx, repo):
        api_key = repo.get_secret_field(dest_id=destination.id, field_name="api_key")
        # ... POST to destination.config["endpoint"] with `api_key`
        return SendResult.success("Sent to My Service")

register_channel_type(MyChannel())
```

The notify tool's schema doesn't change. The destination editor in the
Settings UI automatically picks up the new type from the `/notifications/channel-types`
endpoint and renders the right form fields.

## Audit log (in-app feed)

The desktop sidebar's notifications panel is the audit log of every
`notify` call. Each row carries:

- `summary` -- short message body
- `profile` -- profile that routed the notification (e.g. `"default"`)
- `attempted` -- destination names tried (excludes disabled destinations)
- `delivered_to` -- destinations that succeeded
- `errors` -- `{destination_name: error_message}` for failures

The frontend renders chips for each `delivered_to` entry and red chips for
errors. Rows are stored at `data/notifications/{user_id}.json`; capacity is
200 rows per user, unread rows are never evicted, oldest read rows go first.

The `in_app_notification_level` per-thread setting still gates whether
**autonomous task completions** create rows (`notify_only` = explicit
notify calls only; `all_autonomous` = every autonomous completion;
`off` = nothing). It also suppresses the audit row from a `notify` call
when set to `off`.

## Auto-seed (backward compatibility)

On the first list-destinations / list-profiles / notify call per user,
`ensure_seeded_destinations()` checks each channel type's `auto_seed()`
hook. For every type whose required env vars are populated and whose
default-named destination (`telegram-default`, `discord-default`, etc.)
does NOT already exist, a destination is created and added to the
`"default"` profile.

This means users with existing `.env.docker` webhook config experience
zero behavioural change after the upgrade -- their first `notify` call
still reaches every platform that had its env vars set.

Auto-seed is idempotent; subsequent calls do nothing if all expected
destinations already exist.

## REST API

All routes are user-scoped via `Act-As`; admins can target other users
via the `user_id` query parameter the same way as the activity and
notifications routes.

### Channel types

`GET /notifications/channel-types` -> `{channel_types: [{name, description, config_fields}]}`

### Destinations

```
GET    /notifications/destinations                  list
POST   /notifications/destinations                  create
GET    /notifications/destinations/{id}             get one
PATCH  /notifications/destinations/{id}             update
DELETE /notifications/destinations/{id}             delete (+ cascade-remove from profiles)
POST   /notifications/destinations/{id}/test        send a test message (no audit-log row)
```

Body for create:
```json
{
  "name": "my-phone",
  "type": "telegram",
  "config": {"chat_id": "12345"},
  "secret_fields": {"bot_token": "123:abc"},
  "enabled": true
}
```

Body for update: same shape; secret-field values: a string sets/replaces,
`null` deletes, omitted keys leave the stored value as-is.

### Profiles

```
GET    /notifications/profiles                      list
POST   /notifications/profiles                      create
GET    /notifications/profiles/{id}                 get one
PATCH  /notifications/profiles/{id}                 update (rename or replace destination list)
DELETE /notifications/profiles/{id}                 delete
```

### Preferences

```
GET   /notifications/preferences                    -> {"default_profile": "default"}
PATCH /notifications/preferences                    body: {"default_profile": "urgent"}
```

### In-app feed (existing, with extended payload)

```
GET    /notifications                               list (up to 200 rows, includes audit-log fields)
POST   /notifications/{id}/read                     mark one read
POST   /notifications/read-all                      mark all read
DELETE /notifications/{id}                          delete one
DELETE /notifications                               clear all
```

`NotificationResponse` payload now includes:
```json
{
  "profile": "default",
  "attempted": ["my-phone", "work-email"],
  "delivered_to": ["my-phone"],
  "errors": {"work-email": "No authenticated Outlook account"}
}
```

## MCP tools (agent-driven setup)

The agent can guide the user through notification setup conversationally
via these MCP tools (exposed on `nymeria-mcp`):

- `nymeria_notification_channel_types`
- `nymeria_notification_destination_{list, add, update, delete, test}`
- `nymeria_notification_profile_{list, add, update, delete}`
- `nymeria_notification_preferences_{get, set}`

Typical flow: "Set up email notifications for me" ->
agent calls `_channel_types` to see what's available ->
`_destination_add(type="email_outlook", ...)` ->
`_destination_test(...)` to confirm ->
`_profile_add(name="urgent", destination_names=[...])` ->
`_preferences_set(default_profile="urgent")`.

## Implementation files

| Concern | Path |
|---------|------|
| Destinations + profiles repo | `nymeria/core/notification_destinations.py` |
| Channel type registry + dispatch | `nymeria/core/notification_channels.py` |
| Profile-based send entry point | `nymeria/core/notification_dispatch.py::send_via_profile` |
| In-app feed store (audit log) | `nymeria/core/notifications.py` |
| Notify tool | `nymeria/tools/notify.py` |
| REST routes (destinations/profiles/prefs) | `nymeria/api/routers/notifications_config.py` |
| REST routes (in-app feed) | `nymeria/api/routers/activity.py` |
| MCP setup tools | `nymeria/mcp_server.py` |
| Trigger "notify" action | `nymeria/core/trigger_manager.py::_fire_notify` |
| Desktop sidebar panel | `nymeria-desktop/src/lib/components/notifications/NotificationCenter.svelte` |
| Desktop settings panel | `nymeria-desktop/src/lib/components/notifications/NotificationsPanel.svelte` |
| Desktop store | `nymeria-desktop/src/lib/stores/notifications.svelte.ts` |

## Mobile parity (deferred)

The mobile app (`nymeria-mobile/`) still uses the v1 notification feed
component but does not yet have destinations/profiles settings UI. The
mobile follow-up will mirror the desktop changes in
`nymeria-mobile/src/lib/`. Until then, `scripts/check_cross_app_drift.py`
will flag the divergence; this is expected.
