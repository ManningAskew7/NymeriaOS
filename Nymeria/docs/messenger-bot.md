# Messenger Bot

Nymeria's Messenger integration is an API-hosted Meta Messenger Platform webhook. It receives Page webhook events at `/integrations/messenger/webhook`, verifies `X-Hub-Signature-256` when `MESSENGER_APP_SECRET` is configured, resolves the sender's Page-scoped ID to a Nymeria account, streams the turn through the in-process agent, and replies through the Messenger Send API.

There is no standalone `run.py messenger-bot` command and no Docker Compose bot service. The API service hosts the webhook.

## Setup

1. Create or select a Meta app with Messenger Platform enabled.
2. Connect the Facebook Page that should host the Nymeria bot.
3. Generate a Page access token with Messenger send permissions.
4. Configure the webhook callback URL:

   ```text
   https://<your-public-api-host>/integrations/messenger/webhook
   ```

5. Set the webhook verify token to the same value as `MESSENGER_WEBHOOK_VERIFY_TOKEN`.
6. Subscribe the app to Page messaging events such as messages and postbacks.
7. Set `MESSENGER_PAGE_ACCESS_TOKEN`, `MESSENGER_PAGE_ID`, `MESSENGER_WEBHOOK_VERIFY_TOKEN`, and `MESSENGER_APP_SECRET` in `.env.docker`.
8. Link Messenger users to Nymeria accounts before sending normal chat traffic.

Meta's webhook setup handshake calls the `GET` endpoint with `hub.mode`, `hub.verify_token`, and `hub.challenge`. Nymeria returns the challenge only when the token matches.

## Environment

```bash
MESSENGER_PAGE_ACCESS_TOKEN=...
MESSENGER_PAGE_ID=...
MESSENGER_WEBHOOK_VERIFY_TOKEN=...
MESSENGER_APP_SECRET=...
MESSENGER_GRAPH_API_BASE_URL=https://graph.facebook.com/v23.0
MESSENGER_SHOW_TOOL_EVENTS=false
```

`MESSENGER_PAGE_ID` is recommended for setup clarity and is used as a fallback when sending replies. Inbound webhook payloads also include the recipient Page ID, and Nymeria scopes chat bindings with it.

## Linking Users

Messenger users must be linked to Nymeria accounts before agent traffic is accepted. Nymeria stores Messenger identities as the sender's Page-scoped ID (`sender.id` / PSID).

Example admin link:

```bash
python3 run.py users link-platform user@example.com messenger <PSID>
```

Self-service link codes also work. Issue a platform link code for provider `messenger`, then message the Page:

```text
link <code>
```

The bot also accepts `link_<code>`.

## Thread Binding

Messenger conversations can be bound to desktop-created Nymeria threads with chat-app bind codes:

```text
bind <code>
unbind
```

Bindings are scoped to the Page and sender:

```text
messenger:<pageId>:<psid>
```

If no Page ID is available, Nymeria falls back to:

```text
messenger:<psid>
```

## Native Thread IDs

| Messenger context | Nymeria thread ID |
|---|---|
| Page conversation | `messenger_<pageId>_<psid>` |
| Page conversation without Page ID | `messenger_<psid>` |

Messenger conversations are treated as one-to-one customer chats in Nymeria access control.

## Commands

Plain text commands handled by the Messenger bot:

| Command | Description |
|---|---|
| `link <code>` | Link the Messenger sender to a Nymeria account. |
| `bind <code>` | Bind the current Messenger conversation to a Nymeria thread. |
| `unbind` | Remove the current Messenger chat binding. |
| `stop` | Abort the current Nymeria run for this Messenger chat. |

Slash-style `/stop` is also accepted.

## Limits And Security

- Incoming webhooks should be signed with Meta's raw-body `X-Hub-Signature-256` HMAC. Set `MESSENGER_APP_SECRET` in production.
- Meta can redeliver webhook events; Nymeria deduplicates recent message IDs and generated postback IDs.
- Nymeria only replies to inbound user messages in v1. It does not implement proactive broadcasts or marketing sends.
- Messenger replies are subject to Meta's messaging policies, including the standard response window after a user message.
- Nymeria sends text-only replies in v1 and splits outgoing messages at 2000 characters.
- Echo messages, delivery receipts, read receipts, and non-text attachments are ignored in v1.
