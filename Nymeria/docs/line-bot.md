# LINE Bot

Nymeria's LINE integration is an API-hosted Messaging API webhook. It receives LINE webhook events at `/integrations/line/webhook`, verifies `x-line-signature` when enabled, resolves the sender to a Nymeria account, streams the turn through the in-process agent, and replies through the LINE Messaging API push endpoint.

There is no standalone `run.py line-bot` command and no Docker Compose bot service. The API service hosts the webhook.

## Setup

1. Create a LINE Messaging API channel for the LINE Official Account.
2. Set the webhook URL in the LINE Developers Console:

   ```text
   https://<your-public-api-host>/integrations/line/webhook
   ```

3. Enable webhook use for the channel.
4. Set `LINE_CHANNEL_ACCESS_TOKEN` and `LINE_CHANNEL_SECRET` in `.env.docker`.
5. Link LINE users to Nymeria accounts before sending normal chat traffic.

The LINE Console webhook verification sends an empty-events payload. Nymeria accepts that payload and returns `{"status":"accepted"}`.

## Environment

```bash
LINE_CHANNEL_ACCESS_TOKEN=...
LINE_CHANNEL_SECRET=...
LINE_BOT_USER_ID=U...
LINE_BOT_NAME=Nymeria
LINE_RESPOND_MODE=mention
LINE_VALIDATE_SIGNATURE=true
LINE_SHOW_TOOL_EVENTS=false
LINE_API_BASE_URL=https://api.line.me/v2/bot
```

`LINE_BOT_USER_ID` is optional. When unset, Nymeria uses the webhook `destination` value for mention matching.

`LINE_RESPOND_MODE=mention` means:

- One-to-one chats always respond.
- Group chats and multi-person rooms respond to bot mentions.
- A group or room continues accepting follow-up replies after Nymeria has joined that conversation.

`LINE_RESPOND_MODE=all` makes groups and rooms respond to every text message the webhook receives. Use this only in controlled chats.

## Linking Users

LINE users must be linked to Nymeria accounts before agent traffic is accepted. Nymeria stores LINE identities as the LINE `source.userId` value.

Example admin link:

```bash
python3 run.py users link-platform user@example.com line U1234567890
```

Self-service link codes also work. Issue a platform link code for provider `line`, then DM the bot or mention it with:

```text
link <code>
```

The bot also accepts `link_<code>`.

## Thread Binding

LINE chats can be bound to desktop-created Nymeria threads with chat-app bind codes:

```text
bind <code>
unbind
```

Bindings are scoped to the current LINE source:

```text
line:user:<userId>
line:group:<groupId>
line:room:<roomId>
```

## Native Thread IDs

| LINE context | Nymeria thread ID |
|---|---|
| One-to-one chat | `line_dm_<userId>` |
| Group chat | `line_group_<groupId>` |
| Multi-person room | `line_room_<roomId>` |

LINE groups and rooms are treated as shared channels in Nymeria access control. One-to-one chats are per-user.

## Commands

Plain text commands handled by the LINE bot:

| Command | Description |
|---|---|
| `link <code>` | Link the LINE sender to a Nymeria account. |
| `bind <code>` | Bind the current LINE chat to a Nymeria thread. |
| `unbind` | Remove the current LINE chat binding. |
| `stop` | Abort the current Nymeria run for this LINE chat. |

Slash-style `/stop` is also accepted.

## Limits And Security

- Incoming webhooks are signed with HMAC-SHA256 over the raw request body. Keep `LINE_VALIDATE_SIGNATURE=true` in production.
- LINE can redeliver webhook events; Nymeria deduplicates recent `webhookEventId` values.
- Replies use push messages so streamed/asynchronous agent turns are not constrained by reply-token timing.
- Nymeria sends text-only replies in v1 and splits outgoing messages at 5000 characters.
- Non-text LINE messages are ignored in v1.
