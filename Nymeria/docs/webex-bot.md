# Webex Bot

Nymeria's Webex integration is hosted by the API service as a Webex Messaging webhook client. It receives Webex `messages.created` webhook payloads, fetches message details through the Webex REST API, resolves Webex senders to Nymeria users, streams chat turns through the in-process agent, and sends replies through the Webex Messages API.

## Architecture

```text
Webex messages.created webhook
     -> Nymeria API /integrations/webex/webhook
          -> NymeriaWebexBot
               -> Webex GET /messages/{id}
               -> platform identity and chat binding repos
               -> NymeriaAgent.astream()
               -> Webex POST /messages
```

There is no separate `run.py webex-bot` process. The API container owns the webhook route, so the public API URL must be reachable from Webex over HTTPS.

## Setup

1. Create a Webex bot in the Webex developer portal.
2. Copy the bot access token.
3. Register a Webex webhook with:
   - `resource`: `messages`
   - `event`: `created`
   - `targetUrl`: `https://your-api-host.example.com/integrations/webex/webhook`
   - `secret`: the same value as `WEBEX_WEBHOOK_SECRET`.
4. Set the environment variables below and restart the API container.
5. Link Webex senders to Nymeria users before sending normal chat traffic.

## Environment

Add to `.env.docker`:

```bash
WEBEX_ACCESS_TOKEN=...
WEBEX_BASE_URL=https://webexapis.com/v1
WEBEX_WEBHOOK_SECRET=choose-a-long-random-secret
WEBEX_BOT_PERSON_ID=...          # optional; otherwise resolved via /people/me
WEBEX_BOT_EMAIL=nymeria@webex.bot # optional mention stripping helper
WEBEX_SHOW_TOOL_EVENTS=false
```

`WEBEX_WEBHOOK_SECRET` is required. POST webhooks must include a valid
`X-Spark-Signature` HMAC-SHA1 signature.

## Linking Users

Webex senders must be linked to Nymeria accounts before agent traffic is accepted. Nymeria stores Webex identities as the sender `personId` from Webex message metadata/details.

Example admin link:

```bash
python3 run.py users link-platform user@example.com webex Y2lzY29...
```

Self-service link codes also work. Issue a platform link code for provider `webex`, then send:

```text
link <code>
```

The bot also accepts `link_<code>`.

## Thread Binding

Webex rooms can be bound to desktop-created Nymeria threads with chat-app bind codes:

```text
bind <code>
unbind
```

Binding keys:

```text
webex:<room_id>
webex:<room_id>:<parent_id>
```

## Native Thread IDs

| Webex context | Nymeria thread ID |
|---------------|-------------------|
| 1:1 room | `webex_dm_<person_id>` |
| Group space | `webex_<room_id>` |
| Threaded/root reply | `webex_<room_id>_thread_<parent_id>` |

Webex direct-message threads are private per sender. Group-space threads are classified as shared.

## Commands

Plain text commands handled by the Webex bot:

| Command | Description |
|---------|-------------|
| `link <code>` | Link the Webex sender to a Nymeria account. |
| `bind <code>` | Bind the current Webex room/thread to a Nymeria thread. |
| `unbind` | Remove the current Webex chat binding. |
| `stop` | Abort the current Nymeria run for this Webex chat. |

Other text messages are sent to Nymeria as chat turns.

## Limitations

- Webex webhook only: no polling or websocket listener in v1.
- Text/markdown message details only in v1; inbound files and adaptive-card actions are ignored.
- Webex bots can read direct messages and group-space messages where the bot is mentioned. Group-space webhook configuration should use normal bot visibility expectations, and `mentionedPeople=me` filters are useful when manually listing messages.
