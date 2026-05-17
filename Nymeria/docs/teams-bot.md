# Microsoft Teams Bot

Nymeria's Microsoft Teams integration is an API-hosted Bot Framework webhook. It receives Teams message activities at `/integrations/teams/webhook`, resolves the Teams sender to a Nymeria account, streams the turn through the in-process agent, and replies through the Bot Connector REST API.

There is no standalone `run.py teams-bot` command and no Docker Compose bot service. The API service hosts the webhook.

## Setup

1. Create or register a Microsoft Teams bot in Azure/Bot Framework.
2. Set the bot messaging endpoint to:

   ```text
   https://<your-public-api-host>/integrations/teams/webhook
   ```

3. Enable Teams as a channel for the bot.
4. Configure `.env.docker`.
5. Link Teams users to Nymeria accounts before sending normal chat traffic.

## Environment

```bash
TEAMS_BOT_APP_ID=00000000-0000-0000-0000-000000000000
TEAMS_BOT_APP_PASSWORD=...
TEAMS_BOT_TENANT_ID=00000000-0000-0000-0000-000000000000
TEAMS_BOT_RESPOND_MODE=mention
TEAMS_BOT_VALIDATE_AUTH=true  # deprecated; validation is always enforced
TEAMS_BOT_SHOW_TOOL_EVENTS=false
```

`TEAMS_BOT_RESPOND_MODE=mention` means:

- Personal chats always respond.
- Group chats and channels respond to bot mentions.
- Thread replies continue when the bot is already participating in that Teams thread.

`TEAMS_BOT_RESPOND_MODE=all` makes group/channel conversations respond to every non-bot message the webhook receives. Use this only in controlled chats.

## Linking Users

Teams users must be linked to Nymeria accounts before agent traffic is accepted. Nymeria stores Teams identities as:

```text
<tenant_id>:<aad_object_id>
```

If an Azure AD object ID is unavailable in the activity, Nymeria falls back to the Teams user ID.

Example admin link:

```bash
python3 run.py users link-platform user@example.com teams tenant-id:aad-object-id
```

Self-service link codes also work. Issue a platform link code for provider `teams`, then DM the bot or mention it with:

```text
link <code>
```

The bot also accepts `link_<code>`.

## Thread Binding

Teams conversations can be bound to desktop-created Nymeria threads with chat-app bind codes:

```text
bind <code>
unbind
```

Bindings are scoped to the current conversation/thread. The bot checks thread-specific bindings first, then conversation-level bindings, then falls back to native Teams thread IDs.

## Native Thread IDs

| Teams context | Nymeria thread ID |
|---------------|-------------------|
| Personal chat | `teams_dm_<tenant>_<user>` |
| Group/channel conversation | `teams_<tenant>_<conversation>` |
| Teams thread/root reply | `teams_<tenant>_<conversation>_thread_<root_message_id>` |

Teams group/channel threads are treated as shared channels in Nymeria access control. Personal chats are per-user.

## Commands

Plain text commands handled by the Teams bot:

| Command | Description |
|---------|-------------|
| `link <code>` | Link the Teams user to a Nymeria account. |
| `bind <code>` | Bind the current Teams conversation to a Nymeria thread. |
| `unbind` | Remove the current Teams conversation binding. |
| `stop` | Abort the current Nymeria run for this Teams conversation. |

Other accepted messages are sent to Nymeria as chat turns.

## Notes

- Incoming Bot Framework bearer tokens are always validated. `TEAMS_BOT_VALIDATE_AUTH` is deprecated and ignored.
- Replies use the incoming activity's `serviceUrl`.
- Duplicate message activities are deduped by activity ID.
- Replies are chunked below 4000 characters.
