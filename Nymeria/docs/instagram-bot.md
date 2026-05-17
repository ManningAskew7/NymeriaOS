# Instagram Bot

Nymeria's Instagram integration is an API-hosted Meta Instagram Messaging webhook. It receives professional-account messaging events at `/integrations/instagram/webhook`, requires `X-Hub-Signature-256` verification with `INSTAGRAM_APP_SECRET`, resolves the Instagram-scoped sender ID to a Nymeria account, streams the turn through the in-process agent, and replies through the Instagram Messaging API.

There is no standalone `run.py instagram-bot` command and no Docker Compose bot service. The API service hosts the webhook.

## Setup

1. Create or select a Meta app with Instagram messaging enabled.
2. Connect the Instagram professional account that should host the Nymeria bot.
3. Generate an access token with Instagram messaging permissions.
4. Configure the webhook callback URL:

   ```text
   https://<your-public-api-host>/integrations/instagram/webhook
   ```

5. Set the webhook verify token to the same value as `INSTAGRAM_WEBHOOK_VERIFY_TOKEN`.
6. Subscribe the app to Instagram messaging events.
7. Set `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_IG_USER_ID`, `INSTAGRAM_WEBHOOK_VERIFY_TOKEN`, and `INSTAGRAM_APP_SECRET` in `.env.docker`.
8. Link Instagram users to Nymeria accounts before sending normal chat traffic.

Meta's webhook setup handshake calls the `GET` endpoint with `hub.mode`, `hub.verify_token`, and `hub.challenge`. Nymeria returns the challenge only when the token matches.

## Environment

```bash
INSTAGRAM_ACCESS_TOKEN=...
INSTAGRAM_IG_USER_ID=...
INSTAGRAM_WEBHOOK_VERIFY_TOKEN=...
INSTAGRAM_APP_SECRET=...
INSTAGRAM_GRAPH_API_BASE_URL=https://graph.instagram.com/v23.0
INSTAGRAM_SHOW_TOOL_EVENTS=false
```

`INSTAGRAM_IG_USER_ID` is recommended for setup clarity and is used to scope native thread IDs and chat binding keys when available.

## Linking Users

Instagram users must be linked to Nymeria accounts before agent traffic is accepted. Nymeria stores Instagram identities as the sender's Instagram-scoped ID (`sender.id`).

Example admin link:

```bash
python3 run.py users link-platform user@example.com instagram <sender-id>
```

Self-service link codes also work. Issue a platform link code for provider `instagram`, then message the connected Instagram account:

```text
link <code>
```

The bot also accepts `link_<code>`.

## Thread Binding

Instagram conversations can be bound to desktop-created Nymeria threads with chat-app bind codes:

```text
bind <code>
unbind
```

Bindings are scoped to the Instagram professional account and sender:

```text
instagram:<igUserId>:<senderId>
```

If no Instagram account ID is available, Nymeria falls back to:

```text
instagram:<senderId>
```

## Native Thread IDs

| Instagram context | Nymeria thread ID |
|---|---|
| Professional account conversation | `instagram_<igUserId>_<senderId>` |
| Conversation without account ID | `instagram_<senderId>` |

Instagram conversations are treated as one-to-one customer chats in Nymeria access control.

## Commands

Plain text commands handled by the Instagram bot:

| Command | Description |
|---|---|
| `link <code>` | Link the Instagram sender to a Nymeria account. |
| `bind <code>` | Bind the current Instagram conversation to a Nymeria thread. |
| `unbind` | Remove the current Instagram chat binding. |
| `stop` | Abort the current Nymeria run for this Instagram chat. |

Slash-style `/stop` is also accepted.

## Limits And Security

- Incoming webhooks must be signed with Meta's raw-body `X-Hub-Signature-256` HMAC. Set `INSTAGRAM_APP_SECRET` before enabling the webhook.
- Meta can redeliver webhook events; Nymeria rejects stale message/postback timestamps and deduplicates recent message IDs and generated postback IDs.
- Nymeria only replies to inbound user messages in v1. It does not implement proactive broadcasts or marketing sends.
- Instagram replies are subject to Meta's messaging policies and response windows.
- Nymeria sends text-only replies in v1 and splits outgoing messages at 1000 characters.
- Echo messages, delivery receipts, read receipts, reactions, and non-text attachments are ignored in v1.
